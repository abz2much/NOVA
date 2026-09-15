"""Tests for the in-process bootstrap (v6.28.0).

The live Supervisor/voice/pipeline effects can't run in this sandbox (no
Supervisor, no aiohttp), so these cover the orchestration *guards* (the safety
gates that protect a real HA), the run-once marker, the voice short-circuit, and
the in-process Wyoming/engine logic. Module-level aiohttp imports are stubbed.
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
if "homeassistant.helpers.aiohttp_client" not in sys.modules:
    _ac = types.ModuleType("homeassistant.helpers.aiohttp_client")
    _ac.async_get_clientsession = lambda hass: None
    sys.modules["homeassistant.helpers.aiohttp_client"] = _ac


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
    bootstrap._write_marker("6.28.0", {"addons_ok": True, "voice_ok": True})
    m = bootstrap._read_marker()
    assert m["version"] == "6.28.0" and m["addons_ok"] is True


# ── voice short-circuit (no network) ─────────────────────────────────────────

def test_voice_present_detects_existing(bootstrap):
    q = "medium"
    bootstrap.PIPER_DIR.mkdir(parents=True, exist_ok=True)
    onnx = bootstrap.PIPER_DIR / f"en_GB-nova-{q}.onnx"
    js = bootstrap.PIPER_DIR / f"en_GB-nova-{q}.onnx.json"
    onnx.write_bytes(b"x" * (bootstrap.MIN_ONNX_SIZE + 10))
    js.write_text("{}")
    assert bootstrap._voice_present(q) is True
    assert bootstrap._voice_present("high") is False  # only medium present


# ── voice download checksum verification (v7.87.0) ───────────────────────────

class _FakeResp:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self._status = status

    def get(self, url, timeout=None):
        return _FakeResp(self._status, self._body)


def test_download_accepts_matching_checksum(bootstrap, fake_hass, monkeypatch, tmp_path):
    body = b"totally real onnx bytes"
    digest = bootstrap._sha256(body)
    monkeypatch.setattr(bootstrap, "async_get_clientsession", lambda hass: _FakeSession(body))
    monkeypatch.setattr(bootstrap, "EXPECTED_SHA256", {"voice.onnx": digest})
    dest = tmp_path / "voice.onnx"

    import asyncio
    n = asyncio.run(bootstrap._download_file(fake_hass, "https://example/voice.onnx", dest))
    assert n == len(body)
    assert dest.read_bytes() == body


def test_download_rejects_checksum_mismatch(bootstrap, fake_hass, monkeypatch, tmp_path):
    body = b"tampered or wrong-revision bytes"
    monkeypatch.setattr(bootstrap, "async_get_clientsession", lambda hass: _FakeSession(body))
    monkeypatch.setattr(bootstrap, "EXPECTED_SHA256", {"voice.onnx": "0" * 64})
    dest = tmp_path / "voice.onnx"

    import asyncio
    n = asyncio.run(bootstrap._download_file(fake_hass, "https://example/voice.onnx", dest))
    assert n == 0
    assert not dest.exists()  # never written, not partially trusted


def test_download_without_pinned_checksum_still_succeeds(bootstrap, fake_hass, monkeypatch, tmp_path):
    """Backward compatibility: EXPECTED_SHA256 is empty until real hashes are
    captured (this sandbox can't reach huggingface.co to compute them) — a
    fresh install must still work, verified by size alone same as before."""
    body = b"some voice bytes, unpinned"
    monkeypatch.setattr(bootstrap, "async_get_clientsession", lambda hass: _FakeSession(body))
    monkeypatch.setattr(bootstrap, "EXPECTED_SHA256", {})
    dest = tmp_path / "voice.onnx"

    import asyncio
    n = asyncio.run(bootstrap._download_file(fake_hass, "https://example/voice.onnx", dest))
    assert n == len(body)
    assert dest.read_bytes() == body


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
    bootstrap._write_marker(version, {"addons_ok": True, "voice_ok": True})
    status = await bootstrap.async_run_bootstrap(fake_hass)
    assert status["skipped"] == "already bootstrapped this version"


@pytest.mark.asyncio
async def test_force_overrides_marker(bootstrap, nova_config, fake_hass, monkeypatch):
    # force=True must NOT early-return on the marker; it should proceed past the
    # marker gate (and then do real work, which we cut short by failing addons).
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setattr(nova_config, "get", lambda k, d=None: d)
    bootstrap._write_marker(bootstrap._current_version(), {"addons_ok": True, "voice_ok": True})

    async def _no_addon(hass, slug, friendly):
        return False
    monkeypatch.setattr(bootstrap, "_ensure_addon", _no_addon)

    async def _no_voice(hass, quality):
        return None
    monkeypatch.setattr(bootstrap, "_download_voice", _no_voice)
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


# ── voice-quality resolver (v7.102.x — shared by bootstrap + tts_helper) ────

def test_resolve_installed_quality_prefers_requested(bootstrap):
    bootstrap.PIPER_DIR.mkdir(parents=True, exist_ok=True)
    for q in ("high", "medium"):
        (bootstrap.PIPER_DIR / f"en_GB-nova-{q}.onnx").write_bytes(
            b"x" * (bootstrap.MIN_ONNX_SIZE + 10))
        (bootstrap.PIPER_DIR / f"en_GB-nova-{q}.onnx.json").write_text("{}")
    assert bootstrap.resolve_installed_quality("high") == "high"
    assert bootstrap.resolve_installed_quality("medium") == "medium"


def test_resolve_installed_quality_falls_back_to_other(bootstrap):
    bootstrap.PIPER_DIR.mkdir(parents=True, exist_ok=True)
    (bootstrap.PIPER_DIR / "en_GB-nova-medium.onnx").write_bytes(
        b"x" * (bootstrap.MIN_ONNX_SIZE + 10))
    (bootstrap.PIPER_DIR / "en_GB-nova-medium.onnx.json").write_text("{}")
    assert bootstrap.resolve_installed_quality("high") == "medium"


def test_resolve_installed_quality_none_when_nothing_present(bootstrap):
    assert bootstrap.resolve_installed_quality("high") is None


# ── _download_voice — returns the real quality, not just success/failure ────

@pytest.mark.asyncio
async def test_download_voice_short_circuits_when_requested_present(bootstrap, fake_hass, monkeypatch):
    bootstrap.PIPER_DIR.mkdir(parents=True, exist_ok=True)
    (bootstrap.PIPER_DIR / "en_GB-nova-high.onnx").write_bytes(
        b"x" * (bootstrap.MIN_ONNX_SIZE + 10))
    (bootstrap.PIPER_DIR / "en_GB-nova-high.onnx.json").write_text("{}")

    async def _boom(hass, quality):
        raise AssertionError("must not attempt a download when already installed")
    monkeypatch.setattr(bootstrap, "_try_quality", _boom)

    assert await bootstrap._download_voice(fake_hass, "high") == "high"


@pytest.mark.asyncio
async def test_download_voice_attempts_requested_even_when_fallback_installed(bootstrap, fake_hass, monkeypatch):
    """Only medium is on disk (e.g. left over from a previous fallback) and the
    configured quality is high — an already-present fallback must NOT stop
    Nova from attempting to get the quality actually requested."""
    bootstrap.PIPER_DIR.mkdir(parents=True, exist_ok=True)
    (bootstrap.PIPER_DIR / "en_GB-nova-medium.onnx").write_bytes(
        b"x" * (bootstrap.MIN_ONNX_SIZE + 10))
    (bootstrap.PIPER_DIR / "en_GB-nova-medium.onnx.json").write_text("{}")

    calls = []

    async def _try(hass, quality):
        calls.append(quality)
        return quality == "high"  # the requested quality is downloadable
    monkeypatch.setattr(bootstrap, "_try_quality", _try)

    assert await bootstrap._download_voice(fake_hass, "high") == "high"
    assert calls == ["high"]  # attempted despite medium already being on disk


async def test_download_voice_falls_back_to_already_present_when_requested_download_fails(
        bootstrap, fake_hass, monkeypatch):
    """Requested quality isn't installed and its download fails, but the
    fallback quality is already on disk — must return it without attempting a
    further (redundant) download for it."""
    bootstrap.PIPER_DIR.mkdir(parents=True, exist_ok=True)
    (bootstrap.PIPER_DIR / "en_GB-nova-medium.onnx").write_bytes(
        b"x" * (bootstrap.MIN_ONNX_SIZE + 10))
    (bootstrap.PIPER_DIR / "en_GB-nova-medium.onnx.json").write_text("{}")

    async def _try(hass, quality):
        if quality == "medium":
            raise AssertionError("must not attempt to download an already-present fallback")
        return False  # requested quality's download fails
    monkeypatch.setattr(bootstrap, "_try_quality", _try)

    assert await bootstrap._download_voice(fake_hass, "high") == "medium"


@pytest.mark.asyncio
async def test_download_voice_downloads_requested_quality(bootstrap, fake_hass, monkeypatch):
    calls = []

    async def _try(hass, quality):
        calls.append(quality)
        return quality == "high"
    monkeypatch.setattr(bootstrap, "_try_quality", _try)

    assert await bootstrap._download_voice(fake_hass, "high") == "high"
    assert calls == ["high"]


@pytest.mark.asyncio
async def test_download_voice_falls_back_when_requested_not_hosted(bootstrap, fake_hass, monkeypatch):
    calls = []

    async def _try(hass, quality):
        calls.append(quality)
        return quality == "medium"
    monkeypatch.setattr(bootstrap, "_try_quality", _try)

    assert await bootstrap._download_voice(fake_hass, "high") == "medium"
    assert calls == ["high", "medium"]


@pytest.mark.asyncio
async def test_download_voice_returns_none_when_both_fail(bootstrap, fake_hass, monkeypatch):
    async def _try(hass, quality):
        return False
    monkeypatch.setattr(bootstrap, "_try_quality", _try)

    assert await bootstrap._download_voice(fake_hass, "high") is None


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


async def test_create_pipeline_new_sets_agent_and_voice(bootstrap, fake_hass):
    _hass_with_agent_and_engines(fake_hass)
    created = _FakePipeline(name="Nova")
    fake = _FakeAssistPipelineModule(existing=[], create_result=created)
    with _install_assist_pipeline(fake):
        ok = await bootstrap._create_pipeline(fake_hass, "medium")
    assert ok is True
    assert fake.updates == [{"conversation_engine": "conversation.nova",
                             "tts_voice": "en_GB-nova-medium"}]


async def test_create_pipeline_new_falls_back_when_tts_voice_rejected(bootstrap, fake_hass):
    """Compatibility fallback: some HA versions can reject tts_voice on
    creation — the pipeline must still get its conversation agent set rather
    than being left on HA's default."""
    _hass_with_agent_and_engines(fake_hass)
    created = _FakePipeline(name="Nova")
    fake = _FakeAssistPipelineModule(existing=[], create_result=created, update_raises_first=True)
    with _install_assist_pipeline(fake):
        ok = await bootstrap._create_pipeline(fake_hass, "medium")
    assert ok is True
    assert fake.updates == [{"conversation_engine": "conversation.nova"}]
    assert created.conversation_engine == "conversation.nova"


async def test_create_pipeline_existing_repairs_missing_voice(bootstrap, fake_hass, monkeypatch):
    _hass_with_agent_and_engines(fake_hass)
    existing = _FakePipeline(name="Nova", conversation_engine="conversation.nova",
                              tts_voice="en_GB-nova-high")
    fake = _FakeAssistPipelineModule(existing=[existing])
    monkeypatch.setattr(bootstrap, "_voice_present", lambda q: q == "medium")  # high missing
    with _install_assist_pipeline(fake):
        ok = await bootstrap._create_pipeline(fake_hass, "medium")
    assert ok is True
    assert fake.updates == [{"tts_voice": "en_GB-nova-medium"}]
    assert existing.tts_voice == "en_GB-nova-medium"


async def test_create_pipeline_existing_no_clobber_when_current_voice_present(bootstrap, fake_hass, monkeypatch):
    """A deliberately-chosen voice that's still valid on disk must never be
    overwritten just because it differs from the configured quality."""
    _hass_with_agent_and_engines(fake_hass)
    existing = _FakePipeline(name="Nova", conversation_engine="conversation.nova",
                              tts_voice="en_GB-nova-high")
    fake = _FakeAssistPipelineModule(existing=[existing])
    monkeypatch.setattr(bootstrap, "_voice_present", lambda q: True)  # both present
    with _install_assist_pipeline(fake):
        ok = await bootstrap._create_pipeline(fake_hass, "medium")
    assert ok is True
    assert fake.updates == []
    assert existing.tts_voice == "en_GB-nova-high"


async def test_create_pipeline_existing_no_repair_when_fallback_also_missing(bootstrap, fake_hass, monkeypatch):
    """Can't repoint to a quality that isn't there either — leave it alone."""
    _hass_with_agent_and_engines(fake_hass)
    existing = _FakePipeline(name="Nova", conversation_engine="conversation.nova",
                              tts_voice="en_GB-nova-high")
    fake = _FakeAssistPipelineModule(existing=[existing])
    monkeypatch.setattr(bootstrap, "_voice_present", lambda q: False)  # neither present
    with _install_assist_pipeline(fake):
        ok = await bootstrap._create_pipeline(fake_hass, "medium")
    assert ok is True
    assert fake.updates == []
    assert existing.tts_voice == "en_GB-nova-high"


async def test_create_pipeline_existing_agent_repair_unaffected_by_voice_logic(bootstrap, fake_hass, monkeypatch):
    """Pre-existing conversation-engine repair must still work when there's no
    voice mismatch to consider."""
    _hass_with_agent_and_engines(fake_hass)
    existing = _FakePipeline(name="Nova", conversation_engine="conversation.other",
                              tts_voice="en_GB-nova-medium")
    fake = _FakeAssistPipelineModule(existing=[existing])
    monkeypatch.setattr(bootstrap, "_voice_present", lambda q: True)
    with _install_assist_pipeline(fake):
        ok = await bootstrap._create_pipeline(fake_hass, "medium")
    assert ok is True
    assert fake.updates == [{"conversation_engine": "conversation.nova"}]


# ── async_run_bootstrap — passes the *installed* quality to the pipeline ────

@pytest.mark.asyncio
async def test_run_bootstrap_passes_installed_quality_to_pipeline(bootstrap, nova_config, fake_hass, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    cfg = {"voice_quality": "high", "tts_provider": "piper_nova", "auto_pipeline": True}
    monkeypatch.setattr(nova_config, "get", lambda k, d=None: cfg.get(k, d))

    async def _fake_ensure_addon(hass, slug, friendly):
        return True
    monkeypatch.setattr(bootstrap, "_ensure_addon", _fake_ensure_addon)

    async def _fake_download_voice(hass, quality):
        assert quality == "high"
        return "medium"  # fell back to the other quality
    monkeypatch.setattr(bootstrap, "_download_voice", _fake_download_voice)

    monkeypatch.setattr(bootstrap, "_addon_action", lambda *a, **k: _async_return(True))
    monkeypatch.setattr(bootstrap, "_wait_addon_state", lambda *a, **k: _async_return(True))
    monkeypatch.setattr(bootstrap.asyncio, "sleep", lambda *a, **k: _async_return(None))
    monkeypatch.setattr(bootstrap, "_reload_wyoming", lambda hass: _async_return(0))
    monkeypatch.setattr(bootstrap, "_wait_for_agent", lambda hass, **kw: _async_return("conversation.nova"))

    captured = {}

    async def _fake_create_pipeline(hass, voice_quality):
        captured["voice_quality"] = voice_quality
        return True
    monkeypatch.setattr(bootstrap, "_create_pipeline", _fake_create_pipeline)

    status = await bootstrap.async_run_bootstrap(fake_hass)
    assert status["voice_ok"] is True
    assert captured["voice_quality"] == "medium"


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
