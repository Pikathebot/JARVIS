"""Model download and fit labels (app/agent/model_hub.py, the hub routes in routers/models.py).
Nothing here touches the network: HuggingFace is an httpx.MockTransport."""

import asyncio
import hashlib
import json
import struct
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import model_hub
from app.agent.model_catalog import ModelCatalog, rule_of_thumb_cost_mb
from app.agent.model_hub import (
    DownloadManager,
    HubClient,
    fit_label,
    group_repo_files,
    kv_mib_per_1k,
    read_gguf_metadata,
    read_remote_metadata,
)
from app.governor.resource_governor import ResourceGovernor


# --- a GGUF writer, just enough for headers -----------------------------------------------------

def _s(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<Q", len(raw)) + raw


def _kv(key: str, value) -> bytes:
    if isinstance(value, str):
        return _s(key) + struct.pack("<I", 8) + _s(value)
    if isinstance(value, list) and value and isinstance(value[0], str):
        return _s(key) + struct.pack("<IIQ", 9, 8, len(value)) + b"".join(_s(v) for v in value)
    if isinstance(value, list):
        return _s(key) + struct.pack("<IIQ", 9, 4, len(value)) + b"".join(struct.pack("<I", v) for v in value)
    if isinstance(value, float):
        return _s(key) + struct.pack("<I", 6) + struct.pack("<f", value)
    return _s(key) + struct.pack("<I", 4) + struct.pack("<I", value)


def _gguf(meta: dict, tail: bytes = b"\0" * 64) -> bytes:
    body = b"".join(_kv(k, v) for k, v in meta.items())
    return b"GGUF" + struct.pack("<IQQ", 3, 0, len(meta)) + body + tail


QWEN35 = {
    "general.architecture": "qwen35",
    "tokenizer.ggml.tokens": [f"tok{i}" for i in range(5000)],  # pushes the arch keys past 4 KB
    "qwen35.block_count": 32,
    "qwen35.embedding_length": 4096,
    "qwen35.attention.head_count": 16,
    "qwen35.attention.head_count_kv": 4,
    "qwen35.attention.key_length": 256,
    "qwen35.attention.value_length": 256,
    "qwen35.full_attention_interval": 4,
}


def _reader(data: bytes, calls: list | None = None):
    def read(offset, length):
        if calls is not None:
            calls.append((offset, length))
        return data[offset:offset + length]
    return read


def test_header_parse_reads_past_a_big_vocabulary_in_growing_chunks():
    calls = []
    meta = read_gguf_metadata(_reader(_gguf(QWEN35), calls), first_chunk=1024)
    assert meta["qwen35.full_attention_interval"] == 4
    assert "tokenizer.ggml.tokens" not in meta  # 5000 entries: walked, not kept
    assert len(calls) > 1


def test_kv_rate_of_the_hybrid_9b_matches_what_the_card_measured():
    # 8 attention layers x 4 KV heads x (256 + 256) at q8_0 = 17.0 MiB per 1k tokens
    assert kv_mib_per_1k(read_gguf_metadata(_reader(_gguf(QWEN35)))) == 17.0


def test_kv_rate_of_a_dense_model_and_of_per_layer_head_counts():
    dense = {
        "general.architecture": "llama", "llama.block_count": 42, "llama.embedding_length": 2048,
        "llama.attention.head_count": 16, "llama.attention.head_count_kv": 2,
    }
    assert kv_mib_per_1k(dense) == 22.31  # MiniCPM5-2B: head size from 2048 / 16
    per_layer = {
        "general.architecture": "hy", "hy.block_count": 4, "hy.embedding_length": 1024,
        "hy.attention.head_count": 8, "hy.attention.head_count_kv": [0, 2, 0, 2],
    }
    assert kv_mib_per_1k(per_layer) == pytest.approx(4 * 256 * 34 / 32 * 1024 / 2**20, abs=0.01)
    assert kv_mib_per_1k({"general.architecture": "x"}) is None


def test_not_a_gguf_is_refused():
    with pytest.raises(model_hub.GGUFError):
        read_gguf_metadata(_reader(b"PK\x03\x04" + b"\0" * 100))


@pytest.mark.asyncio
async def test_remote_header_fetches_more_when_the_first_range_is_short():
    data = _gguf(QWEN35)
    calls = []

    async def fetch(offset, length):
        calls.append((offset, length))
        return data[offset:offset + length]

    meta = await read_remote_metadata(fetch, first=512)
    assert kv_mib_per_1k(meta) == 17.0
    assert calls[0] == (0, 512) and len(calls) > 2


# --- fit ---------------------------------------------------------------------------------------

def test_fit_label_needs_500_mb_spare_for_fits():
    assert fit_label(4000, 5000) == "fits"
    assert fit_label(4000, 4499) == "tight"
    assert fit_label(4000, 4000) == "tight"
    assert fit_label(4001, 4000) == "spills"
    assert fit_label(4000, None) == "unknown"


def test_rule_of_thumb_uses_the_models_own_kv_rate():
    mib = 2**20
    assert rule_of_thumb_cost_mb(1000 * mib, None, 32768) == round(880 + 32 * 20 + 500, 1)
    assert rule_of_thumb_cost_mb(1000 * mib, None, 32768, 17.0) == round(880 + 32 * 17 + 500, 1)
    assert rule_of_thumb_cost_mb(1000 * mib, 100 * mib, 1024, 17.0) == round(880 + 350 + 17 + 500, 1)


def test_governor_budget_is_the_card_minus_the_recent_high_water_mark():
    gov = ResourceGovernor(enabled=True)
    gov._external_samples.clear()
    assert gov.fit_budget_mb() is None
    assert gov.card_total_mb() is None
    gov._external_samples.extend([(100.0, 2300.0, 8188.0), (500.0, 1200.0, 8188.0), (900.0, 1500.0, 8188.0)])
    assert gov.fit_budget_mb(window_s=900) == 8188.0 - 2300.0  # the 2.3 GB peak still counts
    assert gov.fit_budget_mb(window_s=600) == 8188.0 - 1500.0  # ...until it ages out
    assert gov.card_total_mb() == 8188.0


# --- repo listing ------------------------------------------------------------------------------

def test_group_repo_files_folds_split_sets_and_separates_projectors():
    tree = [
        {"type": "file", "path": "README.md", "size": 10},
        {"type": "file", "path": "M-Q4.gguf", "size": 4, "lfs": {"oid": "a", "size": 400}},
        {"type": "file", "path": "M-Q8-00002-of-00002.gguf", "lfs": {"oid": "c", "size": 300}},
        {"type": "file", "path": "M-Q8-00001-of-00002.gguf", "lfs": {"oid": "b", "size": 500}},
        {"type": "file", "path": "mmproj-F16.gguf", "lfs": {"oid": "d", "size": 90}},
        {"type": "directory", "path": "sub"},
    ]
    files = group_repo_files(tree)
    assert [f.name for f in files] == ["M-Q4.gguf", "M-Q8-00001-of-00002.gguf", "mmproj-F16.gguf"]
    split = files[1]
    assert split.size_bytes == 800 and [p["path"] for p in split.parts] == [
        "M-Q8-00001-of-00002.gguf", "M-Q8-00002-of-00002.gguf"]
    assert files[0].parts[0] == {"path": "M-Q4.gguf", "size": 400, "sha256": "a"}
    assert files[2].projector


def test_catalog_lists_a_split_model_once(tmp_path):
    root = tmp_path / "models"
    (root / "big").mkdir(parents=True)
    for n in (1, 2, 3):
        (root / "big" / f"Big-Q4-0000{n}-of-00003.gguf").write_bytes(b"\0")
    (root / "big" / "Big-Q4-00001-of-00003.gguf.part").write_bytes(b"\0")
    names = [m.name for m in ModelCatalog(models_dir=root, state_path=tmp_path / "s.json").discover()]
    assert names == ["Big-Q4-00001-of-00003"]


# --- a fake HuggingFace ------------------------------------------------------------------------

class FakeHF:
    """Serves a tree, a search and file bytes with Range support; records Range headers."""

    def __init__(self, files: dict[str, bytes], repo="org/Model-GGUF", bad_hash: set[str] = frozenset()):
        self.repo = repo
        self.files = files
        self.bad_hash = bad_hash
        self.ranges: list[str | None] = []
        self.ignore_range = False

    def tree(self):
        return [
            {"type": "file", "path": name, "size": len(data),
             "lfs": {"oid": "0" * 64 if name in self.bad_hash else hashlib.sha256(data).hexdigest(), "size": len(data)}}
            for name, data in self.files.items()
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/api/models/{self.repo}/tree/main":
            return httpx.Response(200, json=self.tree())
        if path == "/api/models":
            return httpx.Response(200, json=[{"id": self.repo, "downloads": 5, "likes": 1}])
        prefix = f"/{self.repo}/resolve/main/"
        if path.startswith(prefix) and path[len(prefix):] in self.files:
            data = self.files[path[len(prefix):]]
            rng = request.headers.get("Range")
            self.ranges.append(rng)
            if rng and not self.ignore_range:
                start, _, end = rng.removeprefix("bytes=").partition("-")
                start = int(start)
                end = int(end) if end else len(data) - 1
                return httpx.Response(206, content=data[start:end + 1])
            return httpx.Response(200, content=data)
        return httpx.Response(404)

    def hub(self) -> HubClient:
        return HubClient(base="https://hf.test", transport=httpx.MockTransport(self.handler))


def _manager(fake: FakeHF, tmp_path) -> DownloadManager:
    return DownloadManager(fake.hub(), models_dir=tmp_path / "models", chunk_size=1000)


def _parts(fake: FakeHF, *names):
    by_name = {p["path"]: p for f in group_repo_files(fake.tree()) for p in f.parts}
    return [by_name[n] for n in names]


@pytest.mark.asyncio
async def test_download_lands_in_the_repo_folder_after_the_checksum(tmp_path):
    data = bytes(range(256)) * 40
    fake = FakeHF({"M-Q4.gguf": data, "mmproj-F16.gguf": b"p" * 300})
    mgr = _manager(fake, tmp_path)
    job = mgr.start(fake.repo, _parts(fake, "M-Q4.gguf", "mmproj-F16.gguf"))
    await mgr.wait(job.id)
    assert job.status == "done", job.error
    folder = tmp_path / "models" / "Model-GGUF"
    assert (folder / "M-Q4.gguf").read_bytes() == data
    assert (folder / "mmproj-F16.gguf").exists()
    assert not list(folder.glob("*.part"))
    assert job.bytes_done == job.bytes_total == len(data) + 300


@pytest.mark.asyncio
async def test_download_resumes_a_part_file_with_a_range_request(tmp_path):
    data = bytes(range(256)) * 40
    fake = FakeHF({"M-Q4.gguf": data})
    folder = tmp_path / "models" / "Model-GGUF"
    folder.mkdir(parents=True)
    (folder / "M-Q4.gguf.part").write_bytes(data[:4000])
    mgr = _manager(fake, tmp_path)
    job = mgr.start(fake.repo, _parts(fake, "M-Q4.gguf"))
    await mgr.wait(job.id)
    assert job.status == "done", job.error
    assert fake.ranges == ["bytes=4000-"]
    assert (folder / "M-Q4.gguf").read_bytes() == data


@pytest.mark.asyncio
async def test_a_server_that_ignores_range_restarts_the_file(tmp_path):
    data = b"x" * 5000
    fake = FakeHF({"M-Q4.gguf": data})
    fake.ignore_range = True
    folder = tmp_path / "models" / "Model-GGUF"
    folder.mkdir(parents=True)
    (folder / "M-Q4.gguf.part").write_bytes(b"garbage")
    mgr = _manager(fake, tmp_path)
    job = mgr.start(fake.repo, _parts(fake, "M-Q4.gguf"))
    await mgr.wait(job.id)
    assert job.status == "done", job.error
    assert (folder / "M-Q4.gguf").read_bytes() == data


@pytest.mark.asyncio
async def test_a_checksum_mismatch_fails_and_leaves_no_model(tmp_path):
    fake = FakeHF({"M-Q4.gguf": b"x" * 3000}, bad_hash={"M-Q4.gguf"})
    mgr = _manager(fake, tmp_path)
    job = mgr.start(fake.repo, _parts(fake, "M-Q4.gguf"))
    await mgr.wait(job.id)
    assert job.status == "failed" and "checksum" in job.error
    assert not list((tmp_path / "models" / "Model-GGUF").iterdir())


@pytest.mark.asyncio
async def test_not_enough_disk_refuses_before_downloading(tmp_path, monkeypatch):
    fake = FakeHF({"M-Q4.gguf": b"x" * 3000})
    monkeypatch.setattr(model_hub.shutil, "disk_usage", lambda _p: type("U", (), {"free": 1000})())
    mgr = _manager(fake, tmp_path)
    job = mgr.start(fake.repo, _parts(fake, "M-Q4.gguf"))
    await mgr.wait(job.id)
    assert job.status == "failed" and "disk space" in job.error
    assert fake.ranges == []


@pytest.mark.asyncio
async def test_cancel_keeps_the_part_file_for_a_resume(tmp_path):
    fake = FakeHF({"M-Q4.gguf": b"x" * 50_000})
    mgr = _manager(fake, tmp_path)
    job = mgr.start(fake.repo, _parts(fake, "M-Q4.gguf"))
    await asyncio.sleep(0)
    assert mgr.cancel(job.id)
    await mgr.wait(job.id)
    assert job.status == "cancelled"
    assert not (tmp_path / "models" / "Model-GGUF" / "M-Q4.gguf").exists()


def test_start_refuses_paths_that_could_leave_the_folder(tmp_path):
    mgr = DownloadManager(HubClient(), models_dir=tmp_path)
    with pytest.raises(ValueError):
        mgr.start("org/Repo", [{"path": "../evil.gguf", "size": 1}])
    with pytest.raises(ValueError):
        mgr.start("org/Repo", [{"path": "sub/../../x.gguf", "size": 1}])
    with pytest.raises(ValueError):
        mgr.start("../x", [{"path": "a.gguf", "size": 1}])
    with pytest.raises(ValueError):
        mgr.start("org/Repo", [{"path": "/abs.gguf", "size": 1}])


@pytest.mark.asyncio
async def test_a_file_in_a_repo_subfolder_lands_flat_in_the_repo_folder(tmp_path):
    fake = FakeHF({"Q8/M-Q8.gguf": b"y" * 2000})
    mgr = _manager(fake, tmp_path)
    job = mgr.start(fake.repo, _parts(fake, "Q8/M-Q8.gguf"))
    await mgr.wait(job.id)
    assert job.status == "done", job.error
    assert (tmp_path / "models" / "Model-GGUF" / "M-Q8.gguf").read_bytes() == b"y" * 2000


def test_a_model_already_in_a_hand_made_folder_counts_as_downloaded(hub_api, tmp_path):
    client, fake = hub_api
    elsewhere = tmp_path / "models" / "my-model"
    elsewhere.mkdir(parents=True)
    (elsewhere / "M-Q4.gguf").write_bytes(fake.files["M-Q4.gguf"])
    (elsewhere / "M-Q8.gguf").write_bytes(b"wrong size")
    models = {m["name"]: m for m in client.get("/api/models/hub/files", params={"repo": fake.repo}).json()["models"]}
    assert models["M-Q4.gguf"]["downloaded"] is True
    assert models["M-Q8.gguf"]["downloaded"] is False


# --- routes ------------------------------------------------------------------------------------

@pytest.fixture
def hub_api(tmp_path, monkeypatch):
    data = _gguf(QWEN35, tail=b"\0" * 4000)
    fake = FakeHF({"M-Q4.gguf": data, "M-Q8.gguf": data + b"\0" * 4000, "mmproj-F16.gguf": b"p" * 300})
    hub = fake.hub()
    monkeypatch.setattr(model_hub, "_hub", hub)
    monkeypatch.setattr(model_hub, "_downloads", DownloadManager(hub, models_dir=tmp_path / "models"))
    monkeypatch.setattr(model_hub, "budget_provider", lambda: 6000.0)
    from app.main import app
    return TestClient(app), fake


def test_files_route_labels_every_model_per_slot(hub_api):
    client, fake = hub_api
    body = client.get("/api/models/hub/files", params={"repo": fake.repo}).json()
    assert body["kv_mib_per_1k"] == 17.0
    assert [m["name"] for m in body["models"]] == ["M-Q4.gguf", "M-Q8.gguf"]
    fit = body["models"][0]["fit"]
    assert set(fit) == {"text", "vision"}
    assert fit["text"]["main"]["label"] == "fits" and fit["text"]["main"]["source"] == "estimate"
    assert [p["name"] for p in body["projectors"]] == ["mmproj-F16.gguf"]


def test_files_route_rejects_a_bad_repo_and_maps_a_missing_one(hub_api):
    client, _ = hub_api
    assert client.get("/api/models/hub/files", params={"repo": "../etc"}).status_code == 400
    assert client.get("/api/models/hub/files", params={"repo": "org/Nope"}).status_code == 404


def test_download_route_starts_a_job_and_lists_it(hub_api):
    client, fake = hub_api
    resp = client.post("/api/models/download", json={"repo": fake.repo, "file": "M-Q4.gguf", "projector": "mmproj-F16.gguf"})
    assert resp.status_code == 200, resp.text
    job = resp.json()["download"]
    assert job["files"] == ["M-Q4.gguf", "mmproj-F16.gguf"]
    listed = client.get("/api/models/downloads").json()["downloads"]
    assert listed[0]["id"] == job["id"]
    assert client.post("/api/models/download", json={"repo": fake.repo, "file": "mmproj-F16.gguf"}).status_code == 404


def test_models_route_carries_the_budget_and_a_fit_per_model(hub_api):
    client, _ = hub_api
    body = client.get("/api/models").json()
    assert body["fit_budget_mb"] == 6000.0
    for model in body["models"]:
        assert "fit" in model
