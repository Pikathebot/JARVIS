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


def test_model_vram_mb_measured_then_estimated_then_zero(tmp_path):
    """The governor's attribution input: the launch delta when measured, a size-based estimate
    for an adopted server, and 0 when nothing is running."""
    pm = RuntimeProcessManager()
    assert pm.model_vram_mb == 0.0

    gguf = tmp_path / "m.gguf"; gguf.write_bytes(b"\0" * (300 * 1024 * 1024))
    proj = tmp_path / "mmproj.gguf"; proj.write_bytes(b"\0" * (100 * 1024 * 1024))
    pm._externally_managed = True
    pm._current_model_kind = "main"
    pm._loaded_projector = proj
    pm.resolve_model_path = lambda kind: (gguf, "main")  # type: ignore[method-assign]
    assert pm.model_vram_mb == 400.0 + 600.0  # weights + projector + fixed overhead

    pm._model_vram_mb = 5987.3
    assert pm.model_vram_mb == 5987.3


# --- launch budget ladder: projector, then context, then the fast model ------------------


@pytest.fixture
def budget_catalog(tmp_path, monkeypatch):
    """Both slots have a projector; main is the bigger file. Sizes are tiny so the estimate is
    dominated by its constants: projector +250, KV 20 MiB per 1k ctx, +500 fixed."""
    import app.agent.model_catalog as mc
    from app.agent.model_catalog import ModelCatalog
    from app.config import settings

    root = tmp_path / "models"
    _gguf(root / "qwen3.5-9b" / "Qwen3.5-9B-UD-IQ3_XXS.gguf")
    _gguf(root / "qwen3.5-9b" / "mmproj-Q8_0.gguf")
    _gguf(root / "qwen3.5-2b" / "Qwen3.5-2B-UD-Q4_K_XL.gguf")
    _gguf(root / "qwen3.5-2b" / "mmproj-F16.gguf")
    catalog = ModelCatalog(models_dir=root, state_path=tmp_path / "models.json")
    monkeypatch.setattr(mc, "_catalog", catalog)
    monkeypatch.setattr(settings, "llama_ctx_size_main", 32768)
    monkeypatch.setattr(settings, "llama_ctx_size_fast", 8192)
    monkeypatch.setattr(settings, "llama_mmproj_enabled", True)
    return root


def _budget_pm(root, headroom, announcements=None):
    pm = RuntimeProcessManager(
        startup_timeout=5.0,
        main_model_path=str(root / "qwen3.5-9b" / "Qwen3.5-9B-UD-IQ3_XXS.gguf"),
        fast_model_path=str(root / "qwen3.5-2b" / "Qwen3.5-2B-UD-Q4_K_XL.gguf"),
    )
    pm.vram_headroom_provider = lambda: headroom["mb"]
    if announcements is not None:
        pm.on_launch_adjusted = announcements.append
    return pm


async def _launch_and_capture_cmds(pm, model_kind, children=None):
    """Spawn with mock children (alive by default) and return every argv that was tried."""
    alive = MagicMock(); alive.poll = MagicMock(return_value=None)
    alive.stdout = None; alive.stderr = None; alive.pid = 4242

    async def healthy(timeout=None):
        proc = pm._process
        return proc is not None and proc.poll() is None

    with patch.object(pm, "health_check", side_effect=healthy), \
         patch("subprocess.Popen", side_effect=children or [alive]) as mock_spawn:
        assert await pm.ensure_running(model_kind=model_kind) is True
    return [call.args[0] for call in mock_spawn.call_args_list]


