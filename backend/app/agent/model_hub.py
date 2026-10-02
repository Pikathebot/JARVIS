"""
Getting models from HuggingFace, and saying before the download whether they will fit.

Three parts, kept apart so each can be tested without the network:

* **The GGUF header reader** (``read_gguf_header``) walks the metadata block at the start of a
  GGUF file through a ``read(offset, length)`` callable -- a local file, or ranged HTTP requests
  against HuggingFace so a 5 GB model can be sized from its first few MB. What it is after is
  the KV cache's shape (``kv_mib_per_1k``): weights are the file size, but the KV cache is what
  makes context expensive, and it differs 3x between a hybrid like Qwen3.5 (8 of 32 layers keep
  KV) and a dense model of the same size.
* **The fit check** (``fit_label``): the card's total minus what everything *except* Jarvis holds
  (the governor's rolling high-water mark, so a browser tab opening and closing doesn't flip the
  label), against the model's cost -- learned from a real launch when there has been one,
  otherwise the same rule of thumb the launch ladder uses. Fits needs 500 MB to spare; a model
  that would spill is labelled, never blocked (the user's call, 2026-10-02).
* **The download manager** (``DownloadManager``): one job at a time, streamed to a ``.part``
  file, resumed with a Range request after a failure or a restart, checked against the SHA-256
  HuggingFace publishes, and only then renamed to ``.gguf`` -- the catalogue globs ``*.gguf``,
  so a half-written file never shows up as a model.
"""

import asyncio
import hashlib
import json
import logging
import re
import shutil
import struct
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from app.agent.model_catalog import REPO_ROOT, DEFAULT_MODELS_DIR, rule_of_thumb_cost_mb

logger = logging.getLogger("jarvis.agent.model_hub")

HF_BASE = "https://huggingface.co"
CURATED_PATH = Path(__file__).with_name("curated_models.json")

FIT_HEADROOM_MB = 500.0
"""Fits needs this much of the budget left over (agreed with the user 2026-10-02)."""

DISK_SPARE_BYTES = 512 * 1024 * 1024
"""A download is refused unless the disk keeps this much free after it."""

_SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)
_REPO_RE = re.compile(r"^[A-Za-z0-9][\w.\-]*/[\w.\-]+$")


def _is_projector_name(name: str) -> bool:
    return Path(name).name.lower().startswith("mmproj")


def valid_repo(repo: str) -> bool:
    """``owner/name`` with nothing that could climb out of a URL path or a folder."""
    return bool(_REPO_RE.match(repo or "")) and ".." not in repo


# --- GGUF header -----------------------------------------------------------------------------

class GGUFError(ValueError):
    pass


_SCALARS = {
    0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d",
}
_STRING, _ARRAY = 8, 9
_MAX_KEPT_ARRAY = 4096  # per-layer arrays are kept; vocabularies (100k+ entries) are skipped


class _Cursor:
    """Sequential reads over ``read(offset, length)``, fetching in growing chunks."""

    def __init__(self, read: Callable[[int, int], bytes], first_chunk: int, max_bytes: int):
        self._read = read
        self._buf = b""
        self._pos = 0
        self._chunk = first_chunk
        self._max = max_bytes

    def take(self, n: int) -> bytes:
        while self._pos + n > len(self._buf):
            if len(self._buf) >= self._max:
                raise GGUFError(f"GGUF metadata runs past {self._max // (1024 * 1024)} MB")
            want = max(self._chunk, self._pos + n - len(self._buf))
            got = self._read(len(self._buf), want)
            if not got:
                raise GGUFError("File ended inside the GGUF header")
            self._buf += got
            self._chunk = min(self._chunk * 2, 16 * 1024 * 1024)
        out = self._buf[self._pos:self._pos + n]
        self._pos += n
        return out

    def unpack(self, fmt: str):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def string(self) -> bytes:
        return self.take(self.unpack("<Q"))


