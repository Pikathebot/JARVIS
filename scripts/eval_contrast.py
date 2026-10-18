"""Where do two eval runs of the same task part ways? (PLAN 4.15 #1, after harness2's contrastive
Propose-Probe-Compose: compare executions of the *same* task instead of reading one in isolation.)

    python scripts/eval_contrast.py A.json             # within A: flaky tasks, pass vs fail seed
    python scripts/eval_contrast.py A.json B.json      # also: what flipped between A and B

Stdlib only. Needs the runs' traces (``eval_jarvis_tasks.py --trace DIR``; the result file's meta
remembers the folder) for the divergence part; flips work from the result files alone.

For a task that passed on some seeds and failed on others, it lines up a passing and a failing
run step by step (pre-run calls, each tool call with its arguments, safety nets, the answer) and
prints the first step where they differ, with the failing run's answer. That step is where a fix
-- harness or fine-tune -- should aim.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_io  # noqa: E402


def load_traces(meta: dict[str, Any], override: Path | None) -> dict[tuple[str, int], dict[str, Any]]:
    folder = override or (Path(meta["trace_dir"]) if meta.get("trace_dir") else None)
    if folder is None or not folder.is_dir():
        return {}
    out = {}
    for f in folder.glob("*.r*.json"):
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
            out[(t["task"], int(t["rep"]))] = t
        except (OSError, ValueError, KeyError):
            continue
    return out


def _short(args: Any, limit: int = 90) -> str:
    text = json.dumps(args, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def steps(trace: dict[str, Any]) -> list[tuple[str, str]]:
    """A run as (kind, detail) steps; kind is what must match for two runs to still agree."""
    out: list[tuple[str, str]] = []
    for ti, turn in enumerate(trace.get("turns") or []):
        for p in turn.get("pre") or []:
            out.append((f"t{ti} pre {p['name']}", _short(p.get("args"))))
        for rd in turn.get("rounds") or []:
            for c in rd.get("calls") or []:
                out.append((f"t{ti} call {c['name']}", _short(c.get("args"))))
            if rd.get("net"):
                out.append((f"t{ti} net {rd['net']}", ""))
            elif not rd.get("calls"):
                out.append((f"t{ti} answer", (rd.get("content") or "")[:120].replace("\n", " ")))
    return out


def divergence(passed: dict[str, Any], failed: dict[str, Any]) -> str:
    a, b = steps(passed), steps(failed)
    for i in range(max(len(a), len(b))):
        sa = a[i] if i < len(a) else ("(ended)", "")
        sb = b[i] if i < len(b) else ("(ended)", "")
        if sa == sb:
            continue
        how = "same tool, other arguments" if sa[0] == sb[0] else "different step"
        return (f"step {i + 1} ({how}):\n"
                f"        pass r{passed['rep']}: {sa[0]} {sa[1]}\n"
                f"        fail r{failed['rep']}: {sb[0]} {sb[1]}")
    return "no step differs: same calls and answers' openings (the check decided on the answer's wording)"


def final_of(trace: dict[str, Any]) -> str:
    turns = trace.get("turns") or []
    return (turns[-1].get("final") or "")[:200].replace("\n", " ") if turns else ""


def by_task(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, int] = {}
    for r in rows:
        rep = r.get("rep", seen.get(r["id"], 0))  # files before 2026-10-18: n-th row = seed n
        seen[r["id"]] = seen.get(r["id"], 0) + 1
        out.setdefault(r["id"], []).append({**r, "rep": rep})
    return out


def flaky_report(name: str, rows: list[dict[str, Any]], traces: dict[tuple[str, int], dict[str, Any]]) -> None:
    tasks = by_task(rows)
    flaky = {t: rs for t, rs in tasks.items()
             if any(r.get("success") is True for r in rs) and any(r.get("success") is False for r in rs)}
    print(f"\n== {name}: {len(flaky)} task(s) passed on some seeds and failed on others")
    for t, rs in sorted(flaky.items()):
        p = next(r for r in rs if r.get("success") is True)
        f = next(r for r in rs if r.get("success") is False)
        print(f"  {t} ({rs[0].get('group', '')}): passed {sum(r.get('success') is True for r in rs)}/{len(rs)}, "
              f"failed layer {f.get('layer') or '?'}")
        tp, tf = traces.get((t, p["rep"])), traces.get((t, f["rep"]))
        if tp and tf:
            print("      " + divergence(tp, tf))
            print(f"      fail answer: {final_of(tf)!r}")
        else:
            print(f"      calls pass r{p['rep']}: {' > '.join(p.get('calls') or []) or '-'}")
            print(f"      calls fail r{f['rep']}: {' > '.join(f.get('calls') or []) or '-'}  (no traces: run with --trace)")


def flips_report(rows_a: list[dict[str, Any]], rows_b: list[dict[str, Any]],
                 traces_a: dict, traces_b: dict) -> None:
    a = {(t, r["rep"]): r for t, rs in by_task(rows_a).items() for r in rs}
    b = {(t, r["rep"]): r for t, rs in by_task(rows_b).items() for r in rs}
    common = sorted(set(a) & set(b))
    flips = [(k, a[k], b[k]) for k in common
             if a[k].get("success") is not None and b[k].get("success") is not None
             and a[k].get("success") != b[k].get("success")]
    up = sum(1 for _, ra, rb in flips if rb.get("success"))
    print(f"\n== A -> B: {len(common)} runs in both, {up} fail->pass, {len(flips) - up} pass->fail")
    for (t, rep), ra, rb in flips:
        arrow = "fail->PASS" if rb.get("success") else "PASS->fail"
        print(f"  {arrow}  {t} r{rep} ({ra.get('group', '')})")
        passed, failed = (traces_b.get((t, rep)), traces_a.get((t, rep))) if rb.get("success") else \
                         (traces_a.get((t, rep)), traces_b.get((t, rep)))
        if passed and failed:
            print("      " + divergence(passed, failed))
        else:
            print(f"      A: {' > '.join(ra.get('calls') or []) or '-'}   B: {' > '.join(rb.get('calls') or []) or '-'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a", type=Path)
    ap.add_argument("b", type=Path, nargs="?")
    ap.add_argument("--traces-a", type=Path, help="trace folder for A (default: the one its meta names)")
    ap.add_argument("--traces-b", type=Path)
    args = ap.parse_args()

    meta_a, rows_a = eval_io.load_results(args.a)
    traces_a = load_traces(meta_a, args.traces_a)
    print(f"A = {args.a.name}: {len(rows_a)} runs, {len(traces_a)} traces")
    flaky_report("A", rows_a, traces_a)
    if args.b:
        meta_b, rows_b = eval_io.load_results(args.b)
        traces_b = load_traces(meta_b, args.traces_b)
        print(f"\nB = {args.b.name}: {len(rows_b)} runs, {len(traces_b)} traces")
        differs = eval_io.compare_meta(meta_a, meta_b)
        if differs:
            print("A and B differ in: " + "; ".join(differs))
            print("  (a verdict on one change needs the runs to differ only in that change: PLAN 4.15 #3)")
        flaky_report("B", rows_b, traces_b)
        flips_report(rows_a, rows_b, traces_a, traces_b)


if __name__ == "__main__":
    main()
