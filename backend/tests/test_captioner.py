"""
Image captioner sidecar: images become descriptions for models that cannot see, the sidecar
is left alone by the llama-server sweep, and it never shows up as a chat model.
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.agent import captioner as mod
from app.agent.captioner import ImageCaptioner, is_image_attachment
from app.agent.model_catalog import ModelCatalog
from app.config import settings

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da"
    "6364f8cfc00000030101009cd0f9a10000000049454e44ae426082"
)


class FakeLlamaServer(BaseHTTPRequestHandler):
    """Just enough of llama-server's OpenAI-compatible surface to answer one caption."""

    seen: list[dict] = []

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLlamaServer.seen.append(body)
        payload = {"choices": [{"message": {"content": "A red circle on a blue background with the word JARVIS."}}]}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_server():
    FakeLlamaServer.seen = []
    server = HTTPServer(("127.0.0.1", 0), FakeLlamaServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def captioner(tmp_path, fake_server, monkeypatch):
    model = tmp_path / "captioner" / "SmolVLM.gguf"
    model.parent.mkdir()
    model.write_bytes(b"gguf")
    (model.parent / "mmproj-SmolVLM.gguf").write_bytes(b"gguf")
    monkeypatch.setattr(settings, "captioner_enabled", True)
    c = ImageCaptioner(model_path=str(model), port=fake_server, idle_seconds=0)

    # The fake server is already "running": pretend the process is up.
    class Proc:
        pid = 424242

        def poll(self):
            return None

    c._process = Proc()
    return c


@pytest.fixture
def image(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(PNG)
    return p


def test_projector_is_found_beside_the_model(tmp_path):
    model = tmp_path / "m.gguf"
    model.write_bytes(b"x")
    (tmp_path / "mmproj-m.gguf").write_bytes(b"x")

    assert ImageCaptioner(model_path=str(model)).mmproj_path == tmp_path / "mmproj-m.gguf"
    assert ImageCaptioner(model_path=str(tmp_path / "nope" / "m.gguf")).mmproj_path is None


def test_unavailable_without_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "captioner_enabled", True)
    assert ImageCaptioner(model_path=str(tmp_path / "missing.gguf")).available is False


def test_disabled_by_setting(captioner, monkeypatch):
    monkeypatch.setattr(settings, "captioner_enabled", False)
    assert captioner.available is False


def test_is_image_attachment_by_extension():
    assert is_image_attachment({"filename": "a.PNG"})
    assert is_image_attachment({"path": "x/y.webp"})
    assert not is_image_attachment({"filename": "notes.md"})


@pytest.mark.asyncio
async def test_describe_sends_the_image_as_a_data_uri(captioner, image):
    text = await captioner.describe(image)

    assert text == "A red circle on a blue background with the word JARVIS."
    body = FakeLlamaServer.seen[0]
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_annotate_turns_images_into_attachment_text_and_leaves_the_rest(captioner, image, tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("hi")
    attachments = [
        {"id": "1", "filename": "shot.png", "path": str(image)},
        {"id": "2", "filename": "notes.md", "path": str(notes)},
    ]

    out = await captioner.annotate(attachments)

    assert out[1] == attachments[1]
    assert out[0]["path"] == str(image)
    assert out[0]["content"].startswith("Image contents (as seen by a vision model")
    assert "A red circle on a blue background with the word JARVIS." in out[0]["content"]


@pytest.mark.asyncio
async def test_annotate_keeps_an_image_it_could_not_describe(captioner, tmp_path):
    attachments = [{"id": "1", "filename": "gone.png", "path": str(tmp_path / "gone.png")}]

    assert await captioner.annotate(attachments) == attachments


@pytest.mark.asyncio
async def test_annotate_is_a_no_op_when_unavailable(captioner, image, monkeypatch):
    monkeypatch.setattr(settings, "captioner_enabled", False)
    attachments = [{"id": "1", "filename": "shot.png", "path": str(image)}]

    assert await captioner.annotate(attachments) is attachments
    assert FakeLlamaServer.seen == []


@pytest.mark.asyncio
async def test_annotate_caps_images_per_turn(captioner, image):
    captioner.max_images_per_turn = 1
    attachments = [{"filename": "a.png", "path": str(image)}, {"filename": "b.png", "path": str(image)}]

    out = await captioner.annotate(attachments)

    assert "content" in out[0] and "content" not in out[1]
    assert len(FakeLlamaServer.seen) == 1


def test_context_manager_prefers_the_description_over_the_path(memory_test_env_or_none=None):
    from app.memory.context_manager import ContextManager

    cm = ContextManager.__new__(ContextManager)
    fname, content = cm._read_attachment_content(
        {"filename": "shot.png", "path": "C:/x/shot.png", "content": "[Image described]\nA circle."}
    )
    assert fname == "shot.png" and content.endswith("A circle.")


def test_sweep_leaves_protected_pids_alone(monkeypatch):
    from app.agent import runtime_process_manager as rpm

    class P:
        def __init__(self, pid):
            self.pid = pid
            self.info = {"pid": pid, "name": "llama-server.exe"}
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            pass

    procs = [P(1), P(2)]
    monkeypatch.setattr(rpm.psutil if hasattr(rpm, "psutil") else __import__("psutil"), "process_iter", lambda attrs=None: procs)
    rpm.PROTECTED_PIDS.add(2)
    try:
        manager = rpm.RuntimeProcessManager()
        asyncio.run(manager._stop_internal(sweep_all=True))
    finally:
        rpm.PROTECTED_PIDS.discard(2)

    assert [p.terminated for p in procs] == [True, False]


def test_catalogue_hides_the_captioner_folder(tmp_path):
    (tmp_path / "qwen").mkdir()
    (tmp_path / "qwen" / "Qwen3.5-4B.gguf").write_bytes(b"x")
    (tmp_path / "captioner").mkdir()
    (tmp_path / "captioner" / "SmolVLM.gguf").write_bytes(b"x")
    (tmp_path / "captioner" / "mmproj-SmolVLM.gguf").write_bytes(b"x")

    catalog = ModelCatalog(models_dir=tmp_path, state_path=tmp_path / "models.json")

    assert [m.name for m in catalog.discover()] == ["Qwen3.5-4B"]
    assert catalog.projectors() == []


def test_status_reports_files_relative_to_the_repo(captioner):
    status = captioner.status()
    assert status["running"] is True
    assert status["port"] == captioner.port
