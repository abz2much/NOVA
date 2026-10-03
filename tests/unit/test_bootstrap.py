"""Tests for the in-process bootstrap (v6.28.0).

The live Supervisor/pipeline effects can't run in this sandbox (no Supervisor,
no aiohttp), so these cover the orchestration *guards* (the safety gates that
protect a real HA), the run-once marker, the pipeline setup, and the in-process
Wyoming/engine logic. Module-level aiohttp imports are stubbed.
"""
import contextlib
import sys
import types
from types import SimpleNamespace

import pytest

# Stub aiohttp + the HA aiohttp client helper before bootstrap is imported.
if "aiohttp" not in sys.modules:
    _aiohttp = types.ModuleType("aiohttp")
    _aiohttp.ClientTimeout = lambda **kw: None
    _aiohttp.ClientSession = object
    sys.modules["aiohttp"] = _aiohttp


@pytest.fixture
def bootstrap(load):
    return load("bootstrap")


@pytest.fixture
def nova_config(load):
    return load("nova_config")


@pytest.fixture(autouse=True)
def _isolate_fs(tmp_path, monkeypatch, bootstrap):
    from pathlib import Path
    monkeypatch.setattr(bootstrap, "MARKER_PATH", Path(tmp_path / ".bootstrap_done"))
    monkeypatch.setattr(bootstrap, "PIPER_DIR", Path(tmp_path / "piper"))
    yield


# ── supervisor detection ─────────────────────────────────────────────────────