def read_gguf_metadata(
    read: Callable[[int, int], bytes],
    first_chunk: int = 4 * 1024 * 1024,
    max_bytes: int = 64 * 1024 * 1024,
) -> dict[str, Any]:
    """
    The key/value metadata of a GGUF file (v2/v3), without the tensor table.

    Strings and numbers are kept; arrays are kept only when short (per-layer values such as
    ``head_count_kv`` on models that vary it by layer). The vocabulary arrays still have to be
    walked, so a model with a big tokenizer needs a few more MB than the first chunk -- the
    cursor fetches more as it goes.
    """
    cur = _Cursor(read, first_chunk, max_bytes)
    if cur.take(4) != b"GGUF":
        raise GGUFError("Not a GGUF file")
    version = cur.unpack("<I")
    if version < 2:
        raise GGUFError(f"GGUF version {version} is too old to read")
    cur.unpack("<Q")  # tensor count
    kv_count = cur.unpack("<Q")

    def value(vtype: int, keep: bool):
        if vtype in _SCALARS:
            return cur.unpack(_SCALARS[vtype])
        if vtype == _STRING:
            raw = cur.string()
            return raw.decode("utf-8", errors="replace") if keep else None
        if vtype == _ARRAY:
            etype = cur.unpack("<I")
            count = cur.unpack("<Q")
            keep_items = keep and count <= _MAX_KEPT_ARRAY
            if etype in _SCALARS and not keep_items:
                cur.take(struct.calcsize(_SCALARS[etype]) * count)
                return None
            items = [value(etype, keep_items) for _ in range(count)]
            return items if keep_items else None
        raise GGUFError(f"Unknown GGUF value type {vtype}")

    meta: dict[str, Any] = {}
    for _ in range(kv_count):
        key = cur.string().decode("utf-8", errors="replace")
        vtype = cur.unpack("<I")
        v = value(vtype, keep=True)
        if v is not None:
            meta[key] = v
    return meta


def kv_mib_per_1k(meta: dict[str, Any]) -> Optional[float]:
    """
    MiB of q8_0 KV cache per 1024 tokens of context, from a model's metadata; None when the
    metadata doesn't describe attention (the caller falls back to the rule of thumb).

    Hybrid models only keep KV on their attention layers: Qwen3.5 says so with
    ``full_attention_interval`` (one layer in four), others with a per-layer ``head_count_kv``
    array holding 0 on the recurrent layers. Checked against the 9B: ~17 MiB/1k, which is what
    the 4k/16k/32k launches measured.
    """
    arch = meta.get("general.architecture")
    if not arch:
        return None

    def get(name):
        return meta.get(f"{arch}.{name}")

    layers = get("block_count")
    heads = get("attention.head_count")
    kv_heads = get("attention.head_count_kv")
    if kv_heads is None:
        kv_heads = heads
    embd = get("embedding_length")
    if not layers or kv_heads is None:
        return None

    def first_nonzero(v):
        if isinstance(v, list):
            return next((x for x in v if x), 0)
        return v

    head_dim = (embd // first_nonzero(heads)) if embd and first_nonzero(heads) else None
    k_len = get("attention.key_length") or head_dim
    v_len = get("attention.value_length") or head_dim
    if not k_len or not v_len:
        return None

    if isinstance(kv_heads, list):
        per_layer = [int(h) for h in kv_heads[: int(layers)]]
    else:
        per_layer = [int(kv_heads)] * int(layers)
        interval = get("full_attention_interval")
        if interval and int(interval) > 1:
            per_layer = [h if (i + 1) % int(interval) == 0 else 0 for i, h in enumerate(per_layer)]

    elements_per_token = sum(h * (int(k_len) + int(v_len)) for h in per_layer)
    bytes_per_token = elements_per_token * 34 / 32  # q8_0: 32 values in 34 bytes
    return round(bytes_per_token * 1024 / (1024 * 1024), 2)


def read_local_metadata(path: Path) -> dict[str, Any]:
    with open(path, "rb") as fh:
        def read(offset: int, length: int) -> bytes:
            fh.seek(offset)
            return fh.read(length)

        return read_gguf_metadata(read)


# --- fit -------------------------------------------------------------------------------------

def fit_label(cost_mb: Optional[float], budget_mb: Optional[float]) -> str:
    """'fits' (>= 500 MB to spare), 'tight', 'spills', or 'unknown' without a budget."""
    if cost_mb is None or budget_mb is None:
        return "unknown"
    spare = budget_mb - cost_mb
    if spare >= FIT_HEADROOM_MB:
        return "fits"
    if spare >= 0:
        return "tight"
    return "spills"


def slot_ctx_sizes() -> dict[str, int]:
    from app.config import settings

    return {"main": int(settings.llama_ctx_size_main), "fast": int(settings.llama_ctx_size_fast)}


def slot_fits(
    model_bytes: int,
    projector_bytes: Optional[int],
    kv_rate: Optional[float],
    budget_mb: Optional[float],
    learned: Optional[Callable[[str, int], Optional[float]]] = None,
) -> dict[str, dict[str, Any]]:
    """Cost and label per slot. ``learned(slot, ctx)`` returns a measured launch cost when the
    model has run here before; it wins over the estimate."""
    out: dict[str, dict[str, Any]] = {}
    for slot, ctx in slot_ctx_sizes().items():
        cost = learned(slot, ctx) if learned else None
        source = "measured" if cost else "estimate"
        if not cost:
            cost = rule_of_thumb_cost_mb(model_bytes, projector_bytes, ctx, kv_rate)
        out[slot] = {"ctx": ctx, "cost_mb": round(cost), "source": source, "label": fit_label(cost, budget_mb)}
    return out


# --- HuggingFace -------------------------------------------------------------------------------

@dataclass
class HubFile:
    """One downloadable model: a single .gguf, or the parts of a split one."""

    name: str
    """Display name and the file the catalogue will list (the first part of a split set)."""
    parts: list[dict[str, Any]]
    """[{path, size, sha256}] in download order."""
    size_bytes: int
    projector: bool

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "size_bytes": self.size_bytes, "parts": len(self.parts), "projector": self.projector}


