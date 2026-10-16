"""Blind A/B review of two eval runs (PLAN 4.13 #8, option B).

Reads two result files from ``eval_jarvis_tasks.py --json``, pairs their answers to the same task and seed,
and serves a local page that shows each prompt's two answers unlabelled, in random order. You pick the
better one (or tie / skip); the page then reveals which model wrote which, whether each passed the eval's
check and which tools it called. Votes are appended to ``data/preference_votes.jsonl`` as
``{prompt, chosen, rejected, ...}`` -- the preference data #10 trains from (DPO). Pairs with identical
answers, or two empty ones, are skipped; pairs already voted on are skipped too, so a review can be stopped
and resumed. Which side is which never reaches the page before the vote.

Repo root, venv python (no model needed, nothing else running required)::

    python scripts/review_answers.py scripts/eval_results/2026-10-16-research/qwen3.5-9b-iq3xxs_r3.json \\
        scripts/eval_results/2026-10-16-research/defiant-fable-9b-iq3xxs_r3.json

then open http://127.0.0.1:8010 (it opens by itself). Keys: 1 = left, 2 = right, 3 = tie, S = skip,
Enter = next after the reveal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
VOTES_PATH = ROOT / "data" / "preference_votes.jsonl"
PORT = 8010  # 8000-8004 are Jarvis's own


@dataclass
class Pair:
    key: str              # stable across runs: both files, task, seed
    task: str
    group: str
    rep: int
    turns: list[str]      # the user's turns; the answers are to the last one
    answers: dict[str, dict[str, Any]]  # model name -> result row
    left: str             # model shown on the left (never sent before the vote)
    right: str


def model_name(path: Path) -> str:
    return path.stem


def task_turns() -> dict[str, list[str]]:
    """Each eval task's user turns, from the eval script itself."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(ROOT / "backend"))
    import eval_jarvis_tasks as ev
    return {t.id: list(t.turns) for t in ev.TASKS}


