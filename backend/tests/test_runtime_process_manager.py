import asyncio
import os
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.runtime_process_manager import (
    RuntimeProcessManager,
    reset_runtime_process_manager,
    resolve_repo_path,
)


@pytest.fixture(autouse=True)
def clean_pm():
    reset_runtime_process_manager()
    yield
    reset_runtime_process_manager()


def test_resolve_repo_path():
    p_rel = resolve_repo_path("models/test.gguf")
    assert p_rel.is_absolute()
    assert str(p_rel).endswith("models\\test.gguf") or str(p_rel).endswith("models/test.gguf")


@pytest.mark.anyio
async def test_ensure_running_noops_when_healthy():
    pm = RuntimeProcessManager()
    with patch.object(pm, "health_check", return_value=True), \
         patch("subprocess.Popen") as mock_spawn:
        res = await pm.ensure_running(model_kind="main")
        assert res is True
        mock_spawn.assert_not_called()
        assert pm.is_externally_managed is True


@pytest.mark.anyio
async def test_ensure_running_recheck_keeps_externally_managed_false_for_jarvis_spawned():
    pm = RuntimeProcessManager()
    mock_proc = MagicMock()
    mock_proc.poll = MagicMock(return_value=None)
    mock_proc.pid = 8888
    pm._process = mock_proc
    pm._current_model_kind = "main"
    pm._externally_managed = False

    with patch.object(pm, "health_check", return_value=True), \
         patch("subprocess.Popen") as mock_spawn:
        res = await pm.ensure_running(model_kind="main")
        assert res is True
        mock_spawn.assert_not_called()
        # Must stay False because Jarvis spawned it
        assert pm.is_externally_managed is False



@pytest.mark.anyio
async def test_ensure_running_spawns_when_unhealthy():
    pm = RuntimeProcessManager(startup_timeout=5.0)

    mock_proc = MagicMock()
    mock_proc.poll = MagicMock(return_value=None)
    mock_proc.stdout = None
    mock_proc.stderr = None
    mock_proc.pid = 9999

    health_states = [False, False, True]

    async def mock_health(timeout=None):
        if health_states:
            return health_states.pop(0)
        return True

    with patch.object(pm, "health_check", side_effect=mock_health), \
         patch("subprocess.Popen") as mock_spawn:
        mock_spawn.return_value = mock_proc
        res = await pm.ensure_running(model_kind="main")

        assert res is True
        mock_spawn.assert_called_once()
        args, kwargs = mock_spawn.call_args
        cmd = args[0]
        assert "--model" in cmd
        assert "--alias" in cmd
        assert "main" in cmd
        assert "--ctx-size" in cmd


@pytest.mark.anyio
async def test_stop_terminates_and_kills_on_timeout():
    pm = RuntimeProcessManager()
    mock_proc = MagicMock()
    mock_proc.poll = MagicMock(return_value=None)
    mock_proc.pid = 1234
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()
    mock_proc.wait = MagicMock(return_value=0)
    pm._process = mock_proc
    pm._externally_managed = False

    res = await pm.stop()
    assert res is True
    mock_proc.terminate.assert_called_once()
    mock_proc.kill.assert_called_once()
    assert pm._process is None


@pytest.mark.anyio
async def test_externally_managed_server_is_never_killed():
    pm = RuntimeProcessManager()
    pm._externally_managed = True
    pm._process = None

    res = await pm.stop()
    assert res is True
    assert pm._process is None


@pytest.mark.anyio
async def test_stop_does_not_sweep_unrelated_processes_by_default():
    pm = RuntimeProcessManager()
    pm._process = None

    unrelated_proc = MagicMock()
    unrelated_proc.info = {"pid": 55555, "name": "llama-server.exe"}
    unrelated_proc.terminate = MagicMock()

    with patch("psutil.process_iter", return_value=[unrelated_proc]) as mock_iter:
        res = await pm.stop(sweep_all=False)
        assert res is True
        mock_iter.assert_not_called()
        unrelated_proc.terminate.assert_not_called()


@pytest.mark.anyio
async def test_stop_sweeps_all_when_explicitly_requested():
    pm = RuntimeProcessManager()
    pm._process = None

    unrelated_proc = MagicMock()
    unrelated_proc.info = {"pid": 55555, "name": "llama-server.exe"}
    unrelated_proc.terminate = MagicMock()
    unrelated_proc.wait = MagicMock(return_value=0)

    with patch("psutil.process_iter", return_value=[unrelated_proc]) as mock_iter:
        res = await pm.stop(sweep_all=True)
        assert res is True
        mock_iter.assert_called_once()
        unrelated_proc.terminate.assert_called_once()



