"""Bark sidecar server: speech to text (Underdog Bark 3.0 Pro, a Parakeet-TDT fine-tune). Runs in
the Laya venv, never the backend's (torch stays out of that one).

    tools\\laya-venv\\Scripts\\python.exe scripts\\bark_server.py --model tools\\bark-pro --port 8006

Started and stopped by ``backend/app/voice/bark_sidecar.py``. Loopback only, one request at a time:

    GET  /health      -> {"ok": true, "device": ..., "vram_mib": ...}
    POST /transcribe  body = raw little-endian float32 mono 16 kHz samples
                      -> {"text": ..., "ms": 340}

The backend decodes the audio (webm/mp4/wav) itself, so this needs no ffmpeg.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

SAMPLE_RATE = 16000
# The model card's runners prepend 0.25 s of silence; without it the first word's onset is clipped.
LEAD_SILENCE = 4000
# One pass is fine for a spoken command. Longer recordings (a phone message) are cut into
# windows at the quietest point near the edge so a word is not split.
WINDOW = 30 * SAMPLE_RATE
SEARCH = 2 * SAMPLE_RATE


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, default=8006)
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    args = ap.parse_args()

    import numpy as np
    import torch
    from transformers import AutoProcessor, ParakeetForTDT

    t0 = time.perf_counter()
    dtype = torch.bfloat16 if args.device == "cuda" else torch.float32
    processor = AutoProcessor.from_pretrained(args.model)
    model = ParakeetForTDT.from_pretrained(args.model, dtype=dtype).to(args.device).eval()
    lock = threading.Lock()

    def run(wave: "np.ndarray") -> str:
        wave = np.concatenate([np.zeros(LEAD_SILENCE, np.float32), wave])
        inputs = processor(wave, sampling_rate=SAMPLE_RATE, return_tensors="pt").to(args.device, dtype)
        with torch.inference_mode():
            out = model.generate(**inputs, max_new_tokens=512)
        ids = out.sequences if hasattr(out, "sequences") else out
        return processor.batch_decode(ids, skip_special_tokens=True)[0].strip()

    def transcribe(wave: "np.ndarray") -> str:
        if wave.size < SAMPLE_RATE // 10:
            return ""
        parts, start = [], 0
        while start < wave.size:
            end = min(start + WINDOW, wave.size)
            if wave.size - end > 0:
                lo = max(start, end - SEARCH)
                frames = np.abs(wave[lo:end])[: (end - lo) // 160 * 160].reshape(-1, 160).mean(axis=1)
                end = lo + int(frames.argmin()) * 160 + 80 if frames.size else end
            parts.append(run(wave[start:end]))
            start = end
        return " ".join(p for p in parts if p)

    with lock:  # warm-up: first call pays kernel selection
        run(np.zeros(SAMPLE_RATE, np.float32))
    vram = round(torch.cuda.memory_reserved() / 2**20) if args.device == "cuda" else 0
    print(f"bark ready in {time.perf_counter() - t0:.1f}s on {args.device}, {vram} MiB reserved", flush=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
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
                reserved = round(torch.cuda.memory_reserved() / 2**20) if args.device == "cuda" else 0
                self._send(200, {"ok": True, "device": args.device, "vram_mib": reserved})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/transcribe":
                self._send(404, {"error": "not found"})
                return
            try:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                wave = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<f4").astype(np.float32)
                started = time.perf_counter()
                with lock:
                    text = transcribe(wave)
                self._send(200, {"text": text, "ms": round((time.perf_counter() - started) * 1000)})
            except Exception as e:  # the backend falls back to Whisper on any error
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
