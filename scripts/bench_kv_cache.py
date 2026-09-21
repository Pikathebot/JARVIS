"""Measure what a llama-server launch actually costs on the GPU, and what a KV-cache type costs in quality.

Two sub-commands, both driven by the real binaries in ``tools/llama-cpp`` so the numbers are the
ones Jarvis will see, not estimates:

``vram``  launches llama-server with the given model / context / KV type, parses the per-buffer
          allocation lines it prints (model, KV, recurrent-state, compute), records the pynvml VRAM
          delta against the idle desktop, then runs one generation to report prompt-processing and
          decode throughput. Repeat with ``--kv f16 / q8_0 / q4_0`` or ``--ctx`` to see the deltas.

``ppl``   runs llama-perplexity over a text file for each KV type and prints them side by side.
          Perplexity is the cheapest "did this change degrade the model" test there is: an
          identical PPL across KV types on the same text means the compression is lossless for
          that model, a rising one means it is not.

Examples (from the repo root, venv python)::

    python scripts/bench_kv_cache.py vram --model models/qwen3.5-9b/Qwen3.5-9B-UD-Q3_K_XL.gguf --ctx 16384 --kv q8_0
    python scripts/bench_kv_cache.py vram --model models/qwen3.5-9b/Qwen3.5-9B-UD-Q3_K_XL.gguf --ctx 16384 --kv q8_0 --no-mmproj
    python scripts/bench_kv_cache.py ppl  --model models/qwen3.5-4b/Qwen3.5-4B-UD-Q4_K_XL.gguf --text corpus.txt --kv f16 q8_0 q4_0

The script is deliberately standalone (stdlib + httpx + pynvml, both already in the venv) so it can
be run with the backend stopped; it uses port 8011 so it never collides with the live server on 8001.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "tools" / "llama-cpp" / "llama-server.exe"
PERPLEXITY = REPO / "tools" / "llama-cpp" / "llama-perplexity.exe"
PORT = 8011

# One line per backend buffer, e.g. "load_tensors: CUDA0 model buffer size = 4813.34 MiB",
# "llama_kv_cache: CUDA0 KV buffer size = 272.00 MiB", "llama_memory_recurrent: CUDA0 RS buffer size = ..."
BUFFER_RE = re.compile(r"(\w+):\s+(\w+)\s+(model|KV|RS|compute|output)\s+buffer size\s*=\s*([\d.]+)\s*MiB")


# --------------------------------------------------------------------------------------------
# GPU telemetry
# --------------------------------------------------------------------------------------------

def gpu_used_mib() -> float:
    import pynvml

    pynvml.nvmlInit()
    try:
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return pynvml.nvmlDeviceGetMemoryInfo(h).used / (1024 * 1024)
    finally:
        pynvml.nvmlShutdown()


# --------------------------------------------------------------------------------------------
# Common argv
# --------------------------------------------------------------------------------------------

def projector_for(model: Path) -> Path | None:
    # Smallest wins, matching ModelCatalog.projector_for (an F16 and a Q8_0 side by side).
    cands = sorted(model.parent.glob("mmproj*.gguf"), key=lambda c: c.stat().st_size)
    return cands[0] if cands else None


def model_args(a: argparse.Namespace, kv: str) -> list[str]:
    args = [
        "-m", str(a.model),
        "-c", str(a.ctx),
        "-ngl", "99",
        "-fa", "on",
        "-ctk", kv, "-ctv", kv,
        "-b", str(a.batch), "-ub", str(a.ubatch),
        "--no-mmap",
    ]
    return args


# --------------------------------------------------------------------------------------------
# vram
# --------------------------------------------------------------------------------------------

def cmd_vram(a: argparse.Namespace) -> None:
    idle = gpu_used_mib()
    cmd = [str(SERVER), *model_args(a, a.kv), "--host", "127.0.0.1", "--port", str(PORT),
           "--parallel", str(a.parallel), "--fit", "off", "--verbose"]
    proj = None if a.no_mmproj else projector_for(a.model)
    if proj:
        cmd += ["--mmproj", str(proj)]
        if a.mmproj_cpu:
            cmd.append("--no-mmproj-offload")
    if a.image_max_tokens:
        cmd += ["--image-max-tokens", str(a.image_max_tokens)]

    print(f"idle VRAM before launch: {idle:.0f} MiB")
    print("launch:", " ".join(cmd[1:]))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    buffers: dict[str, float] = {}
    log_lines: list[str] = []
    t0 = time.time()
    ready = False
    try:
        # Read the log until the server reports it is listening; buffer lines all appear before that.
        assert proc.stdout is not None
        for line in proc.stdout:
            log_lines.append(line.rstrip())
            m = BUFFER_RE.search(line)
            if m:
                # e.g. "load_tensors: CUDA0 model buffer size", "clip_model_loader: CUDA0 model buffer size"
                key = f"{m.group(2)} {m.group(3)} ({m.group(1)})"
                buffers[key] = buffers.get(key, 0.0) + float(m.group(4))
            if "model loaded" in line:
                ready = True
                break
            if proc.poll() is not None:
                break
        if not ready:
            print("\n".join(log_lines[-40:]))
            raise SystemExit(f"llama-server exited early with code {proc.returncode}")
        load_s = time.time() - t0
        # Keep draining the log so the child never blocks on a full pipe while we talk to it.
        threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()

        # Give the driver a moment to settle, then measure the real delta.
        time.sleep(1.5)
        loaded = gpu_used_mib()

        # Wait for /health to report ready (model fully loaded), then run a timed generation.
        with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=600) as c:
            for _ in range(120):
                try:
                    if c.get("/health").json().get("status") == "ok":
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            prompt = ("Summarise the following in one paragraph.\n\n" + (a.prompt_text or "The quick brown fox jumps over the lazy dog. ") * a.prompt_repeat)
            r = c.post("/completion", json={
                "prompt": prompt, "n_predict": a.n_predict, "temperature": 0, "cache_prompt": False,
            })
            r.raise_for_status()
            timings = r.json().get("timings", {})
        peak = gpu_used_mib()
    finally:
        proc.kill()
        proc.wait(timeout=30)

    print()
    print(f"{'buffer':32s} {'MiB':>9s}")
    for k, v in sorted(buffers.items()):
        print(f"  {k:30s} {v:9.1f}")
    gpu_total = sum(v for k, v in buffers.items() if k.startswith("CUDA0"))
    print(f"  {'sum of CUDA buffers':30s} {gpu_total:9.1f}")
    print()
    print(f"VRAM delta vs idle  : after load {loaded - idle:8.0f} MiB   after generation {peak - idle:8.0f} MiB")
    print(f"  (the difference from the buffer sum is the CUDA context + driver overhead)")
    print(f"load time           : {load_s:.1f} s")
    if timings:
        print(f"prompt processing   : {timings.get('prompt_per_second', 0):8.1f} tok/s  ({timings.get('prompt_n')} tokens)")
        print(f"decode              : {timings.get('predicted_per_second', 0):8.1f} tok/s  ({timings.get('predicted_n')} tokens)")

    if a.json:
        Path(a.json).write_text(json.dumps({
            "model": str(a.model), "ctx": a.ctx, "kv": a.kv, "ubatch": a.ubatch, "mmproj": str(proj) if proj else None,
            "buffers_mib": buffers, "vram_delta_loaded_mib": loaded - idle, "vram_delta_peak_mib": peak - idle,
            "load_s": load_s, "timings": timings,
        }, indent=2))


# --------------------------------------------------------------------------------------------
# ppl
# --------------------------------------------------------------------------------------------

PPL_RE = re.compile(r"Final estimate: PPL = ([\d.]+) \+/- ([\d.]+)")


def cmd_ppl(a: argparse.Namespace) -> None:
    results: dict[str, tuple[float, float, float]] = {}
    for kv in a.kv:
        cmd = [str(PERPLEXITY), *model_args(a, kv), "-f", str(a.text), "--chunks", str(a.chunks)]
        print("run:", " ".join(cmd[1:]))
        t0 = time.time()
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = PPL_RE.search(out.stdout + out.stderr)
        if not m:
            print(out.stderr[-2000:])
            raise SystemExit(f"no PPL line for kv={kv} (exit {out.returncode})")
        results[kv] = (float(m.group(1)), float(m.group(2)), time.time() - t0)
        print(f"  {kv}: PPL {m.group(1)} +/- {m.group(2)}  ({results[kv][2]:.0f}s)")

    base = results[a.kv[0]][0]
    print()
    print(f"{a.model.name}  ctx={a.ctx}  chunks={a.chunks}  ({a.ctx * a.chunks} tokens scored)")
    print(f"{'KV type':10s} {'PPL':>9s} {'+/-':>7s} {'vs ' + a.kv[0]:>10s}")
    for kv, (ppl, err, _) in results.items():
        print(f"{kv:10s} {ppl:9.4f} {err:7.4f} {100 * (ppl - base) / base:+9.2f}%")


# --------------------------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--model", type=Path, required=True)
        sp.add_argument("--ctx", type=int, default=16384)
        sp.add_argument("--batch", type=int, default=2048)
        sp.add_argument("--ubatch", type=int, default=512)

    v = sub.add_parser("vram", help="launch llama-server, report buffers, VRAM delta, throughput")
    common(v)
    v.add_argument("--kv", default="q8_0", help="KV cache type for K and V (f16, q8_0, q4_0, ...)")
    v.add_argument("--parallel", type=int, default=1)
    v.add_argument("--no-mmproj", action="store_true", help="text-only launch even if a projector sits beside the model")
    v.add_argument("--mmproj-cpu", action="store_true", help="keep the projector in system RAM (--no-mmproj-offload)")
    v.add_argument("--image-max-tokens", type=int, default=0)
    v.add_argument("--n-predict", type=int, default=128)
    v.add_argument("--prompt-text", default="")
    v.add_argument("--prompt-repeat", type=int, default=200, help="how many times to repeat the prompt text (~2k tokens default)")
    v.add_argument("--json", help="also write the result to this path")

    q = sub.add_parser("ppl", help="llama-perplexity across KV types")
    common(q)
    q.add_argument("--text", type=Path, required=True)
    q.add_argument("--kv", nargs="+", default=["f16", "q8_0", "q4_0"])
    q.add_argument("--chunks", type=int, default=8)

    a = p.parse_args()
    a.model = (REPO / a.model).resolve() if not a.model.is_absolute() else a.model
    if not a.model.exists():
        sys.exit(f"model not found: {a.model}")
    {"vram": cmd_vram, "ppl": cmd_ppl}[a.cmd](a)


if __name__ == "__main__":
    main()