@pytest.mark.anyio
async def test_switch_model_restarts_with_new_model():
    pm = RuntimeProcessManager()
    pm._current_model_kind = "main"
    pm._process = MagicMock(returncode=None)

    with patch.object(pm, "_stop_internal", new_callable=AsyncMock) as mock_stop, \
         patch.object(pm, "ensure_running", new_callable=AsyncMock) as mock_ensure:
        mock_stop.return_value = True
        mock_ensure.return_value = True

        res = await pm.switch_model("fast")
        assert res is True
        mock_stop.assert_awaited_once()
        mock_ensure.assert_awaited_once_with("fast")


def test_static_audit_no_lms_cli_usage_in_backend_app():
    """
    Static analysis check verifying no active module in backend/app
    references 'lms' CLI subprocess or shutil.which for LM Studio,
    except for the deprecated lmstudio_client.py.
    """
    app_dir = Path(__file__).resolve().parent.parent / "app"
    forbidden_terms = ["shutil.which('lms')", 'shutil.which("lms")', "lms unload", "lms load"]

    for py_file in app_dir.rglob("*.py"):
        if py_file.name == "lmstudio_client.py":
            continue
        code = py_file.read_text(encoding="utf-8", errors="ignore")
        for term in forbidden_terms:
            assert term not in code, f"Forbidden LM Studio CLI pattern '{term}' found in {py_file}"


# --- vision: the projector beside a model is passed as --mmproj -------------


def _gguf(path, size=16):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)
    return path


@pytest.fixture
def vision_catalog(tmp_path, monkeypatch):
    """A catalogue over a temp models/ tree: fast model with a projector, main without."""
    import app.agent.model_catalog as mc
    from app.agent.model_catalog import ModelCatalog

    root = tmp_path / "models"
    _gguf(root / "qwen3.5-4b" / "Qwen3.5-4B-UD-Q4_K_XL.gguf")
    _gguf(root / "qwen3.5-4b" / "mmproj-F16.gguf")
    _gguf(root / "qwen3.5-9b" / "Qwen3.5-9B-UD-Q3_K_XL.gguf")
    catalog = ModelCatalog(models_dir=root, state_path=tmp_path / "models.json")
    monkeypatch.setattr(mc, "_catalog", catalog)
    return root


async def _spawn_and_capture_cmd(pm, model_kind):
    mock_proc = MagicMock()
    mock_proc.poll = MagicMock(return_value=None)
    mock_proc.stdout = None
    mock_proc.stderr = None
    mock_proc.pid = 9999
    health_states = [False, False, True]

    async def mock_health(timeout=None):
        return health_states.pop(0) if health_states else True

    with patch.object(pm, "health_check", side_effect=mock_health),          patch("subprocess.Popen") as mock_spawn:
        mock_spawn.return_value = mock_proc
        assert await pm.ensure_running(model_kind=model_kind) is True
        return mock_spawn.call_args[0][0]


@pytest.mark.asyncio
async def test_launch_passes_mmproj_when_the_model_has_a_projector(vision_catalog):
    pm = RuntimeProcessManager(
        startup_timeout=5.0,
        fast_model_path=str(vision_catalog / "qwen3.5-4b" / "Qwen3.5-4B-UD-Q4_K_XL.gguf"),
    )
    cmd = await _spawn_and_capture_cmd(pm, "fast")

    assert "--mmproj" in cmd
    assert Path(cmd[cmd.index("--mmproj") + 1]).name == "mmproj-F16.gguf"
    assert "--no-mmproj-offload" not in cmd
    assert pm.has_vision("fast") is True


@pytest.mark.asyncio
async def test_launch_is_text_only_when_no_projector_sits_beside_the_model(vision_catalog):
    pm = RuntimeProcessManager(
        startup_timeout=5.0,
        main_model_path=str(vision_catalog / "qwen3.5-9b" / "Qwen3.5-9B-UD-Q3_K_XL.gguf"),
    )
    cmd = await _spawn_and_capture_cmd(pm, "main")

    assert "--mmproj" not in cmd
    assert pm.has_vision("main") is False