def test_launch_rungs_give_up_the_cheapest_thing_first(budget_catalog):
    pm = _budget_pm(budget_catalog, {"mb": None})
    main, _ = pm.resolve_model_path("main")
    rungs = pm._launch_rungs(main, "main")

    assert [r["step"] for r in rungs] == ["full", "no_projector", "reduced_context", "fallback_model", "fallback_model"]
    assert rungs[0]["ctx"] == 32768 and rungs[0]["projector"] is not None
    assert rungs[1]["projector"] is None and rungs[1]["ctx"] == 32768
    assert rungs[2]["ctx"] == 16384
    assert rungs[3]["model"].name == "Qwen3.5-2B-UD-Q4_K_XL.gguf" and rungs[3]["ctx"] == 8192
    assert rungs[4]["projector"] is None
    # The fast slot never falls back to another model, and 8k is the floor for context.
    fast, _ = pm.resolve_model_path("fast")
    assert [r["step"] for r in pm._launch_rungs(fast, "fast")] == ["full", "no_projector"]


@pytest.mark.asyncio
async def test_ladder_skips_the_projector_when_the_estimate_does_not_fit(budget_catalog):
    """Headroom fits the 9B text-only (~1140 MiB by the size rule at 32k) but not with its
    projector (+250): one launch, no --mmproj, the step recorded and announced once."""
    announced = []
    pm = _budget_pm(budget_catalog, {"mb": 1200.0}, announced)

    cmds = await _launch_and_capture_cmds(pm, "main")

    assert len(cmds) == 1 and "--mmproj" not in cmds[0]
    assert cmds[0][cmds[0].index("--ctx-size") + 1] == "32768"
    assert pm.has_vision("main") is False
    assert pm.served_model.name == "Qwen3.5-9B-UD-IQ3_XXS.gguf"
    adj = pm.launch_adjustment
    assert adj["step"] == "no_projector" and adj["requested_projector"] is True and adj["projector"] is False
    assert adj["headroom_mb"] == 1200
    assert [a["step"] for a in announced] == ["no_projector"]


@pytest.mark.asyncio
async def test_ladder_halves_context_before_changing_model(budget_catalog):
    announced = []
    pm = _budget_pm(budget_catalog, {"mb": 900.0}, announced)  # 16k text-only is ~820

    cmds = await _launch_and_capture_cmds(pm, "main")

    assert len(cmds) == 1
    assert cmds[0][cmds[0].index("--ctx-size") + 1] == "16384" and "--mmproj" not in cmds[0]
    assert pm.served_ctx_size == 16384
    assert pm.launch_adjustment["step"] == "reduced_context"
    assert pm.launch_adjustment["requested_ctx"] == 32768
    assert [a["step"] for a in announced] == ["reduced_context"]


@pytest.mark.asyncio
async def test_ladder_serves_main_with_the_fast_model_as_a_last_resort(budget_catalog):
    """Nothing of the 9B fits; the fast slot's file is launched under the main alias, so the
    orchestrator's "main" request is still answered -- and the swap is announced."""
    announced = []
    pm = _budget_pm(budget_catalog, {"mb": 700.0}, announced)  # fast text-only at 8k is ~660

    cmds = await _launch_and_capture_cmds(pm, "main")

    assert len(cmds) == 1
    assert Path(cmds[0][cmds[0].index("--model") + 1]).name == "Qwen3.5-2B-UD-Q4_K_XL.gguf"
    assert cmds[0][cmds[0].index("--alias") + 1] == "main"
    assert "--mmproj" not in cmds[0]
    assert pm.current_model_kind == "main"
    assert pm.served_model.name == "Qwen3.5-2B-UD-Q4_K_XL.gguf"
    assert pm.launch_adjustment["step"] == "fallback_model"
    assert pm.launch_adjustment["requested_model"] == "Qwen3.5-9B-UD-IQ3_XXS.gguf"
    assert [a["step"] for a in announced] == ["fallback_model"]
    # A request for the main slot keeps being served here without a relaunch.
    with patch.object(pm, "health_check", new=AsyncMock(return_value=True)), \
         patch("subprocess.Popen") as mock_spawn:
        assert await pm.ensure_running("main") is True
        mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_last_rung_is_tried_even_when_the_estimate_says_no_and_its_failure_is_the_error(budget_catalog):
    pm = _budget_pm(budget_catalog, {"mb": 10.0})
    dead = MagicMock(); dead.poll = MagicMock(return_value=1); dead.returncode = 1
    dead.stdout = None; dead.stderr = None; dead.pid = 1

    async def never_healthy(timeout=None):
        return False

    with patch.object(pm, "health_check", side_effect=never_healthy), \
         patch("subprocess.Popen", side_effect=[dead]) as mock_spawn, \
         pytest.raises(RuntimeError, match="exited prematurely"):
        await pm.ensure_running("main")
    assert mock_spawn.call_count == 1  # only the last rung was attempted
    assert Path(mock_spawn.call_args.args[0][mock_spawn.call_args.args[0].index("--model") + 1]).name == "Qwen3.5-2B-UD-Q4_K_XL.gguf"


