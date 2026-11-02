"""Long soak: hours of chat, RAG, compaction and cancelled turns against an isolated backend, with RAM logged.

PLAN 4.29 B8 / the open item in 4.26 ("RAM over a long session"). Run it with Jarvis closed (it needs the GPU):

    .venv\\Scripts\\python.exe scripts\\soak.py --hours 3

It starts its own backend on port 8010 with every state path in a temp folder (as eval_real_chat does), so the
real database, notes and Google data are never touched. Every --sample seconds it logs, to
scripts/soak_results/<stamp>/samples.csv, the working set / private bytes of the backend and of each process under
it (llama-servers, sidecars), system RAM and commit, and used VRAM. At the end it prints per-process growth
(first vs last quarter) and turn latency/error counts. Not covered: voice (needs a mic) and governor eviction
(needs another app to grab VRAM mid-run) -- do those by hand while it runs.
"""
from __future__ import annotations

import argparse
import csv
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_real_chat as base  # noqa: E402  (Backend, Api, isolated_env, PORT)

REPO = base.REPO
STOP = threading.Event()

CHAT = [
    "Explain in three sentences how a hash map handles collisions.",
    "Give me a short plan for a weekend trip to a mountain town.",
    "What's the difference between a process and a thread?",
    "Write a haiku about a rainy Monday, then explain the imagery.",
    "Summarise the plot of a heist movie you make up, in a paragraph.",
    "List five tips for sleeping better and say which matters most.",
    "Why is the sky blue? Keep it brief.",
    "Rewrite this politely: 'send me the report now'.",
    "Compare TCP and UDP in a small table.",
    "Suggest names for a small coffee shop, with one line each.",
]
RAG = [
    "What does the governor do when another process grows VRAM?",
    "How does a chat turn flow through the backend?",
    "What is the prompt prefix contract and why does it matter?",
    "Which ports does Jarvis use and for what?",
    "How are confirmations for risky tools handled?",
]


def processes(backend_pid: int) -> dict[str, psutil.Process]:
    """The backend plus everything under it, keyed by a readable role."""
    try:
        root = psutil.Process(backend_pid)
        found = {"backend": root}
        for ch in root.children(recursive=True):
            try:
                name = ch.name().lower()
                cmd = " ".join(ch.cmdline())
                if "llama-server" in name:
                    role = "llama:" + (cmd.split("--port")[1].split()[0] if "--port" in cmd else str(ch.pid))
                else:
                    role = f"{name}:{ch.pid}"
                found[role] = ch
            except psutil.Error:
                continue
        return found
    except psutil.Error:
        return {}


def vram_mb() -> int:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.split()[0])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return -1


def sampler(backend_pid: int, out: Path, every: int) -> None:
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t", "role", "rss_mb", "private_mb", "sys_avail_mb", "commit_used_mb", "vram_used_mb"])
        start = time.time()
        while not STOP.is_set():
            vm, commit, vram = psutil.virtual_memory(), psutil.swap_memory(), vram_mb()
            for role, p in processes(backend_pid).items():
                try:
                    mem = p.memory_full_info()
                    w.writerow([round(time.time() - start), role, round(mem.rss / 2**20),
                                round(getattr(mem, "private", mem.uss) / 2**20), round(vm.available / 2**20),
                                round(commit.used / 2**20), vram])
                except psutil.Error:
                    continue
            f.flush()
            STOP.wait(every)


def make_project(api: "base.Api", home: Path) -> str:
    ws = home / "soak-project"
    ws.mkdir(parents=True, exist_ok=True)
    for name in ("CLAUDE.md", "PLAN.md"):
        shutil.copy(REPO / name, ws / name)
    return api.create_project("soak", ws)


def one_turn(api: "base.Api", body: dict, cancel: bool = False) -> tuple[float, str]:
    """Run one turn; returns (seconds, error text or empty)."""
    started = time.time()
    try:
        events = api.stream(body, stop_after=(lambda k: k in base.STARTED) if cancel else None)
    except httpx.HTTPError as e:
        return time.time() - started, f"{type(e).__name__}: {e}"
    err = next((str(e["data"]) for e in events if e["event"] == "error"), "")
    if not err and not events:
        err = "no events"
    return time.time() - started, err