@pytest.mark.asyncio
async def test_mmproj_can_be_disabled_and_kept_off_the_gpu(vision_catalog, monkeypatch):
    from app.config import settings

    fast = str(vision_catalog / "qwen3.5-4b" / "Qwen3.5-4B-UD-Q4_K_XL.gguf")

    monkeypatch.setattr(settings, "llama_mmproj_enabled", False)
    pm = RuntimeProcessManager(startup_timeout=5.0, fast_model_path=fast)
    assert "--mmproj" not in await _spawn_and_capture_cmd(pm, "fast")
    assert pm.has_vision("fast") is False

    monkeypatch.setattr(settings, "llama_mmproj_enabled", True)
    monkeypatch.setattr(settings, "llama_mmproj_offload", False)
    pm = RuntimeProcessManager(startup_timeout=5.0, fast_model_path=fast)
    cmd = await _spawn_and_capture_cmd(pm, "fast")
    assert "--mmproj" in cmd and "--no-mmproj-offload" in cmd


def test_stale_configured_path_falls_back_to_a_catalogued_model(vision_catalog):
    """Models moved into per-folder layout; a .env path to the old flat file must still resolve."""
    pm = RuntimeProcessManager(fast_model_path="models/Qwen3.5-4B-UD-Q4_K_XL.gguf")
    resolved, alias = pm.resolve_model_path("fast")

    assert alias == "fast"
    assert resolved.name == "Qwen3.5-4B-UD-Q4_K_XL.gguf"
    assert resolved.parent.name == "qwen3.5-4b"


@pytest.mark.asyncio
async def test_projector_that_fails_to_load_falls_back_to_text_only(vision_catalog):
    """A projector OOM (real case: 9B + mmproj on an 8GB card with a game open) must not leave
    the user with no model. The relaunch drops --mmproj and has_vision says so."""
    pm = RuntimeProcessManager(
        startup_timeout=5.0,
        fast_model_path=str(vision_catalog / "qwen3.5-4b" / "Qwen3.5-4B-UD-Q4_K_XL.gguf"),
    )

    # First child dies at once (as llama-server does on cudaMalloc failure); second one lives.
    dead = MagicMock(); dead.poll = MagicMock(return_value=3221226505); dead.returncode = 3221226505
    dead.stdout = None; dead.stderr = None; dead.pid = 1
    alive = MagicMock(); alive.poll = MagicMock(return_value=None)
    alive.stdout = None; alive.stderr = None; alive.pid = 2
    health_states = [False, True]

    async def mock_health(timeout=None):
        return health_states.pop(0) if health_states else True

    with patch.object(pm, "health_check", side_effect=mock_health), \
         patch("subprocess.Popen", side_effect=[dead, alive]) as mock_spawn:
        assert await pm.ensure_running(model_kind="fast") is True

    first, second = (call.args[0] for call in mock_spawn.call_args_list)
    assert "--mmproj" in first
    assert "--mmproj" not in second
    assert second == [arg for arg in first if arg not in ("--mmproj", first[first.index("--mmproj") + 1])]
    assert pm.loaded_projector is None
    assert pm.has_vision("fast") is False  # live answer, despite the projector on disk


@pytest.mark.anyio
async def test_reload_sweeps_a_foreign_server_it_has_not_met_yet():
    """
    A fresh backend has never health-checked, so _externally_managed is False even though a
    server started elsewhere owns the port. reload() must ask the port itself; otherwise the
    new server binds behind the old one (Windows allows it) and the switch is a silent no-op.
    """
    pm = RuntimeProcessManager()
    pm._externally_managed = False
    pm._process = None

    with patch.object(pm, "health_check", new_callable=AsyncMock, return_value=True), \
         patch.object(pm, "stop", new_callable=AsyncMock, return_value=True) as mock_stop, \
         patch.object(pm, "ensure_running", new_callable=AsyncMock, return_value=True):
        assert await pm.reload("fast") is True

    mock_stop.assert_awaited_once_with(sweep_all=True)


@pytest.mark.anyio
async def test_reload_does_not_sweep_when_the_healthy_server_is_our_own_child():
    pm = RuntimeProcessManager()
    pm._externally_managed = False
    pm._process = MagicMock()
    pm._process.poll.return_value = None  # alive

    with patch.object(pm, "health_check", new_callable=AsyncMock, return_value=True), \
         patch.object(pm, "stop", new_callable=AsyncMock, return_value=True) as mock_stop, \
         patch.object(pm, "ensure_running", new_callable=AsyncMock, return_value=True):
        await pm.reload("fast")

    mock_stop.assert_awaited_once_with(sweep_all=False)
