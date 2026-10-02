"""Laya voice-command bench: accuracy and latency of the voice-mode command brain (PLAN 4.9).

Runs in the Laya venv (torch lives there, not in the backend venv):

    $env:HF_HOME='D:\\JARVIS\\tools\\hf-cache'; $env:USE_TF='0'
    tools\\laya-venv\\Scripts\\python.exe scripts\\laya_bench\\bench.py --device cuda
    tools\\laya-venv\\Scripts\\python.exe scripts\\laya_bench\\bench.py --device cpu

The question set and candidate extraction are the backend's own (``app/voice/command_brain.py``),
so what this measures is what voice mode will run. Results go to ``scripts/laya_bench/results/``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

# By file path: the app.voice package __init__ pulls in the backend's settings (pydantic etc.),
# which this venv doesn't have; command_brain itself is stdlib-only.
_spec = importlib.util.spec_from_file_location(
    "command_brain", ROOT / "backend" / "app" / "voice" / "command_brain.py")
brain = importlib.util.module_from_spec(_spec)
sys.modules["command_brain"] = brain  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(brain)


def _norm(v) -> str:
    return " ".join(str(v).lower().replace("'", "").split()).strip(" .!?")


def _arg_ok(key: str, want, got) -> bool:
    if key == "when":  # "at 6pm" vs "6pm": the time is what matters
        w, g = _norm(want), _norm(got or "")
        return bool(g) and (w in g or g in w)
    if isinstance(want, bool) or isinstance(want, int):
        return want == got
    return _norm(want) == _norm(got if got is not None else "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    ap.add_argument("--model", default="convaiinnovations/laya")
    ap.add_argument("--subfolder", default=None, help="e.g. multilingual")
    ap.add_argument("--head", type=int, default=320, help="head_max_len (option budget)")
    ap.add_argument("--max-len", type=int, default=512)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    import torch
    import laya

    t0 = time.perf_counter()
    agent = laya.load(args.model, device=args.device, subfolder=args.subfolder)
    load_s = time.perf_counter() - t0

    def predict(state: dict, questions: dict) -> dict:
        return agent.system_one(state, questions, max_len=args.max_len, head_max_len=args.head)["answers"]

    apps = json.loads((HERE / "apps.json").read_text(encoding="utf-8"))
    cases = [json.loads(line) for line in (HERE / "commands.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]

    for _ in range(3):  # warm up kernels / allocator
        brain.evaluate("open chrome", predict, apps)
    if args.device == "cuda":
        torch.cuda.reset_peak_memory_stats()

    rows, s1, s2, tot = [], [], [], []
    action_ok = compound_ok = full_ok = 0
    arg_hits: dict[str, list[int]] = {}
    per_action: dict[str, list[int]] = {}
    for c in cases:
        t = time.perf_counter()
        plan = brain.evaluate(c["u"], predict, apps)
        total_ms = (time.perf_counter() - t) * 1000
        s1.append(plan.latency_ms.get("stage1", 0))
        if "stage2" in plan.latency_ms:
            s2.append(plan.latency_ms["stage2"])
        tot.append(total_ms)

        a_ok = plan.action == c["action"]
        c_ok = plan.compound == bool(c.get("compound", False))
        args_ok = True
        for k, want in c["args"].items():
            ok = a_ok and _arg_ok(k, want, plan.args.get(k))
            arg_hits.setdefault(f"{c['action']}.{k}", []).append(int(ok))
            args_ok &= ok
        action_ok += a_ok
        compound_ok += c_ok
        full_ok += a_ok and args_ok
        per_action.setdefault(c["action"], []).append(int(a_ok and args_ok))
        rows.append({
            "u": c["u"], "want_action": c["action"], "got_action": plan.action,
            "want_args": c["args"], "got_args": plan.args, "compound": plan.compound,
            "confidence": round(plan.confidence, 3), "ok": bool(a_ok and args_ok),
            "action_probs": plan.answers["action"].get("probabilities"),
            "ms": plan.latency_ms,
        })

    n = len(cases)

    def pct(xs, p):
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1) if xs else None

    summary = {
        "device": args.device, "model": args.model, "subfolder": args.subfolder,
        "head_max_len": args.head, "max_len": args.max_len, "cases": n,
        "load_s": round(load_s, 1),
        "action_acc": round(action_ok / n, 3),
        "full_acc": round(full_ok / n, 3),
        "compound_acc": round(compound_ok / n, 3),
        "per_action_full": {a: f"{sum(v)}/{len(v)}" for a, v in sorted(per_action.items())},
        "per_arg": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(arg_hits.items())},
        "ms_stage1_p50": pct(s1, 0.5), "ms_stage1_p90": pct(s1, 0.9),
        "ms_stage2_p50": pct(s2, 0.5), "ms_stage2_p90": pct(s2, 0.9),
        "ms_total_p50": pct(tot, 0.5), "ms_total_p90": pct(tot, 0.9),
        "ms_total_mean": round(statistics.mean(tot), 1),
    }
    if args.device == "cuda":
        summary["vram_peak_mib"] = round(torch.cuda.max_memory_allocated() / 2**20)

    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{args.device}{('-' + args.tag) if args.tag else ''}.json"
    (out_dir / name).write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")

    print(json.dumps(summary, indent=1))
    print("\nMisses:")
    for r in rows:
        if not r["ok"]:
            print(f"  {r['u']!r}: want {r['want_action']}{r['want_args']} got {r['got_action']}{r['got_args']}"
                  f" conf={r['confidence']}")
    print(f"\n-> {out_dir / name}")


if __name__ == "__main__":
    os.environ.setdefault("USE_TF", "0")
    main()