@pytest.mark.asyncio
async def test_a_dead_child_steps_down_the_ladder_even_when_the_estimate_fit(budget_catalog):
    """The estimate said the full launch fits, llama-server disagreed (the real 0xC0000409 /
    'shared object initialization failed' case): the next rung is tried, not the same one."""
    announced = []
    pm = _budget_pm(budget_catalog, {"mb": 99999.0}, announced)
    dead = MagicMock(); dead.poll = MagicMock(return_value=3221226505); dead.returncode = 3221226505
    dead.stdout = None; dead.stderr = None; dead.pid = 1

    alive = MagicMock(); alive.poll = MagicMock(return_value=None)
    alive.stdout = None; alive.stderr = None; alive.pid = 4242
    cmds = await _launch_and_capture_cmds(pm, "main", children=[dead, alive])
    assert len(cmds) == 2
    assert "--mmproj" in cmds[0] and "--mmproj" not in cmds[1]
    assert [a["step"] for a in announced] == ["no_projector"]


@pytest.mark.asyncio
async def test_adjustment_is_announced_once_and_restoration_is_reported(budget_catalog):
    announced = []
    headroom = {"mb": 1200.0}
    pm = _budget_pm(budget_catalog, headroom, announced)

    await _launch_and_capture_cmds(pm, "main")
    await pm.stop()
    await _launch_and_capture_cmds(pm, "main")          # same outcome: no second announcement
    assert [a["step"] for a in announced] == ["no_projector"]

    await pm.stop()
    headroom["mb"] = 99999.0
    await _launch_and_capture_cmds(pm, "main")          # full configuration again
    assert [a["step"] for a in announced] == ["no_projector", "restored"]
    assert pm.launch_adjustment is None
    assert pm.has_vision("main") is True


@pytest.mark.asyncio
async def test_launch_cost_is_measured_recorded_and_preferred_over_the_size_rule(budget_catalog, monkeypatch):
    import app.agent.runtime_process_manager as rpm
    from app.agent.model_catalog import get_model_catalog

    readings = iter([2321.0, 7300.0])  # card before launch, card after
    monkeypatch.setattr(rpm, "_vram_used_mb", lambda: next(readings, 7300.0))
    pm = _budget_pm(budget_catalog, {"mb": 99999.0})

    await _launch_and_capture_cmds(pm, "main")

    assert pm.model_vram_mb == 4979.0
    assert pm.external_vram_baseline_mb == 2321.0
    main, _ = pm.resolve_model_path("main")
    proj = pm.resolve_projector_path(main)
    assert get_model_catalog().launch_cost(main, proj, 32768) == 4979.0
    # The learned figure now drives the estimate for that configuration only.
    assert pm.estimate_launch_cost_mb(main, proj, 32768) == 4979.0
    assert pm.estimate_launch_cost_mb(main, None, 32768) < 2000.0
    await pm.stop()
    assert pm.external_vram_baseline_mb is None


@pytest.mark.asyncio
async def test_without_a_headroom_provider_only_failures_step_down(budget_catalog):
    pm = _budget_pm(budget_catalog, {"mb": None})
    cmds = await _launch_and_capture_cmds(pm, "main")
    assert len(cmds) == 1 and "--mmproj" in cmds[0]
    assert pm.launch_adjustment is None