def _by_rep(rows: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    """Result files are written seed after seed, so the n-th row of a task is seed n."""
    seen: dict[str, int] = {}
    out = {}
    for row in rows:
        rep = seen.get(row["id"], 0)
        seen[row["id"]] = rep + 1
        out[(row["id"], rep)] = row
    return out


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def build_pairs(file_a: Path, file_b: Path, turns: dict[str, list[str]], voted: set[str],
                rng: random.Random) -> list[Pair]:
    a, b = model_name(file_a), model_name(file_b)
    if a == b:
        a, b = f"{a} (1)", f"{b} (2)"
    rows_a = _by_rep(json.loads(file_a.read_text(encoding="utf-8")))
    rows_b = _by_rep(json.loads(file_b.read_text(encoding="utf-8")))
    pairs = []
    for (task, rep), ra in rows_a.items():
        rb = rows_b.get((task, rep))
        if rb is None:
            continue
        fa, fb = _norm(ra.get("final", "")), _norm(rb.get("final", ""))
        if fa == fb:  # identical (or both empty): nothing to choose
            continue
        names = sorted([file_a.name, file_b.name])
        key = hashlib.sha256(f"{names[0]}|{names[1]}|{task}|{rep}".encode()).hexdigest()[:16]
        if key in voted:
            continue
        left, right = (a, b) if rng.random() < 0.5 else (b, a)
        pairs.append(Pair(key, task, ra.get("group", ""), rep, turns.get(task, []), {a: ra, b: rb}, left, right))
    rng.shuffle(pairs)
    return pairs


def voted_keys(path: Path = VOTES_PATH) -> set[str]:
    if not path.exists():
        return set()
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            keys.add(json.loads(line).get("pair"))
        except json.JSONDecodeError:
            continue
    return keys


def vote_record(pair: Pair, choice: str) -> Optional[dict[str, Any]]:
    """The line saved for a vote; None for a skip. choice: left / right / tie."""
    if choice not in ("left", "right", "tie"):
        return None
    winner = pair.left if choice in ("left", "tie") else pair.right
    loser = pair.right if winner == pair.left else pair.left
    return {
        "pair": pair.key,
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "review-page",
        "task": pair.task,
        "rep": pair.rep,
        "prompt": pair.turns[-1] if pair.turns else "",
        "earlier_turns": pair.turns[:-1],  # multi-turn tasks: only the last answer is compared
        "chosen": pair.answers[winner].get("final", ""),
        "rejected": pair.answers[loser].get("final", ""),
        "chosen_model": winner,
        "rejected_model": loser,
        "tie": choice == "tie",  # DPO skips these; kept so the tally is complete
    }


def reveal(pair: Pair) -> dict[str, Any]:
    def side(model: str) -> dict[str, Any]:
        r = pair.answers[model]
        return {"model": model, "passed": bool(r.get("success")), "calls": r.get("calls") or [],
                "seconds": round(float(r.get("seconds") or 0), 1)}
    return {"left": side(pair.left), "right": side(pair.right)}


class Review:
    def __init__(self, pairs: list[Pair], votes_path: Path = VOTES_PATH):
        self.pairs = pairs
        self.votes_path = votes_path
        self.pos = 0
        self.tally: dict[str, int] = {}
        self.lock = threading.Lock()

    def current(self) -> dict[str, Any]:
        with self.lock:
            if self.pos >= len(self.pairs):
                return {"done": True, "tally": self.tally, "total": len(self.pairs)}
            p = self.pairs[self.pos]
            return {"done": False, "key": p.key, "index": self.pos + 1, "total": len(self.pairs),
                    "task": p.task, "group": p.group, "turns": p.turns,
                    "left": p.answers[p.left].get("final", ""), "right": p.answers[p.right].get("final", "")}

    def vote(self, key: str, choice: str) -> dict[str, Any]:
        with self.lock:
            if self.pos >= len(self.pairs) or self.pairs[self.pos].key != key:
                return {"error": "stale pair; reload"}
            p = self.pairs[self.pos]
            record = vote_record(p, choice)
            if record is not None:
                self.votes_path.parent.mkdir(parents=True, exist_ok=True)
                with self.votes_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                label = "tie" if record["tie"] else record["chosen_model"]
                self.tally[label] = self.tally.get(label, 0) + 1
            self.pos += 1
            return {"reveal": reveal(p), "choice": choice, "tally": self.tally}


def make_handler(review: Review):
    page = PAGE.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif self.path == "/api/pair":
                self._json(review.current())
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self.path != "/api/vote":
                return self._json({"error": "not found"}, 404)
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            self._json(review.vote(str(data.get("key")), str(data.get("choice"))))

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Answer review</title>
<style>
:root{--bg:#f5f5f7;--card:#fff;--ink:#1d1d1f;--muted:#6e6e73;--line:#d2d2d7;--blue:#0a84ff;--green:#30a14e;--red:#d1453b}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--ink:#f5f5f7;--muted:#98989d;--line:#38383a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Segoe UI Variable","Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline;z-index:1}
header h1{font-size:17px;margin:0}header .meta{color:var(--muted);font-size:13px}#tally{margin-left:auto;color:var(--muted);font-size:13px}
main{max-width:1400px;margin:0 auto;padding:16px 20px 120px}
.prompt{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin-bottom:16px}
.prompt .earlier{color:var(--muted);font-size:13px}.prompt .last{font-weight:600}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media (max-width:800px){.cols{grid-template-columns:1fr}}
.ans{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px;min-width:0;overflow-wrap:anywhere}
.ans.win{border-color:var(--blue);box-shadow:0 0 0 2px var(--blue)}.ans h2{font-size:13px;color:var(--muted);margin:0 0 8px;font-weight:600}
.ans pre{background:var(--bg);padding:10px;border-radius:8px;overflow:auto;font-size:13px}.ans code{font-family:Consolas,monospace;font-size:13px}
.ans .empty{color:var(--muted);font-style:italic}
.who{margin-top:12px;padding-top:10px;border-top:1px solid var(--line);font-size:13px}.who b{font-size:14px}
.ok{color:var(--green)}.no{color:var(--red)}
footer{position:fixed;bottom:0;left:0;right:0;background:var(--bg);border-top:1px solid var(--line);padding:12px;display:flex;gap:10px;justify-content:center}
button{font:inherit;padding:8px 18px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.primary{background:var(--blue);border-color:var(--blue);color:#fff}button:disabled{opacity:.4;cursor:default}
kbd{font-size:11px;color:var(--muted);margin-left:6px}
</style></head><body>
<header><h1>Which answer is better?</h1><span class="meta" id="meta"></span><span id="tally"></span></header>
<main id="main"></main>
<footer id="bar">
<button data-c="left">Left<kbd>1</kbd></button><button data-c="tie">Tie<kbd>3</kbd></button>
<button data-c="right">Right<kbd>2</kbd></button><button data-c="skip">Skip<kbd>S</kbd></button>
<button class="primary" id="next" hidden>Next<kbd>Enter</kbd></button>
</footer>
<script>
let pair = null, revealed = false;
const esc = s => s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
function md(src){
  if(!src.trim()) return '<p class="empty">(empty answer)</p>';
  const blocks = [];
  src = src.replace(/```[^\n]*\n([\s\S]*?)```/g,(m,c)=>{blocks.push('<pre><code>'+esc(c)+'</code></pre>');return '\u0000'+(blocks.length-1)+'\u0000';});
  let html = esc(src)
    .replace(/^#{1,6} (.*)$/gm,'<p><b>$1</b></p>')
    .replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/^\s*[-*] (.*)$/gm,'&bull; $1')
    .replace(/\n/g,'<br>');
  return html.replace(/\u0000(\d+)\u0000/g,(m,i)=>blocks[+i]);
}
function side(r){return `<div class="who"><b>${esc(r.model)}</b> &middot; <span class="${r.passed?'ok':'no'}">${r.passed?'passed':'failed'} the check</span> &middot; ${r.seconds}s<br>tools: ${esc(r.calls.join(' > ')||'none')}</div>`;}
function tally(t){document.getElementById('tally').textContent=Object.entries(t||{}).map(([k,v])=>`${k}: ${v}`).join('  ·  ');}
async function load(){
  revealed=false; document.getElementById('next').hidden=true;
  document.querySelectorAll('[data-c]').forEach(b=>b.disabled=false);
  pair = await (await fetch('/api/pair')).json();
  const main=document.getElementById('main');
  if(pair.done){main.innerHTML=`<div class="prompt">All ${pair.total} pairs reviewed. Votes are in data/preference_votes.jsonl.</div>`;
    document.getElementById('bar').hidden=true;tally(pair.tally);return;}
  document.getElementById('meta').textContent=`${pair.index} / ${pair.total} · ${pair.group} · ${pair.task}`;
  const earlier=pair.turns.slice(0,-1).map(t=>`<div class="earlier">earlier: ${esc(t)}</div>`).join('');
  main.innerHTML=`<div class="prompt">${earlier}<div class="last">${esc(pair.turns.at(-1)||pair.task)}</div></div>
   <div class="cols"><div class="ans" id="L"><h2>Left</h2>${md(pair.left)}</div><div class="ans" id="R"><h2>Right</h2>${md(pair.right)}</div></div>`;
  window.scrollTo(0,0);
}
async function vote(choice){
  if(revealed||!pair||pair.done) return;
  revealed=true;
  const res = await (await fetch('/api/vote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:pair.key,choice})})).json();
  if(res.error){alert(res.error);location.reload();return;}
  if(choice==='skip'){load();return;}
  document.getElementById('L').insertAdjacentHTML('beforeend',side(res.reveal.left));
  document.getElementById('R').insertAdjacentHTML('beforeend',side(res.reveal.right));
  if(choice==='left') document.getElementById('L').classList.add('win');
  if(choice==='right') document.getElementById('R').classList.add('win');
  tally(res.tally);
  document.querySelectorAll('[data-c]').forEach(b=>b.disabled=true);
  document.getElementById('next').hidden=false;
}
document.querySelectorAll('[data-c]').forEach(b=>b.onclick=()=>vote(b.dataset.c));
document.getElementById('next').onclick=load;
document.addEventListener('keydown',e=>{
  if(e.key==='1')vote('left');else if(e.key==='2')vote('right');else if(e.key==='3')vote('tie');
  else if(e.key.toLowerCase()==='s')vote('skip');else if(e.key==='Enter'&&revealed)load();
});
load();
</script></body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a", type=Path, help="first eval result file (--json output)")
    ap.add_argument("b", type=Path, help="second eval result file")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--votes", type=Path, default=VOTES_PATH,
                    help="where votes go (another reviewer's votes in their own file, e.g. Claude's)")
    args = ap.parse_args()

    pairs = build_pairs(args.a, args.b, task_turns(), voted_keys(args.votes), random.Random())
    print(f"{len(pairs)} pairs to review ({model_name(args.a)} vs {model_name(args.b)}); votes -> {args.votes}")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Review(pairs, votes_path=args.votes)))
    url = f"http://127.0.0.1:{args.port}"
    print(f"open {url}  (Ctrl+C to stop; already-voted pairs are skipped next time)")
    if not args.no_open:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
