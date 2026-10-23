"""The in-process voice setup (bootstrap.py, v6.28.0).

No Supervisor and no aiohttp exist here, so the live add-on calls are faked.
What this file pins: the gates that keep a real Home Assistant safe (switched
off by default, Supervisor only, once per version unless forced), the run-once
marker, how the Nova Assist pipeline is built or repaired, the Wyoming reload,
engine discovery and the Repair notices a run raises. test_bootstrap_spec.py
covers the Supervisor REST calls and the remaining failure paths.
"""
import inspect
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _aiohttp_stand_in():
    stand_in = ModuleType("aiohttp")
    stand_in.ClientTimeout = lambda **kw: None
    stand_in.ClientSession = object
    return stand_in


# bootstrap imports aiohttp at module level; the real package is not installed.
sys.modules.setdefault("aiohttp", _aiohttp_stand_in())

AGENT = "conversation.nova"
OLD_NOVA_VOICE = "en_GB-nova-high"


@pytest.fixture
def boot(load, tmp_path, monkeypatch):
    module = load("bootstrap")
    monkeypatch.setattr(module, "MARKER_PATH", Path(tmp_path) / ".bootstrap_done")
    monkeypatch.setattr(module, "PIPER_DIR", Path(tmp_path) / "piper")
    return module


@pytest.fixture
def settings(load, monkeypatch):
    """A dict standing in for nova_config; tests fill it in."""
    values: dict = {}
    monkeypatch.setattr(load("nova_config"), "get",
                        lambda key, default=None: values.get(key, default))
    return values


