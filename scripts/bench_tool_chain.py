"""Measure how far a candidate model will *chain* tool calls the way Jarvis needs it to.

Benchmarks like BFCL score single, well-specified calls. What decides whether a model can hold
the fast slot is different: given "find the file that mentions X, read it, write a summary", does
it make the three dependent calls, in order, using the previous result -- or does it stop after
one and narrate the rest? The 2026-09 MiniCPM5-2B trial failed exactly there ("one tool call where
the 4B chained four"), so this drives the real ``llama-server.exe`` with the same flags the backend
uses for the fast slot, offers the real tool schemas from ``app.agent.tools.registry``, and runs
each model through five tasks that can only be completed with 3-4 sequential calls. Tool results
come from a small simulated filesystem/web, so every model sees identical inputs and a task's
success is checked mechanically (did the final write/clipboard/toast carry the right content?).

Examples (from the repo root, venv python)::

    python scripts/bench_tool_chain.py models/qwen3.5-2b/Qwen3.5-2B-UD-Q4_K_XL.gguf models/minicpm5-2b/MiniCPM5-2B-Q4_K_M.gguf
    python scripts/bench_tool_chain.py models/qwen3.5-4b/Qwen3.5-4B-UD-Q4_K_XL.gguf --thinking
    python scripts/bench_tool_chain.py <model> --json results.json --ctx 8192

Standalone like ``bench_kv_cache.py`` (httpx + pynvml from the venv, plus the backend package for
the tool schemas) and on port 8012, so it never collides with the live server on 8001.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
SERVER = REPO / "tools" / "llama-cpp" / "llama-server.exe"
PORT = 8012
MAX_ROUNDS = 8

# The flags the backend's fast slot runs with (backend/.env LLAMA_EXTRA_ARGS), minus the sampling
# temperature: chaining is judged greedily so a run is reproducible and a miss is the model's, not
# the dice's.
EXTRA_ARGS = ["-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--spec-type", "ngram-simple",
              "--top-p", "0.95", "--top-k", "20", "--min-p", "0.0"]

SYSTEM_PROMPT = (
    "You are Jarvis, a local assistant on the user's Windows PC. You have tools. When a task needs "
    "several steps, call the tools one after another, using each result for the next call, until "
    "the task is actually done. Do not describe what you would do -- do it. Answer briefly when "
    "finished."
)


# --------------------------------------------------------------------------------------------
# Simulated world
# --------------------------------------------------------------------------------------------

FILES: dict[str, str] = {
    "project/main.py": "import util\n\n# TODO: handle empty input\n\ndef run():\n    return util.go()\n",
    "project/util.py": "def go():\n    # TODO: cache the result\n    return 42\n\n# TODO: remove debug print\nprint('x')\n",
    "project/README.md": "# Project\n\nNothing to do here.\n",
    "project/config.json": '{\n  "name": "demo",\n  "version": "1.4.3",\n  "debug": false\n}\n',
    "notes/2026-09-01-meeting.md": "# Meeting 1 Sep\n\nAction items:\n- Alice: draft the budget\n",
    "notes/2026-09-18-meeting.md": (
        "# Meeting 18 Sep\n\nDiscussion about the launch.\n\nAction items:\n"
        "- Bob: book the venue\n- Carol: send invites\n- Dave: order the cake\n"
    ),
    "notes/ideas.txt": "random ideas\n",
    "logs/app.log": "INFO start\nERROR db timeout\nINFO retry\nERROR db timeout\nINFO ok\n",
    "logs/worker.log": "INFO start\nERROR queue full\nERROR queue full\nERROR queue full\nWARN slow\nERROR disk\n",
}

WEB_RESULTS = [
    {"title": "llama.cpp release b7412: MTP speculative decoding merged", "url": "https://example.com/llamacpp/b7412",
     "snippet": "Release notes for b7412."},
    {"title": "Unsloth: Qwen3.6 MTP GGUFs", "url": "https://example.com/unsloth-mtp", "snippet": "MTP GGUFs now available."},
]
WEB_PAGES = {
    "https://example.com/llamacpp/b7412": "llama.cpp b7412\n\nThis release merges multi-token prediction (MTP) "
                                          "speculative decoding for Qwen3.5/3.6 GGUFs. Build number: b7412.",
    "https://example.com/unsloth-mtp": "Unsloth blog. MTP works with llama.cpp b7412 or newer.",
}


def _norm(p: str) -> str:
    return p.replace("\\", "/").strip("/").removeprefix("./")


class World:
    """The fake filesystem, web and OS the tools act on; records every side effect for scoring."""

    def __init__(self) -> None:
        self.files = dict(FILES)
        self.clipboard = ""
        self.toasts: list[dict[str, Any]] = []
        self.writes: dict[str, str] = {}

    # Each handler mirrors the real tool's argument names (registry.py *Args models) and returns
    # a result string in the same spirit as the real tool -- a listing, a file body, a status.
    def list_directory(self, directory_path: str = ".", **_: Any) -> str:
        d = _norm(directory_path)
        if d in (".", ""):
            names = sorted({f.split("/")[0] for f in self.files})
        else:
            names = sorted({f[len(d) + 1:].split("/")[0] for f in self.files if f.startswith(d + "/")})
        if not names:
            return f"Error: directory not found: {directory_path}"
        return "\n".join(names)

    def read_file(self, file_path: str, **_: Any) -> str:
        f = self.files.get(_norm(file_path))
        return f if f is not None else f"Error: file not found: {file_path}"

    def write_file(self, file_path: str, content: str = "", **_: Any) -> str:
        self.files[_norm(file_path)] = content
        self.writes[_norm(file_path)] = content
        return f"Wrote {len(content)} characters to {file_path}"

    def patch_file(self, file_path: str, search_block: str = "", replacement_block: str = "", **_: Any) -> str:
        p = _norm(file_path)
        body = self.files.get(p)
        if body is None:
            return f"Error: file not found: {file_path}"
        if search_block not in body:
            return "Error: search_block not found in file"
        body = body.replace(search_block, replacement_block, 1)
        self.files[p] = body
        self.writes[p] = body
        return f"Patched {file_path}"

    def find_files(self, pattern: str = "*", root_dir: str = ".", **_: Any) -> str:
        d = _norm(root_dir)
        rx = re.compile("^" + re.escape(pattern).replace(r"\*", ".*") + "$")
        hits = [f for f in self.files if (d in (".", "") or f.startswith(d + "/")) and rx.match(f.split("/")[-1])]
        return "\n".join(hits) or "No files matched."

    def grep_in_files(self, pattern: str, path: str = ".", **_: Any) -> str:
        d = _norm(path)
        rx = re.compile(pattern)
        out = []
        for f, body in self.files.items():
            if d not in (".", "") and not f.startswith(d + "/"):
                continue
            for i, line in enumerate(body.splitlines(), 1):
                if rx.search(line):
                    out.append(f"{f}:{i}: {line}")
        return "\n".join(out) or "No matches."

    def get_clipboard(self, **_: Any) -> str:
        return self.clipboard

    def set_clipboard(self, text: str = "", **_: Any) -> str:
        self.clipboard = text
        return "Clipboard updated."

    def send_toast(self, title: str = "", message: str = "", **_: Any) -> str:
        self.toasts.append({"title": title, "message": message})
        return "Toast shown."

    def web_search(self, query: str, **_: Any) -> str:
        return json.dumps(WEB_RESULTS, indent=1)

    def fetch_url(self, url: str, **_: Any) -> str:
        return WEB_PAGES.get(url, f"Error: 404 for {url}")

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        fn = getattr(self, name, None)
        if fn is None:
            return f"Error: unknown tool {name}", False
        try:
            return fn(**args), True
        except TypeError as exc:
            return f"Error: bad arguments for {name}: {exc}", False


# --------------------------------------------------------------------------------------------
# Tasks: each needs a chain of dependent calls; ``check`` inspects the world afterwards.
# --------------------------------------------------------------------------------------------

@dataclass
class Task:
    name: str
    prompt: str
    min_chain: int                       # fewest sequential calls a correct solution needs
    check: Callable[[World, str], bool]  # (world, final assistant text) -> success


def _t1(w: World, _: str) -> bool:
    body = w.writes.get("todo_summary.txt") or w.writes.get("project/todo_summary.txt") or ""
    return all(s in body for s in ("empty input", "cache the result", "remove debug print"))


def _t2(w: World, _: str) -> bool:
    return all(s in w.clipboard for s in ("book the venue", "send invites", "order the cake")) and "budget" not in w.clipboard


def _t3(w: World, _: str) -> bool:
    body = w.files.get("project/config.json", "")
    return '"1.4.4"' in body and "project/config.json" in w.writes


def _t4(_: World, text: str) -> bool:
    return "b7412" in text


def _t5(w: World, text: str) -> bool:
    msg = " ".join(t["title"] + " " + t["message"] for t in w.toasts) + " " + text
    return "worker" in msg.lower() and bool(w.toasts)


TASKS = [
    Task("todo-summary",
         "In the project folder, find every Python file that contains a TODO comment, read them, and "
         "write a file todo_summary.txt in the project folder listing each TODO line.",
         3, _t1),
    Task("newest-note-to-clipboard",
         "Look in the notes folder, open the most recent meeting note, and copy just its action items "
         "to the clipboard.",
         3, _t2),
    Task("bump-version",
         "Read project/config.json, bump the patch part of the version number by one, and save the file.",
         2, _t3),
    Task("web-then-fetch",
         "Search the web for which llama.cpp release merged MTP speculative decoding, open the top "
         "result, and tell me the build number.",
         2, _t4),
    Task("compare-logs-toast",
         "Which of the log files in the logs folder has more ERROR lines? Read both, then send me a "
         "toast notification with the answer.",
         3, _t5),
]


# --------------------------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------------------------

def gpu_used_mib() -> float:
    try:
        import pynvml
    except ImportError:
        return 0.0
    pynvml.nvmlInit()
    try:
        return pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(0)).used / (1024 * 1024)
    finally:
        pynvml.nvmlShutdown()


def projector_for(model: Path) -> Path | None:
    cands = sorted(model.parent.glob("mmproj*.gguf"), key=lambda c: c.stat().st_size)
    return cands[0] if cands else None


class Server:
    def __init__(self, model: Path, ctx: int, thinking: bool, mmproj: bool) -> None:
        self.model, self.ctx, self.thinking, self.mmproj = model, ctx, thinking, mmproj
        self.proc: subprocess.Popen[str] | None = None
        self.vram_delta = 0.0

    def __enter__(self) -> "Server":
        idle = gpu_used_mib()
        cmd = [str(SERVER), "-m", str(self.model), "-c", str(self.ctx), "-ngl", "99", "--no-mmap",
               "--parallel", "1", "--jinja", "--host", "127.0.0.1", "--port", str(PORT), *EXTRA_ARGS,
               "--chat-template-kwargs", json.dumps({"enable_thinking": self.thinking}),
               "--reasoning", "on" if self.thinking else "off"]
        proj = projector_for(self.model) if self.mmproj else None
        if proj:
            cmd += ["--mmproj", str(proj)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace")
        tail: list[str] = []
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            tail.append(line.rstrip())
            tail = tail[-40:]
            if "model loaded" in line:
                break
            if self.proc.poll() is not None:
                print("\n".join(tail))
                raise SystemExit(f"llama-server exited with code {self.proc.returncode}")
        threading.Thread(target=lambda: [None for _ in self.proc.stdout], daemon=True).start()  # type: ignore[union-attr]
        with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=120) as c:
            for _ in range(240):
                try:
                    if c.get("/health").json().get("status") == "ok":
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
        time.sleep(1.0)
        self.vram_delta = gpu_used_mib() - idle
        return self

    def __exit__(self, *_: Any) -> None:
        if self.proc:
            self.proc.kill()
            self.proc.wait(timeout=30)


# --------------------------------------------------------------------------------------------
# Running one task
# --------------------------------------------------------------------------------------------

@dataclass
class Result:
    task: str
    success: bool
    calls: list[str] = field(default_factory=list)
    rounds: int = 0
    bad_calls: int = 0          # unknown tool or unusable arguments
    unparsed: bool = False      # tool-call-looking text the server did not parse as a call
    seconds: float = 0.0
    completion_tokens: int = 0
    final_text: str = ""

    @property
    def chain(self) -> int:
        return len(self.calls)


TOOL_CALL_TEXT = re.compile(r"<tool_call>|\"name\"\s*:\s*\"(read_file|list_directory|write_file)\"|<function=")


def run_task(client: httpx.Client, tools: list[dict[str, Any]], task: Task) -> Result:
    world = World()
    res = Result(task=task.name, success=False)
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT},
                                      {"role": "user", "content": task.prompt}]
    t0 = time.time()
    for _ in range(MAX_ROUNDS):
        res.rounds += 1
        r = client.post("/v1/chat/completions", json={
            "messages": messages, "tools": tools, "tool_choice": "auto",
            "temperature": 0.0, "max_tokens": 1024, "cache_prompt": True,
        })
        r.raise_for_status()
        body = r.json()
        res.completion_tokens += body.get("usage", {}).get("completion_tokens", 0)
        msg = body["choices"][0]["message"]
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            res.final_text = content.strip()
            if TOOL_CALL_TEXT.search(content):
                res.unparsed = True
            break
        # Feed every call back, exactly as the orchestrator would.
        messages.append({"role": "assistant", "content": content or None, "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw = fn.get("arguments", {})
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            except json.JSONDecodeError:
                args, ok_args = {}, False
            else:
                ok_args = True
            out, ok = world.call(name, args)
            if not (ok and ok_args):
                res.bad_calls += 1
            res.calls.append(name)
            messages.append({"role": "tool", "tool_call_id": tc.get("id", name), "content": out})
    res.seconds = time.time() - t0
    res.success = task.check(world, res.final_text)
    return res


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------

def jarvis_tools() -> list[dict[str, Any]]:
    """The real schemas the backend sends, for the tools the simulated world implements."""
    from app.agent.tool_schema import convert_tool_to_openai_schema
    from app.agent.tools import registry

    names = ("read_file", "list_directory", "write_file", "patch_file", "find_files", "grep_in_files",
             "get_clipboard", "set_clipboard", "send_toast", "web_search", "fetch_url")
    out = []
    for n in names:
        schema = convert_tool_to_openai_schema(getattr(registry, n))
        if schema:
            out.append(schema)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("models", nargs="+", type=Path)
    ap.add_argument("--ctx", type=int, default=8192, help="context size (fast slot default 8192)")
    ap.add_argument("--thinking", action="store_true", help="enable_thinking=true (fast slot runs with it off)")
    ap.add_argument("--no-mmproj", action="store_true", help="do not load a projector even if one sits beside the model")
    ap.add_argument("--json", type=Path, help="write all results here")
    a = ap.parse_args()

    tools = jarvis_tools()
    print(f"{len(tools)} tool schemas from the registry; {len(TASKS)} tasks; thinking={'on' if a.thinking else 'off'}\n")

    all_results: dict[str, Any] = {}
    for model in a.models:
        if not model.exists():
            print(f"skip {model}: not found")
            continue
        print(f"=== {model.name}")
        with Server(model, a.ctx, a.thinking, not a.no_mmproj) as srv:
            print(f"    VRAM delta after load: {srv.vram_delta:.0f} MiB")
            with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=600) as c:
                results = [run_task(c, tools, t) for t in TASKS]
        for r, t in zip(results, TASKS):
            flag = "PASS" if r.success else "fail"
            extra = " unparsed-tool-text" if r.unparsed else ""
            print(f"    {flag}  {t.name:26s} chain {r.chain:2d} (need >= {t.min_chain})  bad {r.bad_calls}  "
                  f"{r.seconds:5.1f}s  {r.completion_tokens:4d} tok  [{' > '.join(r.calls) or '-'}]{extra}")
            if not r.success and r.final_text:
                print(f"           final: {r.final_text[:160]!r}")
        passed = sum(r.success for r in results)
        chained = sum(r.chain >= t.min_chain for r, t in zip(results, TASKS))
        print(f"    => {passed}/{len(TASKS)} tasks correct, {chained}/{len(TASKS)} chained enough, "
              f"{sum(r.seconds for r in results):.0f}s total\n")
        all_results[str(model)] = {
            "vram_delta_mib": srv.vram_delta, "passed": passed, "chained": chained,
            "tasks": [r.__dict__ for r in results],
        }

    if a.json:
        a.json.write_text(json.dumps(all_results, indent=2))
        print(f"wrote {a.json}")


if __name__ == "__main__":
    main()
