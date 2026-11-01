"""Settings > Updates (app/updates): llama.cpp swaps and voice package installs, all faked --
no network, no real llama-server, no pip."""
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest

from app.governor import game_mode
from app.remote import remote_route_allowed
from app.updates import llama_cpp
from app.updates.manager import UpdateManager
from app.updates.packages import Venv


def _release(build: int, cuda: str = "12.4", sha: str = "") -> dict:
    name = f"llama-b{build}-bin-win-cuda-{cuda}-x64.zip"
    return {"tag_name": f"b{build}", "published_at": "2026-10-31T21:35:54Z", "html_url": f"https://x/b{build}",
            "assets": [{"name": name, "size": 10, "browser_download_url": f"https://dl/{name}",
                        "digest": f"sha256:{sha}" if sha else None}]}


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def _install(folder: Path, build: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "llama-server.exe").write_text(f"version: 0.7.0-dev (build {build}, commit abc123)")
    (folder / "cudart64_12.dll").write_text("runtime")


@pytest.fixture
def fake_builds(monkeypatch):
    """installed_build reads the text the fake llama-server.exe holds instead of running it."""
    def read(folder: Path):
        exe = folder / "llama-server.exe"
        return llama_cpp.parse_version(exe.read_text()) if exe.is_file() else None
    monkeypatch.setattr(llama_cpp, "installed_build", read)


class Calls:
    def __init__(self, relaunch_ok=(True,)):
        self.log: list[str] = []
        self.blocked_during_release: list = []
        self._relaunch = list(relaunch_ok)

    async def release_all(self):
        self.blocked_during_release.append(game_mode.blocked())
        self.log.append("release")

    async def release_voice(self):
        self.blocked_during_release.append(game_mode.blocked())
        self.log.append("release-voice")

    async def relaunch(self):
        self.log.append("relaunch")
        return self._relaunch.pop(0) if self._relaunch else True


def _manager(tmp_path, calls, zip_bytes=b"", releases=None, runner=None, venvs=()):
    def handler(request: httpx.Request) -> httpx.Response:
        if "api.github.com" in request.url.host:
            return httpx.Response(200, json=releases or [])
        if request.url.host == "pypi.org":
            name = request.url.path.split("/")[2]
            return httpx.Response(200, json={"info": {"version": {"laya": "0.4.19", "faster-whisper": "1.2.1"}.get(name, "9.0")}})
        return httpx.Response(200, content=zip_bytes)

    kwargs = {"runner": runner} if runner else {}
    return UpdateManager(
        llama_dir=tmp_path / "tools" / "llama-cpp", downloads_dir=tmp_path / "tools" / ".updates",
        state_path=tmp_path / "data" / "tool_updates.json",
        release_all=calls.release_all, release_voice=calls.release_voice, relaunch=calls.relaunch,
        transport=httpx.MockTransport(handler), venv_list=lambda: venvs, **kwargs,
    )


# --- llama.cpp ------------------------------------------------------------------------------------

def test_parse_version_reads_both_formats():
    assert llama_cpp.parse_version("version: 0.7.0-dev (build 11899, commit c6fb3b5be)") == {"build": 11899, "commit": "c6fb3b5be"}
    assert llama_cpp.parse_version("version: 10689 (3f2c1a)") == {"build": 10689, "commit": "3f2c1a"}
    assert llama_cpp.parse_version("nonsense") is None


def test_pick_release_takes_the_newest_build_for_the_installed_cuda_major():
    releases = [_release(12024, cuda="13.4"), _release(12023, sha="ab" * 32), _release(12030, cuda="13.4"),
                {"tag_name": "v0.8.0", "assets": []}]
    best = llama_cpp.pick_release(releases, 12)
    assert (best.build, best.asset, best.sha256) == (12023, "llama-b12023-bin-win-cuda-12.4-x64.zip", "ab" * 32)
    assert llama_cpp.pick_release(releases, 11) is None


