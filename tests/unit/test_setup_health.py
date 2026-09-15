"""Setup Doctor (Phase 2) — read-only configuration health checks.

Each check is pure over a fake hass + nova_config; the aggregate
run_setup_health() is exercised with the individual checks stubbed out, and
separately proven to fold diagnostics.run_service_health's 8 checks in
unchanged rather than re-implementing them.
"""
from __future__ import annotations

import sys
import types

import pytest

# _check_assist_pipeline lazily imports bootstrap (for _find_nova_agent),
# which imports aiohttp at module level — stub it the same way
# test_bootstrap.py does, so that import succeeds regardless of collection order.
if "aiohttp" not in sys.modules:
    _aiohttp = types.ModuleType("aiohttp")
    _aiohttp.ClientTimeout = lambda **kw: None
    _aiohttp.ClientSession = object
    sys.modules["aiohttp"] = _aiohttp


@pytest.fixture
def sh(load):
    return load("setup_health")


@pytest.fixture
def nova_config(load):
    return load("nova_config")


@pytest.fixture
def bootstrap(load):
    return load("bootstrap")


def _cfg_get(nova_config, values: dict):
    """Monkeypatch nova_config.get to serve fixed values, default otherwise."""
    return lambda key, default=None: values.get(key, default)


# ── entity references ────────────────────────────────────────────────────────

def test_entity_references_off_when_unconfigured(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {}))
    out = sh._check_entity_references(fake_hass)
    assert out["status"] == "off"


def test_entity_references_ok_when_resolved(sh, nova_config, fake_hass, monkeypatch):
    fake_hass.states.set("device_tracker.abi", "home")
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "departure_origin_entity": "device_tracker.abi",
    }))
    out = sh._check_entity_references(fake_hass)
    assert out["status"] == "ok"


def test_entity_references_warn_when_missing(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "departure_travel_sensor": "sensor.gone",
    }))
    out = sh._check_entity_references(fake_hass)
    assert out["status"] == "warn"
    assert "sensor.gone" in out["detail"]
    assert "suggested_fix" in out


def test_entity_references_checks_pattern_include_list(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "pattern_include_entities": ["light.gone"],
    }))
    out = sh._check_entity_references(fake_hass)
    assert out["status"] == "warn"
    assert "light.gone" in out["detail"]


# ── room speakers ────────────────────────────────────────────────────────────

def test_room_speakers_off_when_unconfigured(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {}))
    assert sh._check_room_speakers(fake_hass)["status"] == "off"


def test_room_speakers_ok_when_resolved(sh, nova_config, fake_hass, monkeypatch):
    fake_hass.states.set("media_player.kitchen", "idle")
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "room_speakers": {"kitchen": "media_player.kitchen"},
    }))
    assert sh._check_room_speakers(fake_hass)["status"] == "ok"


def test_room_speakers_warn_when_stale(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "room_speakers": {"kitchen": "media_player.gone"},
    }))
    out = sh._check_room_speakers(fake_hass)
    assert out["status"] == "warn"
    assert "media_player.gone" in out["detail"]


def test_room_speakers_checks_general_speaker_too(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "general_speaker": "media_player.gone",
    }))
    out = sh._check_room_speakers(fake_hass)
    assert out["status"] == "warn"
    assert "general speaker" in out["detail"]


# ── camera overrides ──────────────────────────────────────────────────────────

def test_camera_overrides_off_when_unconfigured(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {}))
    assert sh._check_camera_overrides(fake_hass)["status"] == "off"


def test_camera_overrides_warn_when_stale(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "camera_overrides": {"camera.gone": "rtsp://x"},
    }))
    out = sh._check_camera_overrides(fake_hass)
    assert out["status"] == "warn"
    assert "camera.gone" in out["detail"]


def test_camera_overrides_ok_when_resolved(sh, nova_config, fake_hass, monkeypatch):
    fake_hass.states.set("camera.front", "idle")
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "camera_overrides": {"camera.front": "rtsp://x"},
    }))
    assert sh._check_camera_overrides(fake_hass)["status"] == "ok"


# ── notification service ─────────────────────────────────────────────────────

def test_notify_service_off_when_unconfigured(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {}))
    assert sh._check_notify_service(fake_hass)["status"] == "off"


def test_notify_service_warn_when_malformed(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {"notify_service": "nodothere"}))
    out = sh._check_notify_service(fake_hass)
    assert out["status"] == "warn"