def workload(api: "base.Api", project_id: str, deadline: float, log: list) -> None:
    rng, n = random.Random(7), 0
    long_session = f"soak-long-{int(time.time())}"  # never ephemeral: grows until compaction kicks in
    while time.time() < deadline and not STOP.is_set():
        n += 1
        roll = rng.random()
        if roll < 0.45:
            kind, body = "long-chat", {"message": f"({n}) " + rng.choice(CHAT) + " Go into some detail.",
                                       "session_id": long_session, "chat_mode": "FREEFORM"}
        elif roll < 0.65:
            kind, body = "ephemeral", {"message": rng.choice(CHAT), "session_id": f"soak-e-{n}",
                                       "chat_mode": "FREEFORM", "ephemeral": True}
        elif roll < 0.90:
            kind, body = "rag", {"message": rng.choice(RAG), "session_id": f"soak-r-{n % 6}",
                                 "chat_mode": "WORKSPACE", "project_id": project_id}
        else:
            kind, body = "cancel", {"message": "Write a long essay about the history of computing.",
                                    "session_id": f"soak-c-{n}", "chat_mode": "FREEFORM", "ephemeral": True}
        secs, err = one_turn(api, body, cancel=(kind == "cancel"))
        log.append((time.time(), kind, secs, err))
        if kind == "cancel":
            time.sleep(3)
        time.sleep(rng.uniform(1, 8))  # a person doesn't fire turns back to back


def growth(rows: list[list[str]]) -> list[str]:
    by_role: dict[str, list[int]] = {}
    for _, role, _, priv, *_ in rows:
        by_role.setdefault(role, []).append(int(priv))
    lines = []
    for role, pts in sorted(by_role.items()):
        if len(pts) < 8:
            continue
        q = max(1, len(pts) // 4)
        first, last = statistics.mean(pts[:q]), statistics.mean(pts[-q:])
        lines.append(f"  {role:<22} {first:7.0f} -> {last:7.0f} MB  ({last - first:+.0f}, peak {max(pts)})")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=3.0)
    ap.add_argument("--sample", type=int, default=30, help="seconds between RAM samples")
    args = ap.parse_args()
    if base._jarvis_running() or not base._port_free(base.PORT):
        print("Close Jarvis (and anything on port", base.PORT, ") first: the soak needs the GPU and its own backend.")
        return 2
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = REPO / "scripts" / "soak_results" / stamp
    out.mkdir(parents=True)
    home = Path(tempfile.mkdtemp(prefix="jarvis-soak-"))
    log: list = []
    with base.Backend(home, out / "backend.log", "off") as be:
        api = base.Api()
        sampler_t = threading.Thread(target=sampler, args=(be.proc.pid, out / "samples.csv", args.sample), daemon=True)
        sampler_t.start()
        try:
            print(f"Soak running for {args.hours} h; results in {out}. Ctrl+C to stop early.")
            workload(api, make_project(api, home), time.time() + args.hours * 3600, log)
        except KeyboardInterrupt:
            print("Stopped early.")
        finally:
            STOP.set()
            sampler_t.join(timeout=60)
    shutil.rmtree(home, ignore_errors=True)

    errs = [(k, e) for _, k, _, e in log if e]
    lines = [f"{len(log)} turns, {len(errs)} with an error."]
    for kind in ("long-chat", "ephemeral", "rag", "cancel"):
        secs = sorted(s for _, k, s, _ in log if k == kind)
        if secs:
            lines.append(f"  {kind:<10} n={len(secs):<4} p50 {secs[len(secs) // 2]:.1f}s  p95 {secs[int(len(secs) * .95)]:.1f}s")
    lines += [f"  error [{k}]: {e[:160]}" for k, e in errs[:10]]
    with (out / "samples.csv").open(encoding="utf-8") as f:
        rows = list(csv.reader(f))[1:]
    lines += ["Private memory by process, first vs last quarter of the run:"] + growth(rows)
    (out / "summary.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