def test_extract_flattens_one_top_folder_and_refuses_paths_outside(tmp_path):
    zp = tmp_path / "a.zip"
    zp.write_bytes(_zip({"build/llama-server.exe": "x", "build/ggml.dll": "y"}))
    llama_cpp.extract(zp, tmp_path / "out")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["ggml.dll", "llama-server.exe"]

    zp.write_bytes(_zip({"../evil.dll": "x"}))
    with pytest.raises(ValueError):
        llama_cpp.extract(zp, tmp_path / "out2")
    assert not (tmp_path / "evil.dll").exists()


def test_swap_in_keeps_the_old_build_and_swap_back_exchanges_them(tmp_path):
    cur, staged, prev = tmp_path / "llama-cpp", tmp_path / "staged", tmp_path / "llama-cpp.previous"
    _install(cur, 1)
    _install(staged, 2)
    llama_cpp.swap_in(cur, staged, prev)
    assert "build 2" in (cur / "llama-server.exe").read_text() and "build 1" in (prev / "llama-server.exe").read_text()
    llama_cpp.swap_back(cur, prev)
    assert "build 1" in (cur / "llama-server.exe").read_text() and "build 2" in (prev / "llama-server.exe").read_text()


@pytest.mark.asyncio
async def test_llama_update_downloads_verifies_swaps_and_starts_the_model(tmp_path, fake_builds):
    data = _zip({"llama-server.exe": "version: 0.8.0 (build 12024, commit def456)", "ggml-cuda.dll": "new"})
    calls = Calls()
    m = _manager(tmp_path, calls, data, releases=[_release(12024, sha=hashlib.sha256(data).hexdigest())])
    _install(m.llama_dir, 11899)

    status = await m.status(check=True)
    llama = status["tools"][0]
    assert (llama["installed"], llama["latest"], llama["update_available"]) == ("build 11899", "build 12024", True)

    m.start("llama-cpp", "update")
    await m.wait()
    assert m.job.status == "done", m.job.message
    assert "build 12024" in (m.llama_dir / "llama-server.exe").read_text()
    assert (m.llama_dir / "cudart64_12.dll").exists()  # carried over from the old install
    assert "build 11899" in (m.previous_dir / "llama-server.exe").read_text()
    assert calls.log == ["release", "relaunch"]
    assert calls.blocked_during_release == ["llama.cpp is being updated; the model is back in a moment."]
    assert game_mode.blocked() is None
    assert (await m.status())["tools"][0]["roll_back_to"] == "build 11899"


@pytest.mark.asyncio
async def test_llama_update_puts_the_old_build_back_when_the_model_will_not_start(tmp_path, fake_builds):
    data = _zip({"llama-server.exe": "version: 0.8.0 (build 12024, commit def456)"})
    calls = Calls(relaunch_ok=(False, True))
    m = _manager(tmp_path, calls, data, releases=[_release(12024)])
    _install(m.llama_dir, 11899)
    await m.check()
    m.start("llama-cpp", "update")
    await m.wait()
    assert m.job.status == "failed" and "previous one is back" in m.job.message
    assert "build 11899" in (m.llama_dir / "llama-server.exe").read_text()
    assert calls.log == ["release", "relaunch", "release", "relaunch"]


@pytest.mark.asyncio
async def test_llama_update_with_a_bad_checksum_changes_nothing(tmp_path, fake_builds):
    data = _zip({"llama-server.exe": "version: 0.8.0 (build 12024, commit def456)"})
    calls = Calls()
    m = _manager(tmp_path, calls, data, releases=[_release(12024, sha="00" * 32)])
    _install(m.llama_dir, 11899)
    await m.check()
    m.start("llama-cpp", "update")
    await m.wait()
    assert m.job.status == "failed" and "checksum" in m.job.message
    assert calls.log == [] and not m.previous_dir.exists()