def test_is_supervised_reflects_token(bootstrap, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert bootstrap.is_supervised() is False
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    assert bootstrap.is_supervised() is True
    assert bootstrap.supervisor_token() == "tok"


# ── run-once marker ──────────────────────────────────────────────────────────

def test_marker_roundtrip(bootstrap):
    assert bootstrap._read_marker() == {}
    bootstrap._write_marker("6.28.0", {"addons_ok": True})
    m = bootstrap._read_marker()
    assert m["version"] == "6.28.0" and m["addons_ok"] is True


# ── no voice download ────────────────────────────────────────────────────────

def test_bootstrap_has_no_voice_download(bootstrap):
    """Nova uses Home Assistant's default voice; nothing is fetched from
    anywhere, and no voice file is ever written."""
    for name in ("HF_BASE", "HF_REVISION", "EXPECTED_SHA256",
                 "_download_voice", "_download_file", "resolve_installed_quality"):
        assert not hasattr(bootstrap, name)


# ── async_run_bootstrap guards (the safety gates) ────────────────────────────

@pytest.mark.asyncio
async def test_skips_when_flag_disabled(bootstrap, nova_config, fake_hass, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setattr(nova_config, "get",
                        lambda k, d=None: False if k == "auto_bootstrap" else d)
    status = await bootstrap.async_run_bootstrap(fake_hass)
    assert status["skipped"] == "auto_bootstrap disabled"
    assert status["addons_ok"] is False


@pytest.mark.asyncio
async def test_skips_without_supervisor(bootstrap, nova_config, fake_hass, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(nova_config, "get", lambda k, d=None: d)
    status = await bootstrap.async_run_bootstrap(fake_hass)
    assert status["supervised"] is False
    assert "Supervisor" in status["skipped"]


@pytest.mark.asyncio
async def test_skips_when_already_bootstrapped(bootstrap, nova_config, fake_hass, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setattr(nova_config, "get", lambda k, d=None: d)
    version = bootstrap._current_version()
    bootstrap._write_marker(version, {"addons_ok": True})
    status = await bootstrap.async_run_bootstrap(fake_hass)
    assert status["skipped"] == "already bootstrapped this version"


@pytest.mark.asyncio
async def test_force_overrides_marker(bootstrap, nova_config, fake_hass, monkeypatch):
    # force=True must NOT early-return on the marker; it should proceed past the
    # marker gate (and then do real work, which we cut short by failing addons).
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setattr(nova_config, "get", lambda k, d=None: d)
    bootstrap._write_marker(bootstrap._current_version(), {"addons_ok": True})

    async def _no_addon(hass, slug, friendly):
        return False
    monkeypatch.setattr(bootstrap, "_ensure_addon", _no_addon)

    monkeypatch.setattr(bootstrap, "_reload_wyoming",
                        lambda hass: _async_return(0))
    monkeypatch.setattr(bootstrap, "_wait_for_agent",
                        lambda hass, **kw: _async_return(None))

    status = await bootstrap.async_run_bootstrap(fake_hass, force=True)
    assert status["skipped"] is None          # did not skip on marker
    assert status["addons_ok"] is False        # ran phase 1


# ── in-process Wyoming reload ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reload_wyoming_reloads_each_entry(bootstrap):
    reloaded = []

    class _Entries:
        def async_entries(self, domain):
            assert domain == "wyoming"
            return [SimpleNamespace(entry_id="w1"), SimpleNamespace(entry_id="w2")]

        async def async_reload(self, eid):
            reloaded.append(eid)

    hass = SimpleNamespace(config_entries=_Entries())
    n = await bootstrap._reload_wyoming(hass)
    assert n == 2 and reloaded == ["w1", "w2"]


# ── engine / agent discovery ─────────────────────────────────────────────────

def test_find_engine_prefers_hint(bootstrap, fake_hass):
    fake_hass.states.set("stt.faster_whisper", "idle")
    fake_hass.states.set("stt.something_else", "idle")
    assert bootstrap._find_engine(fake_hass, "stt", "whisper") == "stt.faster_whisper"


def test_find_engine_falls_back_to_first(bootstrap, fake_hass):
    fake_hass.states.set("tts.piper", "idle")
    assert bootstrap._find_engine(fake_hass, "tts", "piper") == "tts.piper"
    # no match for hint → first available
    assert bootstrap._find_engine(fake_hass, "tts", "nope") == "tts.piper"


def _async_return(value):
    async def _coro():
        return value
    return _coro()


# ── _create_pipeline — behavioural (not source-inspection) ──────────────────

class _FakePipeline:
    def __init__(self, name="Nova", conversation_engine=None, tts_voice=None):
        self.name = name
        self.conversation_engine = conversation_engine
        self.tts_voice = tts_voice


class _FakeAssistPipelineModule:
    """Minimal stand-in for homeassistant.components.assist_pipeline."""

    def __init__(self, existing=None, create_result=None, update_raises_first=False):
        self._existing = existing or []
        self._create_result = create_result
        self.updates: list[dict] = []
        self._update_raises_first = update_raises_first
        self._raised_once = False

    def async_get_pipelines(self, hass):
        return list(self._existing)

    async def async_create_default_pipeline(self, hass, stt_engine_id, tts_engine_id, pipeline_name):
        return self._create_result

    async def async_update_pipeline(self, hass, pipeline, **kwargs):
        if self._update_raises_first and not self._raised_once:
            self._raised_once = True
            raise RuntimeError("tts_voice rejected on this HA version")
        self.updates.append(kwargs)
        for k, v in kwargs.items():
            setattr(pipeline, k, v)


@contextlib.contextmanager
def _install_assist_pipeline(fake):
    components = sys.modules["homeassistant.components"]
    sys.modules["homeassistant.components.assist_pipeline"] = fake
    components.assist_pipeline = fake
    try:
        yield fake
    finally:
        del sys.modules["homeassistant.components.assist_pipeline"]
        del components.assist_pipeline


def _hass_with_agent_and_engines(fake_hass):
    fake_hass.states.set("conversation.nova", "idle")
    fake_hass.states.set("stt.faster_whisper", "idle")
    fake_hass.states.set("tts.piper", "idle")
    return fake_hass


async def test_create_pipeline_new_sets_agent_only(bootstrap, fake_hass):
    _hass_with_agent_and_engines(fake_hass)
    created = _FakePipeline(name="Nova")
    fake = _FakeAssistPipelineModule(existing=[], create_result=created)
    with _install_assist_pipeline(fake):
        ok = await bootstrap._create_pipeline(fake_hass)
    assert ok is True
    assert fake.updates == [{"conversation_engine": "conversation.nova"}]
    assert created.tts_voice is None  # Home Assistant's default voice


async def test_create_pipeline_existing_clears_missing_nova_voice(bootstrap, fake_hass, monkeypatch):
    """A pipeline left pointing at the old Nova voice, whose file is gone,
    falls back to Home Assistant's default voice."""
    _hass_with_agent_and_engines(fake_hass)
    existing = _FakePipeline(name="Nova", conversation_engine="conversation.nova",
                              tts_voice="en_GB-nova-high")
    fake = _FakeAssistPipelineModule(existing=[existing])
    monkeypatch.setattr(bootstrap, "_legacy_voice_present", lambda v: False)
    with _install_assist_pipeline(fake):
        ok = await bootstrap._create_pipeline(fake_hass)
    assert ok is True
    assert fake.updates == [{"tts_voice": None}]
    assert existing.tts_voice is None


async def test_create_pipeline_existing_keeps_nova_voice_when_file_present(bootstrap, fake_hass, monkeypatch):
    _hass_with_agent_and_engines(fake_hass)
    existing = _FakePipeline(name="Nova", conversation_engine="conversation.nova",
                              tts_voice="en_GB-nova-high")
    fake = _FakeAssistPipelineModule(existing=[existing])
    monkeypatch.setattr(bootstrap, "_legacy_voice_present", lambda v: True)
    with _install_assist_pipeline(fake):
        ok = await bootstrap._create_pipeline(fake_hass)
    assert ok is True
    assert fake.updates == []
    assert existing.tts_voice == "en_GB-nova-high"


async def test_create_pipeline_existing_leaves_other_voice_alone(bootstrap, fake_hass):
    _hass_with_agent_and_engines(fake_hass)
    existing = _FakePipeline(name="Nova", conversation_engine="conversation.nova",
                              tts_voice="en_GB-alba-medium")
    fake = _FakeAssistPipelineModule(existing=[existing])
    with _install_assist_pipeline(fake):
        ok = await bootstrap._create_pipeline(fake_hass)
    assert ok is True
    assert fake.updates == []


async def test_create_pipeline_existing_repairs_agent(bootstrap, fake_hass):
    _hass_with_agent_and_engines(fake_hass)
    existing = _FakePipeline(name="Nova", conversation_engine="conversation.other",
                              tts_voice=None)
    fake = _FakeAssistPipelineModule(existing=[existing])
    with _install_assist_pipeline(fake):
        ok = await bootstrap._create_pipeline(fake_hass)
    assert ok is True
    assert fake.updates == [{"conversation_engine": "conversation.nova"}]


# ── async_run_bootstrap — builds the pipeline without a voice ───────────────

@pytest.mark.asyncio
async def test_run_bootstrap_builds_pipeline_without_voice(bootstrap, nova_config, fake_hass, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    cfg = {"auto_pipeline": True}
    monkeypatch.setattr(nova_config, "get", lambda k, d=None: cfg.get(k, d))

    async def _fake_ensure_addon(hass, slug, friendly):
        return True
    monkeypatch.setattr(bootstrap, "_ensure_addon", _fake_ensure_addon)
    monkeypatch.setattr(bootstrap, "_reload_wyoming", lambda hass: _async_return(0))
    monkeypatch.setattr(bootstrap, "_wait_for_agent", lambda hass, **kw: _async_return("conversation.nova"))

    async def _fake_create_pipeline(hass):
        return True
    monkeypatch.setattr(bootstrap, "_create_pipeline", _fake_create_pipeline)

    status = await bootstrap.async_run_bootstrap(fake_hass)
    assert status["addons_ok"] is True
    assert status["pipeline_ok"] is True
    assert "voice_ok" not in status


def test_ensure_pipeline_agent_repairs_by_name_or_voice(bootstrap):
    """The every-startup repair must catch a Nova pipeline by name OR its Nova
    voice, skip when already correct, set the agent via async_update_pipeline, and
    run outside the Supervisor/marker gate (so it can't be raced or skipped)."""
    import inspect
    src = inspect.getsource(bootstrap.async_ensure_pipeline_agent)
    assert "async_update_pipeline" in src
    assert "conversation_engine=agent" in src
    assert "tts_voice" in src               # matches by Nova voice too
    assert "== agent" in src                # idempotent: skip when already right
    sched = inspect.getsource(bootstrap.schedule_bootstrap)
    assert "async_ensure_pipeline_agent" in sched   # wired to run every start


# ── Voice setup Repair notices (v8.6.0) ──────────────────────────────────────

class _Notices:
    def __init__(self):
        self.calls = []

    def note_voice_setup_step(self, hass, step, number, total):
        self.calls.append(("step", step, number, total))

    def note_voice_setup_incomplete(self, hass, failed):
        self.calls.append(("incomplete", list(failed)))

    def clear_voice_setup(self, hass):
        self.calls.append(("clear",))


def _wire_run(bootstrap, nova_config, monkeypatch, load, *, addons=True, pipeline=True, cfg=None):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    cfg = {"auto_pipeline": True, **(cfg or {})}
    monkeypatch.setattr(nova_config, "get", lambda k, d=None: cfg.get(k, d))
    rn = load("repair_notices")
    notices = _Notices()
    for name in ("note_voice_setup_step", "note_voice_setup_incomplete", "clear_voice_setup"):
        monkeypatch.setattr(rn, name, getattr(notices, name))

    async def _addon(hass, slug, friendly):
        return addons
    monkeypatch.setattr(bootstrap, "_ensure_addon", _addon)
    monkeypatch.setattr(bootstrap, "_reload_wyoming", lambda hass: _async_return(0))
    monkeypatch.setattr(bootstrap, "_wait_for_agent", lambda hass, **kw: _async_return("conversation.nova"))

    async def _pipe(hass):
        return pipeline
    monkeypatch.setattr(bootstrap, "_create_pipeline", _pipe)
    return notices


async def test_first_run_shows_each_step_then_clears(bootstrap, nova_config, fake_hass, monkeypatch, load):
    notices = _wire_run(bootstrap, nova_config, monkeypatch, load)
    await bootstrap.async_run_bootstrap(fake_hass)
    assert notices.calls == [("step", "addons", 1, 3), ("step", "wyoming", 2, 3),
                             ("step", "pipeline", 3, 3), ("clear",)]


async def test_first_run_without_auto_pipeline_has_two_steps(bootstrap, nova_config, fake_hass, monkeypatch, load):
    notices = _wire_run(bootstrap, nova_config, monkeypatch, load, cfg={"auto_pipeline": False})
    await bootstrap.async_run_bootstrap(fake_hass)
    assert notices.calls == [("step", "addons", 1, 2), ("step", "wyoming", 2, 2), ("clear",)]


async def test_failures_raise_incomplete_naming_parts(bootstrap, nova_config, fake_hass, monkeypatch, load):
    notices = _wire_run(bootstrap, nova_config, monkeypatch, load, addons=False, pipeline=False)
    await bootstrap.async_run_bootstrap(fake_hass)
    assert notices.calls[-1] == ("incomplete", ["Piper, Whisper, openWakeWord", "Assist pipeline"])


async def test_version_update_rerun_shows_no_progress(bootstrap, nova_config, fake_hass, monkeypatch, load):
    bootstrap._write_marker("0.0.1", {"addons_ok": True, "pipeline_ok": True})
    notices = _wire_run(bootstrap, nova_config, monkeypatch, load)
    await bootstrap.async_run_bootstrap(fake_hass)
    assert notices.calls == [("clear",)]


async def test_skip_rechecks_failed_pipeline_live(bootstrap, nova_config, fake_hass, monkeypatch, load):
    bootstrap._write_marker(bootstrap._current_version(),
                            {"addons_ok": True, "pipeline_tried": True, "pipeline_ok": False})
    notices = _wire_run(bootstrap, nova_config, monkeypatch, load)
    sh = load("setup_health")
    monkeypatch.setattr(sh, "_check_assist_pipeline", lambda hass: {"status": "warn"})
    status = await bootstrap.async_run_bootstrap(fake_hass)
    assert status["skipped"] == "already bootstrapped this version"
    assert notices.calls == [("incomplete", ["Assist pipeline"])]

    notices.calls.clear()
    monkeypatch.setattr(sh, "_check_assist_pipeline", lambda hass: {"status": "ok"})
    await bootstrap.async_run_bootstrap(fake_hass)
    assert notices.calls == [("clear",)]


async def test_no_notices_without_supervisor(bootstrap, nova_config, fake_hass, monkeypatch, load):
    notices = _wire_run(bootstrap, nova_config, monkeypatch, load)
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    await bootstrap.async_run_bootstrap(fake_hass)
    assert notices.calls == []
