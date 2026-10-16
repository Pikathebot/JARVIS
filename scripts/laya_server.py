"""Laya sidecar server for voice mode (PLAN 4.9). Runs in the Laya venv, never the backend's.

    tools\\laya-venv\\Scripts\\python.exe scripts\\laya_server.py --model tools\\laya-voice --port 8004

Started and stopped by ``backend/app/voice/laya_sidecar.py``. Loopback only, one request at a
time (a single model on one GPU gains nothing from concurrency):

    GET  /health   -> {"ok": true, "model": ..., "device": ..., "vram_mib": ...}
    POST /predict  {"state": {...}, "questions": {...}, "max_len": 512, "head_max_len": 320}
                   -> {"answers": {...}, "ms": 41}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("USE_TF", "0")  # transformers probing TensorFlow can deadlock model construction
os.environ.setdefault("HF_HUB_OFFLINE", "1")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, default=8004)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--fp16", action="store_true", help="keep the weights in half precision")
    args = ap.parse_args()

    import torch
    import laya

    t0 = time.perf_counter()
    agent = laya.load(args.model, device=args.device)
    if args.fp16 and args.device == "cuda":
        agent.model.half()
        torch.cuda.empty_cache()  # else the allocator keeps the fp32 copy's blocks reserved
    lock = threading.Lock()
    with lock:  # warm-up: first call pays kernel selection
        agent.system_one({"utterance": "open chrome"},
                         {"a": {"type": "choice", "instructions": "x", "criteria": {"A": "a", "B": "b"}}})
    vram = round(torch.cuda.memory_reserved() / 2**20) if args.device == "cuda" else 0
    print(f"laya ready in {time.perf_counter() - t0:.1f}s on {args.device}, {vram} MiB reserved", flush=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):  # quiet: the backend logs what matters
            pass

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True, "model": args.model, "device": args.device, "vram_mib": vram})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/predict":
                self._send(404, {"error": "not found"})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                t = time.perf_counter()
                with lock:
                    res = agent.system_one(body["state"], body["questions"],
                                           max_len=body.get("max_len"), head_max_len=body.get("head_max_len"))
                self._send(200, {"answers": res["answers"], "ms": round((time.perf_counter() - t) * 1000)})
            except Exception as e:  # a bad question must not kill the sidecar
                self._send(400, {"error": f"{type(e).__name__}: {e}"})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