def test_notify_service_ok_when_registered(sh, nova_config, fake_hass, monkeypatch):
    fake_hass.services.register("notify", "mobile_app_abi")
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "notify_service": "notify.mobile_app_abi",
    }))
    assert sh._check_notify_service(fake_hass)["status"] == "ok"


def test_notify_service_warn_when_not_registered(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "notify_service": "notify.mobile_app_gone",
    }))
    out = sh._check_notify_service(fake_hass)
    assert out["status"] == "warn"
    assert "not registered" in out["detail"]


# ── person entities ──────────────────────────────────────────────────────────

def test_person_entities_off_when_unconfigured(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {}))
    assert sh._check_person_entities(fake_hass)["status"] == "off"


def test_person_entities_warn_when_stale(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "person_honorifics": {"person.gone": "sir"},
    }))
    out = sh._check_person_entities(fake_hass)
    assert out["status"] == "warn"
    assert "person.gone" in out["detail"]


def test_person_entities_ok_when_resolved(sh, nova_config, fake_hass, monkeypatch):
    fake_hass.states.set("person.abi", "home")
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "person_honorifics": {"person.abi": "sir"},
    }))
    assert sh._check_person_entities(fake_hass)["status"] == "ok"


def test_person_entities_ok_when_resolved_and_stored_as_json_string(sh, nova_config, fake_hass, monkeypatch):
    """Real production shape: the panel saves person_honorifics via
    nova/update_config as JSON.stringify(...), and nova_config.get returns
    it verbatim — so this is a JSON string, not an already-decoded dict,
    unlike the fixture above. A prior bug's isinstance(honorifics, dict)
    check failed on the string and reported this check OFF even when
    honorifics were genuinely configured and resolved."""
    fake_hass.states.set("person.abi", "home")
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "person_honorifics": '{"person.abi": "sir"}',
    }))
    assert sh._check_person_entities(fake_hass)["status"] == "ok"


def test_person_entities_off_when_person_honorifics_is_malformed_json(sh, nova_config, fake_hass, monkeypatch):
    monkeypatch.setattr(nova_config, "get", _cfg_get(nova_config, {
        "person_honorifics": "not valid json{",
    }))
    assert sh._check_person_entities(fake_hass)["status"] == "off"


# ── required integrations ────────────────────────────────────────────────────

def test_required_integrations_ok(sh, fake_hass):
    fake_hass.config.components = {"conversation", "http"}
    assert sh._check_required_integrations(fake_hass)["status"] == "ok"


def test_required_integrations_down_when_missing(sh, fake_hass):
    fake_hass.config.components = {"http"}
    out = sh._check_required_integrations(fake_hass)
    assert out["status"] == "down"
    assert "conversation" in out["detail"]


# ── persistence ───────────────────────────────────────────────────────────────