@pytest.fixture
def supervised(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")


def _returning(value):
    async def _answer(*args, **kwargs):
        return value
    return _answer


def _fake_phases(boot, monkeypatch, *, addons=True, pipeline=True, agent=AGENT):
    """Replace every live phase; returns the slugs _ensure_addon was asked for."""
    asked = []

    async def _addon(hass, slug, friendly):
        asked.append(slug)
        return addons
    monkeypatch.setattr(boot, "_ensure_addon", _addon)
    monkeypatch.setattr(boot, "_reload_wyoming", _returning(0))
    monkeypatch.setattr(boot, "_wait_for_agent", _returning(agent))
    monkeypatch.setattr(boot, "_create_pipeline", _returning(pipeline))
    return asked


# ── is this a Supervisor install? ───────────────────────────────────────────

def test_supervised_only_with_a_token(boot, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert boot.is_supervised() is False
    assert boot.supervisor_token() == ""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    assert boot.is_supervised() is True
    assert boot.supervisor_token() == "test-token"


# ── the marker that makes it run once per version ───────────────────────────

def test_marker_written_then_read_back(boot):
    assert boot._read_marker() == {}
    boot._write_marker("6.28.0", {"addons_ok": True})
    assert boot._read_marker() == {"version": "6.28.0", "addons_ok": True}


@pytest.mark.parametrize("name", ["HF_BASE", "HF_REVISION", "EXPECTED_SHA256",
                                  "_download_voice", "_download_file",
                                  "resolve_installed_quality"])
def test_no_voice_is_downloaded(boot, name):
    """Nova uses Home Assistant's default voice: nothing is fetched and no
    voice file is written, so none of the old download machinery remains."""
    assert not hasattr(boot, name)


# ── the gates in front of a run ─────────────────────────────────────────────

@pytest.mark.parametrize("switch", [None, False], ids=["fresh-install", "switched-off"])
async def test_off_unless_switched_on(boot, settings, supervised, fake_hass, monkeypatch, switch):
    """A new install leaves the voice add-on setup off until it is set up
    (8.41.0), and switching it off keeps it off."""
    if switch is not None:
        settings["auto_bootstrap"] = switch
    asked = _fake_phases(boot, monkeypatch)
    status = await boot.async_run_bootstrap(fake_hass)
    assert status["skipped"] == "auto_bootstrap disabled"
    assert status["addons_ok"] is False and status["supervised"] is False
    assert asked == []
    assert boot._read_marker() == {}


async def test_needs_the_supervisor(boot, settings, fake_hass, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings["auto_bootstrap"] = True
    asked = _fake_phases(boot, monkeypatch)
    status = await boot.async_run_bootstrap(fake_hass)
    assert status["supervised"] is False
    assert "Supervisor" in status["skipped"]
    assert asked == []


async def test_same_version_runs_only_once(boot, settings, supervised, fake_hass, monkeypatch):
    settings["auto_bootstrap"] = True
    boot._write_marker(boot._current_version(), {"addons_ok": True})
    asked = _fake_phases(boot, monkeypatch)
    status = await boot.async_run_bootstrap(fake_hass)
    assert status["skipped"] == "already bootstrapped this version"
    assert status["supervised"] is True
    assert asked == []


async def test_force_runs_past_the_marker(boot, settings, supervised, fake_hass, monkeypatch):
    settings["auto_bootstrap"] = True
    version = boot._current_version()
    boot._write_marker(version, {"addons_ok": True})
    asked = _fake_phases(boot, monkeypatch, addons=False, agent=None)
    status = await boot.async_run_bootstrap(fake_hass, force=True)
    assert status["skipped"] is None                 # the marker did not stop it
    assert status["addons_ok"] is False              # phase one really ran
    assert asked == list(boot.REQUIRED_ADDONS)
    rewritten = boot._read_marker()
    assert rewritten["version"] == version and rewritten["addons_ok"] is False


async def test_full_run_builds_the_pipeline_and_no_voice(boot, settings, supervised,
                                                         fake_hass, monkeypatch):
    settings.update(auto_bootstrap=True, auto_pipeline=True)
    _fake_phases(boot, monkeypatch)
    status = await boot.async_run_bootstrap(fake_hass)
    assert status["addons_ok"] is True
    assert status["wyoming_ok"] is True
    assert status["pipeline_ok"] is True
    assert "voice_ok" not in status


# ── Wyoming and engine discovery ────────────────────────────────────────────

async def test_every_wyoming_entry_is_reloaded(boot):
    reloaded = []

    async def _reload(entry_id):
        reloaded.append(entry_id)

    def _entries(domain):
        assert domain == "wyoming"
        return [SimpleNamespace(entry_id=eid) for eid in ("wy-a", "wy-b")]
    hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_entries=_entries, async_reload=_reload))
    assert await boot._reload_wyoming(hass) == 2
    assert reloaded == ["wy-a", "wy-b"]


@pytest.mark.parametrize("present, domain, hint, expected", [
    (["stt.faster_whisper", "stt.something_else"], "stt", "whisper", "stt.faster_whisper"),
    (["stt.something_else", "stt.faster_whisper"], "stt", "whisper", "stt.faster_whisper"),
    (["tts.piper"], "tts", "piper", "tts.piper"),
    (["tts.piper"], "tts", "nope", "tts.piper"),       # no match: the first there is
])
def test_engine_choice(boot, fake_hass, present, domain, hint, expected):
    for entity_id in present:
        fake_hass.states.set(entity_id, "idle")
    assert boot._find_engine(fake_hass, domain, hint) == expected


# ── building or repairing the Nova pipeline ─────────────────────────────────

class _Pipeline:
    def __init__(self, conversation_engine=None, tts_voice=None, name="Nova"):
        self.name = name
        self.conversation_engine = conversation_engine
        self.tts_voice = tts_voice


class _AssistComponent:
    """Just enough of homeassistant.components.assist_pipeline."""

    def __init__(self, existing=(), made=None):
        self.existing = list(existing)
        self.made = made
        self.updates = []

    def async_get_pipelines(self, hass):
        return list(self.existing)

    async def async_create_default_pipeline(self, hass, stt_engine_id, tts_engine_id,
                                            pipeline_name):
        return self.made

    async def async_update_pipeline(self, hass, pipeline, **changes):
        self.updates.append(changes)
        for field, value in changes.items():
            setattr(pipeline, field, value)


@pytest.fixture
def assist(monkeypatch, fake_hass):
    """Installs a fake assist_pipeline component and the agent plus engines a
    pipeline needs; returns a function that sets the component up."""
    for entity_id in (AGENT, "stt.faster_whisper", "tts.piper"):
        fake_hass.states.set(entity_id, "idle")

    def _install(**kwargs):
        component = _AssistComponent(**kwargs)
        monkeypatch.setitem(sys.modules, "homeassistant.components.assist_pipeline", component)
        monkeypatch.setattr(sys.modules["homeassistant.components"], "assist_pipeline",
                            component, raising=False)
        return component
    return _install


async def test_new_pipeline_gets_nova_as_agent_and_the_default_voice(boot, assist, fake_hass):
    made = _Pipeline()
    component = assist(made=made)
    assert await boot._create_pipeline(fake_hass) is True
    assert component.updates == [{"conversation_engine": AGENT}]
    assert made.conversation_engine == AGENT
    assert made.tts_voice is None                    # Home Assistant's default voice


@pytest.mark.parametrize("agent, voice, on_disk, updates, voice_after", [
    # The old Nova voice whose model file has gone falls back to the default.
    (AGENT, OLD_NOVA_VOICE, False, [{"tts_voice": None}], None),
    # Still on disk: left as it is.
    (AGENT, OLD_NOVA_VOICE, True, [], OLD_NOVA_VOICE),
    # Somebody else's chosen voice is never touched.
    (AGENT, "en_GB-alba-medium", None, [], "en_GB-alba-medium"),
    # Another agent answering is put right.
    ("conversation.other", None, None, [{"conversation_engine": AGENT}], None),
], ids=["old-voice-gone", "old-voice-present", "other-voice", "wrong-agent"])
async def test_existing_pipeline_is_repaired_only_where_needed(
        boot, assist, fake_hass, monkeypatch, agent, voice, on_disk, updates, voice_after):
    existing = _Pipeline(conversation_engine=agent, tts_voice=voice)
    component = assist(existing=[existing])
    if on_disk is not None:
        monkeypatch.setattr(boot, "_legacy_voice_present", lambda v: on_disk)
    assert await boot._create_pipeline(fake_hass) is True
    assert component.updates == updates
    assert existing.tts_voice == voice_after
    assert existing.conversation_engine == AGENT


# ── Repair notices for the voice setup (v8.6.0) ─────────────────────────────

@pytest.fixture
def notices(load, monkeypatch):
    """Captures the voice setup notices instead of raising Repair issues."""
    seen = []
    repairs = load("repair_notices")
    monkeypatch.setattr(repairs, "note_voice_setup_step",
                        lambda hass, step, number, total: seen.append(("step", step, number, total)))
    monkeypatch.setattr(repairs, "note_voice_setup_incomplete",
                        lambda hass, failed: seen.append(("incomplete", list(failed))))
    monkeypatch.setattr(repairs, "clear_voice_setup", lambda hass: seen.append(("clear",)))
    return seen


@pytest.fixture
def switched_on(settings, supervised):
    settings.update(auto_bootstrap=True, auto_pipeline=True)
    return settings


@pytest.mark.parametrize("with_pipeline, expected", [
    (True, [("step", "addons", 1, 3), ("step", "wyoming", 2, 3),
            ("step", "pipeline", 3, 3), ("clear",)]),
    (False, [("step", "addons", 1, 2), ("step", "wyoming", 2, 2), ("clear",)]),
], ids=["three-steps", "two-steps"])
async def test_first_run_shows_progress_then_clears(boot, switched_on, notices, fake_hass,
                                                    monkeypatch, with_pipeline, expected):
    switched_on["auto_pipeline"] = with_pipeline
    _fake_phases(boot, monkeypatch)
    await boot.async_run_bootstrap(fake_hass)
    assert notices == expected


async def test_failures_are_named_in_the_incomplete_notice(boot, switched_on, notices,
                                                           fake_hass, monkeypatch):
    _fake_phases(boot, monkeypatch, addons=False, pipeline=False)
    await boot.async_run_bootstrap(fake_hass)
    assert notices[-1] == ("incomplete", ["Piper, Whisper, openWakeWord", "Assist pipeline"])


async def test_rerun_after_an_update_is_quiet(boot, switched_on, notices, fake_hass, monkeypatch):
    boot._write_marker("0.0.1", {"addons_ok": True, "pipeline_ok": True})
    _fake_phases(boot, monkeypatch)
    await boot.async_run_bootstrap(fake_hass)
    assert notices == [("clear",)]


async def test_skipped_start_rechecks_a_failed_pipeline(boot, switched_on, notices, fake_hass,
                                                        monkeypatch, load):
    boot._write_marker(boot._current_version(),
                       {"addons_ok": True, "pipeline_tried": True, "pipeline_ok": False})
    _fake_phases(boot, monkeypatch)
    health = load("setup_health")

    monkeypatch.setattr(health, "_check_assist_pipeline", lambda hass: {"status": "warn"})
    status = await boot.async_run_bootstrap(fake_hass)
    assert status["skipped"] == "already bootstrapped this version"
    assert notices == [("incomplete", ["Assist pipeline"])]

    # Fixed by hand since: the next start clears the notice.
    notices.clear()
    monkeypatch.setattr(health, "_check_assist_pipeline", lambda hass: {"status": "ok"})
    await boot.async_run_bootstrap(fake_hass)
    assert notices == [("clear",)]


async def test_no_notices_without_the_supervisor(boot, switched_on, notices, fake_hass,
                                                 monkeypatch):
    _fake_phases(boot, monkeypatch)
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    await boot.async_run_bootstrap(fake_hass)
    assert notices == []
