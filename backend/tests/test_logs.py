"""Settings -> Developer -> Logs (PLAN 4.17)."""

import io

import httpx
import pytest

from app import logs
from app.agent.runtime_process_manager import RuntimeProcessManager

BACKEND_SAMPLE = [
    "2026-10-19 11:53:46,960 [INFO] httpx: HTTP Request: GET http://127.0.0.1:8001/v1/models \"HTTP/1.1 200 OK\"",
    "INFO:     127.0.0.1:52883 - \"GET /health HTTP/1.1\" 200 OK",
    "2026-10-19 11:53:47,091 [INFO] httpx: HTTP Request: GET http://127.0.0.1:8001/slots \"HTTP/1.1 200 OK\"",
    "2026-10-19 11:53:48,000 [INFO] jarvis: Routing decision: mode='main'",
    "2026-10-19 11:53:49,000 [WARNING] jarvis.google: Calendar sync failed: offline",
    "2026-10-19 11:53:50,000 [ERROR] jarvis: Unexpected error in chat endpoint",
    "Traceback (most recent call last):",
    '  File "main.py", line 1, in chat',
    "RuntimeError: boom",
    "INFO:     127.0.0.1:52884 - \"POST /chat/stream HTTP/1.1\" 200 OK",
    "2026-10-19 11:53:51,000 [INFO] jarvis: Turn finished",
]


def test_quiet_drops_the_polling_noise():
    kept = logs.filter_lines(BACKEND_SAMPLE)
    assert not any("/health" in l or "/slots" in l or "/v1/models" in l for l in kept)
    assert any("Routing decision" in l for l in kept) and any("POST /chat/stream" in l for l in kept)
    assert logs.filter_lines(BACKEND_SAMPLE, quiet=False) == BACKEND_SAMPLE


def test_warning_level_keeps_tracebacks_with_their_error():
    kept = logs.filter_lines(BACKEND_SAMPLE, level="warning")
    assert kept == BACKEND_SAMPLE[4:9]


def test_search_is_case_insensitive():
    assert logs.filter_lines(BACKEND_SAMPLE, search="CALENDAR") == [BACKEND_SAMPLE[4]]


def test_tail_reads_only_the_end(tmp_path):
    path = tmp_path / "backend.log"
    path.write_text("".join(f"line {i:05d}\n" for i in range(5000)), encoding="utf-8")
    tail = logs.tail_file(path, max_bytes=1000)
    assert tail[-1] == "line 04999"
    assert all(l.startswith("line ") and len(l) == 10 for l in tail)  # no partial first line
    assert len(tail) < 100
    assert logs.tail_file(tmp_path / "missing.log") == []


def test_read_logs_backend_caps_and_reports(tmp_path, monkeypatch):
    path = tmp_path / "backend.log"
    path.write_text("\n".join(BACKEND_SAMPLE) + "\n", encoding="utf-8")
    monkeypatch.setattr(logs, "BACKEND_LOG", path)
    out = logs.read_logs("backend", lines=2)
    assert out["path"] == str(path) and out["lines"] == BACKEND_SAMPLE[-2:]
    assert out["total_matched"] == 8
    with pytest.raises(ValueError):
        logs.read_logs("nonsense")


def test_model_output_is_stamped_and_skips_poll_requests(monkeypatch):
    pm = RuntimeProcessManager()
    raw = (b"srv  log_server_r: request: GET /slots 127.0.0.1 200\n"
           b"main: model loaded\n"
           b"srv  log_server_r: request: GET /health 127.0.0.1 200\n"
           b"slot print_timing: id 0 | prompt eval time = 120 ms\n")
    pm._drain_sync_stream(io.BytesIO(raw), "stderr")
    got = list(pm._recent_output)
    assert [l[9:] for l in got] == ["[stderr] main: model loaded",
                                    "[stderr] slot print_timing: id 0 | prompt eval time = 120 ms"]
    assert all(l[2] == ":" and l[5] == ":" for l in got)

    import app.agent.runtime_process_manager as rpm
    monkeypatch.setattr(rpm, "_process_manager_instance", pm)
    assert logs.read_logs("model")["lines"] == got


def test_sidecar_lines_are_labelled_and_merged_by_time(monkeypatch):
    from app.agent.captioner import get_image_captioner
    from app.rag.embedding_sidecar import get_embedding_sidecar
    from app.voice.laya_sidecar import get_laya_sidecar

    monkeypatch.setattr(get_image_captioner(), "_recent_output", ["10:00:02 caption ready"])
    monkeypatch.setattr(get_embedding_sidecar(), "_recent_output", ["10:00:01 embed up", "10:00:03 embed done"])
    monkeypatch.setattr(get_laya_sidecar(), "_recent", ["09:59:59 laya up"])
    assert logs.read_logs("sidecars")["lines"] == [
        "09:59:59 [laya] laya up", "10:00:01 [embedding] embed up",
        "10:00:02 [captioner] caption ready", "10:00:03 [embedding] embed done",
    ]


@pytest.mark.asyncio
async def test_endpoint(tmp_path, monkeypatch):
    from app.main import app

    path = tmp_path / "backend.log"
    path.write_text("\n".join(BACKEND_SAMPLE) + "\n", encoding="utf-8")
    monkeypatch.setattr(logs, "BACKEND_LOG", path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/logs", params={"source": "backend", "level": "warning"})
        assert res.status_code == 200 and res.json()["lines"] == BACKEND_SAMPLE[4:9]
        res = await ac.get("/api/logs", params={"source": "bogus"})
        assert res.status_code == 400