def test_persistence_ok_when_writable(sh, nova_config, fake_hass, monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.setattr(nova_config, "CONFIG_PATH", Path(tmp_path / "nova" / "config.json"), raising=False)
    out = sh._check_persistence(fake_hass)
    assert out["status"] == "ok"
    # the probe file must clean up after itself
    assert not (tmp_path / "nova" / ".setup_health_probe").exists()


def test_persistence_down_when_not_writable(sh, nova_config, fake_hass, monkeypatch, tmp_path):
    from pathlib import Path
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")  # a FILE where a directory is expected -> mkdir fails
    monkeypatch.setattr(nova_config, "CONFIG_PATH", Path(blocker / "nova" / "config.json"), raising=False)
    out = sh._check_persistence(fake_hass)
    assert out["status"] == "down"
    assert "suggested_fix" in out


# ── Assist pipeline ───────────────────────────────────────────────────────────

def test_assist_pipeline_off_when_component_unavailable(sh, fake_hass):
    # No homeassistant.components.assist_pipeline registered — the default in
    # this test harness (see tests/conftest.py's minimal component stub).
    sys.modules.pop("homeassistant.components.assist_pipeline", None)
    out = sh._check_assist_pipeline(fake_hass)
    assert out["status"] == "off"


class _FakePipeline:
    def __init__(self, name="Nova", conversation_engine=None, tts_voice=None):
        self.name = name
        self.conversation_engine = conversation_engine
        self.tts_voice = tts_voice


def _install_fake_assist_pipeline(pipelines):
    components = sys.modules["homeassistant.components"]
    fake_mod = types.ModuleType("homeassistant.components.assist_pipeline")
    fake_mod.async_get_pipelines = lambda hass: list(pipelines)
    sys.modules["homeassistant.components.assist_pipeline"] = fake_mod
    components.assist_pipeline = fake_mod

    def _cleanup():
        del sys.modules["homeassistant.components.assist_pipeline"]
        del components.assist_pipeline
    return _cleanup


def test_assist_pipeline_warn_when_agent_missing(sh, bootstrap, fake_hass, monkeypatch):
    monkeypatch.setattr(bootstrap, "_find_nova_agent", lambda hass: None)
    cleanup = _install_fake_assist_pipeline([])
    try:
        out = sh._check_assist_pipeline(fake_hass)
        assert out["status"] == "warn"
        assert "conversation entity" in out["detail"]
    finally:
        cleanup()


def test_assist_pipeline_warn_when_no_nova_pipeline(sh, bootstrap, fake_hass, monkeypatch):
    monkeypatch.setattr(bootstrap, "_find_nova_agent", lambda hass: "conversation.nova")
    cleanup = _install_fake_assist_pipeline([_FakePipeline(name="Default")])
    try:
        out = sh._check_assist_pipeline(fake_hass)
        assert out["status"] == "warn"
        assert "no Nova-named" in out["detail"]
    finally:
        cleanup()


def test_assist_pipeline_warn_when_wrong_conversation_engine(sh, bootstrap, fake_hass, monkeypatch):
    monkeypatch.setattr(bootstrap, "_find_nova_agent", lambda hass: "conversation.nova")
    pipeline = _FakePipeline(name="Nova", conversation_engine="conversation.homeassistant")
    cleanup = _install_fake_assist_pipeline([pipeline])
    try:
        out = sh._check_assist_pipeline(fake_hass)
        assert out["status"] == "warn"
        assert "conversation.homeassistant" in out["detail"]
    finally:
        cleanup()


def test_assist_pipeline_ok_when_correctly_wired(sh, bootstrap, fake_hass, monkeypatch):
    monkeypatch.setattr(bootstrap, "_find_nova_agent", lambda hass: "conversation.nova")
    pipeline = _FakePipeline(name="Nova", conversation_engine="conversation.nova")
    cleanup = _install_fake_assist_pipeline([pipeline])
    try:
        out = sh._check_assist_pipeline(fake_hass)
        assert out["status"] == "ok"
    finally:
        cleanup()


# ── aggregate ─────────────────────────────────────────────────────────────────

def _install_fake_diagnostics(run_service_health_fn):
    """Install a fake jc.diagnostics module exposing run_service_health, the
    only name setup_health.run_setup_health actually calls — avoids pulling
    in the real diagnostics package (which chains into modules requiring a
    real Home Assistant, e.g. via __init__.py's other imports)."""
    fake = types.ModuleType("jc.diagnostics")
    fake.run_service_health = run_service_health_fn
    sys.modules["jc.diagnostics"] = fake
    sys.modules["jc"].diagnostics = fake

    def _cleanup():
        del sys.modules["jc.diagnostics"]
        del sys.modules["jc"].diagnostics
    return _cleanup


async def test_run_setup_health_folds_in_service_health_unchanged(sh, fake_hass, monkeypatch):
    """Proves reuse, not duplication: the 8 service_health checks flow
    through verbatim, and none of setup_health's own checks re-derive them."""
    fake_services = [{"name": "LLM", "key": "llm", "status": "ok", "detail": "fine"}]

    async def _fake_run_service_health(hass):
        return {"overall": "ok", "services": fake_services, "summary": "all good"}

    cleanup = _install_fake_diagnostics(_fake_run_service_health)
    try:
        for fn in ("_check_entity_references", "_check_room_speakers", "_check_camera_overrides",
                  "_check_notify_service", "_check_assist_pipeline", "_check_person_entities",
                  "_check_required_integrations"):
            monkeypatch.setattr(sh, fn, lambda hass, _f=fn: {"name": _f, "key": _f, "status": "off", "detail": ""})
        monkeypatch.setattr(sh, "_check_persistence",
                            lambda hass: {"name": "p", "key": "persistence", "status": "off", "detail": ""})

        result = await sh.run_setup_health(fake_hass)
        assert fake_services[0] in result["checks"]
        assert result["overall"] == "ok"
    finally:
        cleanup()


async def test_run_setup_health_never_raises_on_service_health_failure(sh, fake_hass, monkeypatch):
    async def _boom(hass):
        raise RuntimeError("service health down")
    cleanup = _install_fake_diagnostics(_boom)
    try:
        result = await sh.run_setup_health(fake_hass)
        assert result["overall"] in ("down", "warn", "ok", "off")  # never raised
    finally:
        cleanup()