@pytest.mark.asyncio
async def test_rollback_without_a_previous_build_fails_and_an_update_is_refused_in_game_mode(tmp_path, fake_builds):
    calls = Calls()
    m = _manager(tmp_path, calls)
    _install(m.llama_dir, 11899)
    m.start("llama-cpp", "rollback")
    await m.wait()
    assert m.job.status == "failed" and calls.log == []

    game_mode.game_mode.active = True
    try:
        with pytest.raises(RuntimeError):
            m.start("llama-cpp", "update")
    finally:
        game_mode.game_mode.active = False


# --- packages -------------------------------------------------------------------------------------

class FakePip:
    """Answers `pip list` from a dict and applies `pkg==ver` installs to it."""

    def __init__(self, have: dict[str, str]):
        self.have = dict(have)
        self.installs: list[list[str]] = []

    def __call__(self, cmd, timeout):
        if "list" in cmd:
            out = json.dumps([{"name": n, "version": v} for n, v in self.have.items()])
            return subprocess.CompletedProcess(cmd, 0, out, "")
        self.installs.append(cmd)
        for spec in (a for a in cmd if "==" in a):
            name, version = spec.split("==")
            self.have[name] = version
            if name == "laya":
                self.have["numpy"] = "2.5.0"  # a dependency that moved with it
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _venv(tmp_path, live=False, held=("torch",)) -> Venv:
    py = tmp_path / "venv" / "python.exe"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("")
    return Venv("sidecars", "Bark and Laya", "", py, "pip", ("laya",), held=held, live=live)


@pytest.mark.asyncio
async def test_package_update_holds_torch_installs_and_records_a_rollback(tmp_path):
    pip = FakePip({"laya": "0.3.23", "torch": "2.11.0+cu128", "numpy": "2.4.6"})
    calls = Calls()
    venv = _venv(tmp_path)
    m = _manager(tmp_path, calls, runner=pip, venvs=(venv,))

    row = (await m.status(check=True))["tools"][1]
    assert row["update_available"] and {p["name"]: p["held"] for p in row["packages"]} == {"laya": False, "torch": True}

    m.start("sidecars", "update")
    await m.wait()
    assert m.job.status == "done", m.job.message
    cmd = pip.installs[0]
    assert "laya==0.4.19" in cmd
    assert (m.downloads_dir / "sidecars-constraints.txt").read_text().strip() == "torch==2.11.0+cu128"
    assert calls.blocked_during_release == ["Voice packages are being updated."]

    m.start("sidecars", "rollback")
    await m.wait()
    assert m.job.status == "done", m.job.message
    assert pip.have["laya"] == "0.3.23" and pip.have["numpy"] == "2.4.6"


@pytest.mark.asyncio
async def test_live_venv_update_is_queued_and_installed_at_the_next_start(tmp_path):
    pip = FakePip({"laya": "0.3.23"})
    calls = Calls()
    venv = _venv(tmp_path, live=True, held=())
    m = _manager(tmp_path, calls, runner=pip, venvs=(venv,))
    await m.check()
    m.start("sidecars", "update")
    await m.wait()
    assert m.job.status == "done" and "next starts" in m.job.message
    assert pip.installs == [] and calls.log == []

    restarted = _manager(tmp_path, calls, runner=pip, venvs=(venv,))
    assert not restarted.packages_ready.is_set()
    restarted.apply_pending()
    assert restarted.packages_ready.is_set()
    assert pip.have["laya"] == "0.4.19"
    assert json.loads(restarted.state_path.read_text())["pending"] == {}


def test_hold_blocks_model_starts_only_while_held():
    assert game_mode.blocked() is None
    with game_mode.hold("tool-update", "busy"):
        assert game_mode.blocked() == "busy"
    assert game_mode.blocked() is None


def test_updates_are_not_reachable_from_the_phone_port():
    assert not remote_route_allowed("GET", "/api/updates")
    assert not remote_route_allowed("POST", "/api/updates/llama-cpp")