def group_repo_files(tree: list[dict[str, Any]]) -> list[HubFile]:
    """The tree API's entries -> models and projectors, split sets folded into one entry."""
    singles: list[HubFile] = []
    splits: dict[str, list[tuple[int, dict]]] = {}
    for entry in tree:
        path = entry.get("path", "")
        if entry.get("type") != "file" or not path.lower().endswith(".gguf"):
            continue
        lfs = entry.get("lfs") or {}
        part = {"path": path, "size": int(lfs.get("size") or entry.get("size") or 0), "sha256": lfs.get("oid")}
        m = _SPLIT_RE.search(path)
        if m:
            splits.setdefault(path[: m.start()], []).append((int(m.group(1)), part))
        else:
            singles.append(HubFile(Path(path).name, [part], part["size"], _is_projector_name(path)))
    for _, parts in splits.items():
        parts.sort(key=lambda p: p[0])
        ordered = [p for _, p in parts]
        singles.append(HubFile(
            Path(ordered[0]["path"]).name, ordered, sum(p["size"] for p in ordered),
            _is_projector_name(ordered[0]["path"]),
        ))
    singles.sort(key=lambda f: (f.projector, f.size_bytes))
    return singles


class HubClient:
    """The few HuggingFace calls the picker needs. ``transport`` is for tests."""

    def __init__(self, base: str = HF_BASE, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.base = base.rstrip("/")
        self._transport = transport
        self._tree_cache: dict[str, tuple[float, list[HubFile]]] = {}
        self._kv_cache: dict[tuple[str, str], Optional[float]] = {}

    def client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, transport=self._transport,
            headers={"User-Agent": "Jarvis-local/1.0"},
        )

    def file_url(self, repo: str, path: str) -> str:
        return f"{self.base}/{repo}/resolve/main/{path}"

    async def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        params = {"search": query, "filter": "gguf", "sort": "downloads", "direction": "-1", "limit": str(limit)}
        async with self.client() as c:
            resp = await c.get(f"{self.base}/api/models", params=params)
            resp.raise_for_status()
            return [
                {"repo": m.get("id"), "downloads": m.get("downloads", 0), "likes": m.get("likes", 0),
                 "updated": m.get("lastModified") or m.get("createdAt")}
                for m in resp.json() if m.get("id")
            ]

    async def files(self, repo: str) -> list[HubFile]:
        cached = self._tree_cache.get(repo)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        async with self.client() as c:
            resp = await c.get(f"{self.base}/api/models/{repo}/tree/main", params={"recursive": "true"})
            resp.raise_for_status()
            grouped = group_repo_files(resp.json())
        self._tree_cache[repo] = (time.time(), grouped)
        return grouped

    async def repo_kv_rate(self, repo: str, files: list[HubFile]) -> Optional[float]:
        """One header per repo: every quant in a repo is the same model, so the same KV shape,
        and reading a header costs ~5 s over the network. The smallest file is read."""
        models = [f for f in files if not f.projector]
        if not models:
            return None
        return await self.kv_rate(repo, min(models, key=lambda f: f.size_bytes).parts[0]["path"])

    async def kv_rate(self, repo: str, path: str) -> Optional[float]:
        """KV MiB per 1k tokens from the file's header, read with Range requests."""
        key = (repo, path)
        if key in self._kv_cache:
            return self._kv_cache[key]
        url = self.file_url(repo, path)
        rate: Optional[float] = None
        try:
            async with self.client(timeout=60.0) as c:
                async def fetch(offset: int, length: int) -> bytes:
                    resp = await c.get(url, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
                    if resp.status_code != 206:
                        raise GGUFError(f"Range request answered {resp.status_code}")
                    return resp.content

                rate = kv_mib_per_1k(await read_remote_metadata(fetch))
        except Exception as exc:
            logger.info("Could not read the GGUF header of %s/%s: %s", repo, path, exc)
        self._kv_cache[key] = rate
        return rate


class _NeedMore(Exception):
    pass


async def read_remote_metadata(fetch, first: int = 4 * 1024 * 1024, limit: int = 64 * 1024 * 1024) -> dict[str, Any]:
    """The synchronous header parser over async range fetches: parse the prefix fetched so
    far, and when it runs off the end, fetch as much again and start over (1-2 rounds for a
    big tokenizer; the parse itself takes milliseconds)."""
    have = await fetch(0, first)
    while True:
        def read(offset: int, length: int) -> bytes:
            if offset >= len(have):
                raise _NeedMore()
            return have[offset:offset + length]

        try:
            return read_gguf_metadata(read, first_chunk=len(have), max_bytes=len(have) + 1)
        except _NeedMore:
            if len(have) >= limit:
                raise GGUFError(f"GGUF metadata runs past {limit // (1024 * 1024)} MB")
            have += await fetch(len(have), len(have))


budget_provider: Optional[Callable[[], Optional[float]]] = None
"""Set at startup to the governor's ``fit_budget_mb``; None leaves every label 'unknown'."""


def current_budget_mb() -> Optional[float]:
    if budget_provider is None:
        return None
    try:
        value = budget_provider()
    except Exception as exc:
        logger.debug("fit budget provider failed: %s", exc)
        return None
    return None if value is None else float(value)


_local_kv: dict[tuple[str, float], Optional[float]] = {}


def local_kv_rate(path: Path) -> Optional[float]:
    """KV rate of a model on disk, cached per file and modification time."""
    try:
        key = (str(path.resolve()), path.stat().st_mtime)
    except OSError:
        return None
    if key not in _local_kv:
        try:
            _local_kv[key] = kv_mib_per_1k(read_local_metadata(path))
        except Exception as exc:
            logger.info("Could not read the GGUF header of %s: %s", path, exc)
            _local_kv[key] = None
    return _local_kv[key]


def load_curated(path: Path = CURATED_PATH) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [e for e in data.get("models", []) if valid_repo(e.get("repo", "")) and e.get("file")]
    except Exception as exc:
        logger.warning("Could not read curated models from %s: %s", path, exc)
        return []


# --- downloads ---------------------------------------------------------------------------------

@dataclass
class DownloadJob:
    id: str
    repo: str
    files: list[dict[str, Any]]
    """Every part to fetch, model parts first then the projector: [{path, size, sha256}]."""
    dest_dir: str
    status: str = "queued"  # queued | downloading | verifying | done | failed | cancelled
    bytes_done: int = 0
    bytes_total: int = 0
    current_file: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["files"] = [f["path"] for f in self.files]
        return d


class DownloadCancelled(Exception):
    pass


class DownloadManager:
    """
    Fetches files into ``models/<repo name>/``, one job at a time.

    The folder is the convention the catalogue already pairs projectors by, so downloading a
    model together with the repo's ``mmproj`` gives it vision with nothing else to set up.
    """

    def __init__(self, hub: HubClient, models_dir: Optional[Path] = None, chunk_size: int = 1024 * 1024):
        self.hub = hub
        self.models_dir = Path(models_dir) if models_dir else DEFAULT_MODELS_DIR
        self.chunk_size = chunk_size
        self._jobs: dict[str, DownloadJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def dest_dir_for(self, repo: str) -> Path:
        return self.models_dir / repo.split("/", 1)[1]

    def jobs(self) -> list[DownloadJob]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get(self, job_id: str) -> Optional[DownloadJob]:
        return self._jobs.get(job_id)

    def start(self, repo: str, files: list[dict[str, Any]]) -> DownloadJob:
        if not valid_repo(repo):
            raise ValueError(f"Not a HuggingFace repo id: {repo!r}")
        for f in files:
            # Repo paths may sit in subfolders (big split quants often do); each file is saved
            # under its own name in the repo's folder, so only the URL uses the path.
            segments = f["path"].split("/")
            if (not f["path"].lower().endswith(".gguf") or "\\" in f["path"]
                    or any(seg in ("", ".", "..") for seg in segments)):
                raise ValueError(f"Not a downloadable GGUF path: {f['path']!r}")
        active = [j for j in self._jobs.values() if j.status in ("queued", "downloading", "verifying")]
        wanted = {f["path"] for f in files}
        for j in active:
            if j.repo == repo and wanted & {f["path"] for f in j.files}:
                return j  # already on its way
        dest = self.dest_dir_for(repo)
        job = DownloadJob(
            id=uuid.uuid4().hex[:12], repo=repo, files=files, dest_dir=dest.as_posix(),
            bytes_total=sum(int(f.get("size") or 0) for f in files),
        )
        self._jobs[job.id] = job
        self._tasks[job.id] = asyncio.create_task(self._run(job))
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        task = self._tasks.get(job_id)
        if not job or job.status not in ("queued", "downloading", "verifying"):
            return False
        job.status = "cancelled"
        if task:
            task.cancel()
        return True

    async def wait(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self, job: DownloadJob) -> None:
        try:
            async with self._lock:
                if job.status == "cancelled":
                    return
                await self._download(job)
                job.status = "done"
        except (asyncio.CancelledError, DownloadCancelled):
            job.status = "cancelled"
        except Exception as exc:
            logger.warning("Download %s (%s) failed: %s", job.id, job.repo, exc)
            job.status = "failed"
            job.error = str(exc)
        finally:
            job.finished_at = time.time()
            job.current_file = None

    async def _download(self, job: DownloadJob) -> None:
        dest_dir = Path(job.dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        def on_disk(f) -> int:
            final = dest_dir / Path(f["path"]).name
            if final.exists():
                return final.stat().st_size
            part = final.with_name(final.name + ".part")
            return part.stat().st_size if part.exists() else 0

        remaining = sum(max(0, int(f.get("size") or 0) - on_disk(f)) for f in job.files)
        free = shutil.disk_usage(dest_dir).free
        if remaining + DISK_SPARE_BYTES > free:
            raise RuntimeError(
                f"Not enough disk space: needs {remaining / 1e9:.1f} GB plus "
                f"{DISK_SPARE_BYTES / 1e9:.1f} GB spare, {free / 1e9:.1f} GB free"
            )

        job.status = "downloading"
        done_before = 0
        async with self.hub.client(timeout=httpx.Timeout(60.0, read=120.0)) as client:
            for f in job.files:
                await self._fetch_file(client, job, f, dest_dir, done_before)
                done_before += int(f.get("size") or 0)
                job.bytes_done = done_before

    async def _fetch_file(self, client: httpx.AsyncClient, job: DownloadJob, f: dict, dest_dir: Path, done_before: int) -> None:
        name = Path(f["path"]).name
        size = int(f.get("size") or 0)
        final = dest_dir / name
        part = final.with_name(name + ".part")
        job.current_file = name

        if final.exists() and (not size or final.stat().st_size == size):
            return  # already here (a re-download of a set, or the projector beside a new quant)

        digest = hashlib.sha256()
        offset = part.stat().st_size if part.exists() else 0
        if size and offset > size:
            part.unlink()
            offset = 0
        if offset:
            # Resuming: the hash has to cover the bytes already on disk too.
            await asyncio.to_thread(_hash_into, part, digest)
        job.bytes_done = done_before + offset

        if not size or offset < size:
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            async with client.stream("GET", self.hub.file_url(job.repo, f["path"]), headers=headers) as resp:
                if resp.status_code == 200 and offset:
                    # The server ignored the Range: start the file over.
                    offset = 0
                    digest = hashlib.sha256()
                    part.unlink(missing_ok=True)
                elif resp.status_code not in (200, 206):
                    raise RuntimeError(f"{name}: HuggingFace answered {resp.status_code}")
                with open(part, "ab") as out:
                    async for chunk in resp.aiter_bytes(self.chunk_size):
                        if job.status == "cancelled":
                            raise DownloadCancelled()
                        await asyncio.to_thread(_write_and_hash, out, digest, chunk)
                        offset += len(chunk)
                        job.bytes_done = done_before + offset

        if size and offset != size:
            raise RuntimeError(f"{name}: got {offset} bytes, expected {size}")
        job.status = "verifying"
        expected = f.get("sha256")
        if expected and digest.hexdigest() != expected:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{name}: checksum mismatch, the partial file was deleted")
        part.replace(final)
        job.status = "downloading"


def _write_and_hash(out, digest, chunk: bytes) -> None:
    out.write(chunk)
    digest.update(chunk)


def _hash_into(path: Path, digest) -> None:
    with open(path, "rb") as fh:
        while block := fh.read(8 * 1024 * 1024):
            digest.update(block)


_hub: Optional[HubClient] = None
_downloads: Optional[DownloadManager] = None


def get_hub_client() -> HubClient:
    global _hub
    if _hub is None:
        _hub = HubClient()
    return _hub


def get_download_manager() -> DownloadManager:
    global _downloads
    if _downloads is None:
        _downloads = DownloadManager(get_hub_client())
    return _downloads
