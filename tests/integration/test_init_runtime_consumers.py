"""Phase 3B.2 __init__.py runtime_config readers on NovaRuntime, against a
real Home Assistant (PHACC).

Drives the real setup, camera event listeners, scheduled ticks and services
to prove each migrated reader:

* reads the runtime's own runtime_config, not the hass.data bridge,
* is not affected by a drifted, missing or damaged bridge,
* sees in-place changes on the next event, tick or service call,
* fails visibly for a loaded entry with no runtime instead of using
  defaults, and stays safe during setup and after unload.

Readers covered: the camera auto-analysis flags (_auto_flag), the host-health
and sleep-prompt ticks, lockdown setup, _get_speakers, nova.test_notify and
nova.test_routing.

Everything outward is faked: vision analysis, host sampling, sleep prompts,
lockdown wiring and notifications only record what they were given. No
device is touched: there are no lock, alarm or cover entities here.
"""
import logging

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .test_runtime_data import (  # noqa: F401  (autouse fixtures)
    DOMAIN,
    _add_entry,
    _no_real_config,
    _restore_nova_config,
    _setup,
)


def _drift_bridge(hass, entry, values: dict) -> dict:
    """Point the bridge at a different, stale dict. A migrated reader must
    not see it."""
    stale = dict(values)
    hass.data[DOMAIN][entry.entry_id]["runtime_config"] = stale
    return stale


def _drop_runtime(entry):
    """A loaded entry whose runtime was lost (broken ownership)."""
    object.__delattr__(entry, "runtime_data")
    assert entry.state is ConfigEntryState.LOADED


def _unavailable_logged(caplog) -> bool:
    return any("Nova runtime is not available" in (r.getMessage() + str(r.exc_info))
               for r in caplog.records)


@pytest.fixture
def effective_calls(monkeypatch):
    """Record the runtime_config object handed to effective_config_with_runtime
    (identity, not a copy) while still returning the real merge."""
    from custom_components.nova import nova_config
    real = nova_config.effective_config_with_runtime
    seen: list[dict] = []

    def _spy(entry=None, runtime_config=None):
        seen.append(runtime_config)
        return real(entry, runtime_config)

    monkeypatch.setattr(nova_config, "effective_config_with_runtime", _spy)
    return seen


# ── Camera auto-analysis flags (_auto_flag) ─────────────────────────────────

@pytest.fixture
def camera_calls(monkeypatch):
    """Map any Nest device to camera.<device_id> and record analysis calls
    instead of running vision."""
    import custom_components.nova as nova
    from custom_components.nova import camera
    calls: list[tuple[str, str]] = []

    async def _analyze(hass, client, honorific, tts, spk, entity_id, reason,
                       doorbell=False):
        calls.append(("analyze", entity_id))

    async def _visitor(hass, client, honorific, entity_id):
        calls.append(("visitor", entity_id))

    monkeypatch.setattr(camera, "_nest_device_to_camera",
                        lambda hass, device_id: f"camera.{device_id}")
    monkeypatch.setattr(camera, "async_visitor_observation", _visitor)
    monkeypatch.setattr(nova, "async_auto_analyze_on_event", _analyze)
    return calls


async def _chime(hass, device_id):
    hass.bus.async_fire("nest_event", {"device_id": device_id,
                                       "type": "doorbell_chime"})
    await hass.async_block_till_done()


async def _person(hass, device_id):
    hass.bus.async_fire("nest_event", {"device_id": device_id,
                                       "type": "camera_person"})
    await hass.async_block_till_done()


async def test_auto_flag_reads_live_runtime_values(hass, camera_calls):
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config
    rc.pop("camera_auto_analyze", None)
    rc.pop("visitor_learning", None)

    await _chime(hass, "d1")                       # default: on
    assert camera_calls == [("analyze", "camera.d1")]

    rc["camera_auto_analyze"] = False              # panel turns it off
    await _chime(hass, "d2")
    assert camera_calls == [("analyze", "camera.d1")]

    rc["camera_auto_analyze"] = True               # and back on
    await _chime(hass, "d3")
    assert camera_calls[-1] == ("analyze", "camera.d3")


@pytest.mark.parametrize(("value", "fires"), [
    (True, True), (False, False), ("on", True), ("yes", True), ("1", True),
    ("TRUE", True), ("off", False), ("0", False), ("no", False), (1, True),
    (0, False), ("", False),
])
async def test_auto_flag_keeps_boolean_coercion(hass, camera_calls, value, fires):
    entry = await _setup(hass)
    entry.runtime_data.runtime_config["camera_auto_analyze"] = value
    await _chime(hass, "d1")
    assert bool(camera_calls) is fires


async def test_visitor_learning_flag_is_live(hass, camera_calls):
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config
    rc["camera_auto_analyze"] = True
    rc.pop("visitor_learning", None)

    await _person(hass, "p1")                      # default: on
    assert camera_calls == [("visitor", "camera.p1")]
    rc["visitor_learning"] = "false"
    await _person(hass, "p2")
    assert camera_calls == [("visitor", "camera.p1")]
    rc["visitor_learning"] = True
    await _person(hass, "p3")
    assert camera_calls[-1] == ("visitor", "camera.p3")


async def test_frigate_motion_flag_defaults_off_and_is_live(hass, camera_calls):
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config
    rc["camera_auto_analyze"] = True
    rc.pop("camera_auto_analyze_motion", None)
    hass.states.async_set("camera.drive", "idle")

    def _frigate():
        hass.bus.async_fire("frigate_event", {
            "type": "new", "after": {"camera": "drive", "label": "person"}})

    _frigate()
    await hass.async_block_till_done()
    assert camera_calls == []
    rc["camera_auto_analyze_motion"] = "on"
    _frigate()
    await hass.async_block_till_done()
    assert camera_calls == [("analyze", "camera.drive")]


async def test_auto_flag_ignores_a_drifted_bridge(hass, camera_calls):
    entry = await _setup(hass)
    entry.runtime_data.runtime_config["camera_auto_analyze"] = False
    _drift_bridge(hass, entry, {"camera_auto_analyze": True})
    await _chime(hass, "d1")
    assert camera_calls == []


@pytest.mark.parametrize("damage", ["missing", "not_a_dict", "no_key"])
async def test_auto_flag_ignores_a_missing_or_damaged_bridge(
        hass, camera_calls, damage):
    entry = await _setup(hass)
    entry.runtime_data.runtime_config["camera_auto_analyze"] = False
    if damage == "missing":
        hass.data[DOMAIN].pop(entry.entry_id)
    elif damage == "not_a_dict":
        hass.data[DOMAIN][entry.entry_id] = "garbage"
    else:
        hass.data[DOMAIN][entry.entry_id].pop("runtime_config")
    await _chime(hass, "d1")
    assert camera_calls == []
    entry.runtime_data.runtime_config["camera_auto_analyze"] = True
    await _chime(hass, "d2")
    assert camera_calls == [("analyze", "camera.d2")]


async def test_auto_flag_fails_visibly_for_loaded_entry_without_runtime(
        hass, camera_calls, caplog):
    entry = await _setup(hass)
    entry.runtime_data.runtime_config["camera_auto_analyze"] = False
    # The bridge would say "analyze"; defaults would say "analyze" too.
    _drift_bridge(hass, entry, {"camera_auto_analyze": True})
    _drop_runtime(entry)
    with caplog.at_level(logging.ERROR):
        await _chime(hass, "d1")
    assert camera_calls == []
    assert _unavailable_logged(caplog)


async def test_auto_flag_uses_defaults_for_an_event_during_setup(
        hass, camera_calls, monkeypatch):
    """A camera event that lands before the runtime exists uses the defaults
    and does not break setup."""
    import custom_components.nova as nova
    real = nova.NovaRuntime
    fired: list[bool] = []

    def _runtime_after_event(**kwargs):
        entry_ = hass.config_entries.async_entries(DOMAIN)[0]
        assert not hasattr(entry_, "runtime_data")
        hass.bus.async_fire("nest_event", {"device_id": "early",
                                           "type": "doorbell_chime"})
        hass.bus.async_fire("frigate_event", {
            "type": "new", "after": {"camera": "drive", "label": "person"}})
        fired.append(True)
        return real(**kwargs)

    monkeypatch.setattr(nova, "NovaRuntime", _runtime_after_event)
    hass.states.async_set("camera.drive", "idle")
    entry = await _setup(hass)
    assert fired == [True]
    assert entry.state is ConfigEntryState.LOADED
    # camera_auto_analyze defaults on, camera_auto_analyze_motion defaults off.
    assert camera_calls == [("analyze", "camera.early")]


# ── Scheduled ticks ─────────────────────────────────────────────────────────

@pytest.fixture
def tick_calls(monkeypatch):
    from custom_components.nova import host_health, sleep_detection
    calls: dict[str, list[dict]] = {"host_health": [], "sleep_prompt": []}

    async def _hh(hass, config):
        calls["host_health"].append(config)
        return {}

    async def _sleep(hass, config):
        calls["sleep_prompt"].append(config)

    monkeypatch.setattr(host_health, "tick", _hh)
    monkeypatch.setattr(sleep_detection, "maybe_prompt_sleep", _sleep)
    return calls


def _tick(entry, name):
    return entry.runtime_data.scheduler._tasks[name].cb


TICKS = [("host_health", "host_health_cpu_threshold"),
         ("sleep_prompt", "sleep_prompt_time")]


@pytest.mark.parametrize(("name", "key"), TICKS)
async def test_tick_reads_live_runtime_each_time(
        hass, tick_calls, effective_calls, name, key):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    tick = _tick(entry, name)

    runtime.runtime_config[key] = "first"
    await tick(None)
    runtime.runtime_config[key] = "second"          # panel write, in place
    await tick(None)

    assert [c[key] for c in tick_calls[name]] == ["first", "second"]
    assert effective_calls and all(rc is runtime.runtime_config
                                   for rc in effective_calls)


@pytest.mark.parametrize(("name", "key"), TICKS)
async def test_tick_ignores_a_drifted_or_missing_bridge(
        hass, tick_calls, name, key):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    tick = _tick(entry, name)
    runtime.runtime_config[key] = "runtime"

    _drift_bridge(hass, entry, {key: "bridge"})
    await tick(None)
    hass.data[DOMAIN].pop(entry.entry_id)
    await tick(None)
    assert [c[key] for c in tick_calls[name]] == ["runtime", "runtime"]


@pytest.mark.parametrize(("name", "key"), TICKS)
async def test_tick_without_runtime_on_loaded_entry_warns_and_skips(
        hass, tick_calls, caplog, name, key):
    entry = await _setup(hass)
    tick = _tick(entry, name)          # the scheduler still holds it
    _drift_bridge(hass, entry, {key: "bridge"})
    _drop_runtime(entry)
    with caplog.at_level(logging.WARNING):
        await tick(None)                # contained: never raises
    assert tick_calls[name] == []
    assert any(r.levelno == logging.WARNING and "skipped" in r.getMessage()
               and "Nova runtime is not available" in r.getMessage()
               for r in caplog.records)


@pytest.mark.parametrize(("name", "key"), TICKS)
async def test_tick_after_unload_is_safe(hass, tick_calls, caplog, name, key):
    """A tick that races an unload uses defaults quietly; nothing raises."""
    entry = await _setup(hass)
    tick = _tick(entry, name)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    with caplog.at_level(logging.WARNING):
        await tick(None)
    assert len(tick_calls[name]) == 1
    assert not _unavailable_logged(caplog)


# ── Lockdown setup ──────────────────────────────────────────────────────────

@pytest.fixture
def lockdown_calls(monkeypatch):
    from custom_components.nova import cognitive_core
    calls: list[dict] = []

    async def _ensure(hass, config):
        calls.append(config)

    monkeypatch.setattr(cognitive_core, "ensure_lockdown", _ensure)
    return calls


@pytest.fixture
def before_lockdown(monkeypatch):
    """Run a hook at the last awaited setup step before lockdown wiring
    (reminder watcher start), once the runtime exists."""
    from custom_components.nova.reminders import ReminderWatcher
    real = ReminderWatcher.async_start
    hooks: list = []

    async def _start(self):
        await real(self)
        for hook in hooks:
            hook()

    monkeypatch.setattr(ReminderWatcher, "async_start", _start)
    return hooks


async def test_lockdown_gets_current_runtime_values(
        hass, lockdown_calls, before_lockdown, effective_calls):
    seen = {}

    def _hook():
        entry_ = hass.config_entries.async_entries(DOMAIN)[0]
        runtime = entry_.runtime_data
        seen["runtime"] = runtime
        runtime.runtime_config["lockdown_on_alarm"] = "runtime"
        _drift_bridge(hass, entry_, {"lockdown_on_alarm": "bridge"})

    before_lockdown.append(_hook)
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert [c["lockdown_on_alarm"] for c in lockdown_calls] == ["runtime"]
    assert effective_calls[-1] is seen["runtime"].runtime_config


async def test_lockdown_without_runtime_is_non_fatal_and_visible(
        hass, lockdown_calls, before_lockdown, caplog):
    def _hook():
        entry_ = hass.config_entries.async_entries(DOMAIN)[0]
        _drift_bridge(hass, entry_, {"lockdown_on_alarm": "bridge"})
        object.__delattr__(entry_, "runtime_data")

    before_lockdown.append(_hook)
    with caplog.at_level(logging.WARNING):
        entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED       # setup still finished
    assert lockdown_calls == []                          # no bridge fallback
    assert any("lockdown wiring failed" in r.getMessage()
               and "Nova runtime is not available" in r.getMessage()
               for r in caplog.records)


# ── _get_speakers ───────────────────────────────────────────────────────────

async def test_get_speakers_reads_live_runtime(hass):
    import custom_components.nova as nova
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config

    rc["announcement_speakers"] = '["media_player.kitchen"]'   # panel JSON
    assert nova._get_speakers(hass, entry) == ["media_player.kitchen"]
    rc["announcement_speakers"] = ["media_player.lounge"]      # list, in place
    assert nova._get_speakers(hass, entry) == ["media_player.lounge"]


async def test_get_speakers_ignores_a_drifted_or_missing_bridge(hass):
    import custom_components.nova as nova
    entry = await _setup(hass)
    entry.runtime_data.runtime_config["announcement_speakers"] = ["media_player.a"]
    _drift_bridge(hass, entry, {"announcement_speakers": ["media_player.stale"]})
    assert nova._get_speakers(hass, entry) == ["media_player.a"]
    hass.data[DOMAIN].pop(entry.entry_id)
    assert nova._get_speakers(hass, entry) == ["media_player.a"]


async def test_get_speakers_keeps_its_fallback_for_bad_runtime_values(hass, monkeypatch):
    import custom_components.nova as nova
    from custom_components.nova import nova_config
    entry = await _setup(hass)
    entry.runtime_data.runtime_config["announcement_speakers"] = "not json"
    monkeypatch.setattr(nova_config, "effective_config",
                        lambda e: {"announcement_speakers": ["media_player.cfg"]})
    assert nova._get_speakers(hass, entry) == ["media_player.cfg"]


async def test_get_speakers_raises_for_loaded_entry_without_runtime(hass, monkeypatch):
    import custom_components.nova as nova
    from custom_components.nova import nova_config
    from custom_components.nova.runtime import NovaRuntimeUnavailable
    entry = await _setup(hass)
    _drift_bridge(hass, entry, {"announcement_speakers": ["media_player.stale"]})
    monkeypatch.setattr(nova_config, "effective_config",
                        lambda e: {"announcement_speakers": ["media_player.cfg"]})
    _drop_runtime(entry)
    with pytest.raises(NovaRuntimeUnavailable):
        nova._get_speakers(hass, entry)


async def test_get_speakers_after_unload_uses_the_fallback(hass, monkeypatch):
    import custom_components.nova as nova
    from custom_components.nova import nova_config
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    monkeypatch.setattr(nova_config, "effective_config",
                        lambda e: {"announcement_speakers": ["media_player.cfg"]})
    assert nova._get_speakers(hass, entry) == ["media_player.cfg"]


# ── nova.test_notify ────────────────────────────────────────────────────────

@pytest.fixture
def notify_configs(monkeypatch):
    from custom_components.nova import notify_targets
    seen: list[dict] = []

    async def _send(hass, config, payload, **kwargs):
        seen.append(dict(config))
        return 1

    monkeypatch.setattr(notify_targets, "async_send_configured_notifications", _send)
    return seen


async def _setup_with_data(hass, data_extra: dict, options: dict):
    from homeassistant.setup import async_setup_component
    from .test_wiring_smoke import _make_entry
    assert await async_setup_component(hass, "homeassistant", {})
    base = _make_entry()
    entry = MockConfigEntry(domain=DOMAIN, data={**base.data, **data_extra},
                            options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _notify(hass):
    await hass.services.async_call(DOMAIN, "test_notify", {}, blocking=True)
    await hass.async_block_till_done()


async def test_test_notify_precedence_is_data_then_options_then_runtime(
        hass, notify_configs):
    entry = await _setup_with_data(
        hass,
        {"notify_service": "notify.from_data", "data_only": "data"},
        {"notify_service": "notify.from_options", "options_only": "options"})
    rc = entry.runtime_data.runtime_config

    rc["notify_service"] = "notify.from_runtime"
    await _notify(hass)
    rc.pop("notify_service")                     # runtime no longer sets it
    await _notify(hass)
    rc["notify_service"] = "notify.changed"      # live change, in place
    await _notify(hass)

    assert [c["notify_service"] for c in notify_configs] == [
        "notify.from_runtime", "notify.from_options", "notify.changed"]
    assert notify_configs[0]["data_only"] == "data"
    assert notify_configs[0]["options_only"] == "options"


async def test_test_notify_ignores_a_drifted_or_missing_bridge(hass, notify_configs):
    entry = await _setup(hass)
    entry.runtime_data.runtime_config["notify_service"] = "notify.runtime"
    _drift_bridge(hass, entry, {"notify_service": "notify.stale"})
    await _notify(hass)
    hass.data[DOMAIN].pop(entry.entry_id)
    await _notify(hass)
    assert [c["notify_service"] for c in notify_configs] == [
        "notify.runtime", "notify.runtime"]


async def test_test_notify_fails_for_loaded_entry_without_runtime(hass, notify_configs):
    from custom_components.nova.runtime import NovaRuntimeUnavailable
    entry = await _setup(hass)
    _drift_bridge(hass, entry, {"notify_service": "notify.stale"})
    _drop_runtime(entry)
    with pytest.raises(NovaRuntimeUnavailable):
        await _notify(hass)
    assert notify_configs == []


# ── nova.test_routing ───────────────────────────────────────────────────────

async def _routing(hass, caplog) -> dict[str, str]:
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await hass.services.async_call(DOMAIN, "test_routing", {}, blocking=True)
        await hass.async_block_till_done()
    lines = [r.getMessage() for r in caplog.records]
    return {
        "speakers": next(m for m in lines if m.startswith("Announcement speakers (panel)")),
        "pairings": next(m for m in lines if m.startswith("Satellite pairings (panel)")),
    }


async def test_test_routing_reads_runtime_values(hass, caplog):
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config
    rc["announcement_speakers"] = '["media_player.a"]'
    rc["satellite_pairings"] = '{"assist_satellite.k": "media_player.k"}'
    _drift_bridge(hass, entry, {"announcement_speakers": ["media_player.stale"],
                                "satellite_pairings": {"x": "y"}})
    out = await _routing(hass, caplog)
    assert out["speakers"] == "Announcement speakers (panel): ['media_player.a']"
    assert out["pairings"] == (
        "Satellite pairings (panel): {'assist_satellite.k': 'media_player.k'}")

    rc["announcement_speakers"] = ["media_player.b"]           # live change
    rc["satellite_pairings"] = {"assist_satellite.l": "media_player.l"}
    hass.data[DOMAIN].pop(entry.entry_id)                      # bridge gone
    out = await _routing(hass, caplog)
    assert out["speakers"] == "Announcement speakers (panel): ['media_player.b']"
    assert out["pairings"] == (
        "Satellite pairings (panel): {'assist_satellite.l': 'media_player.l'}")


async def test_test_routing_output_with_no_or_bad_values(hass, caplog):
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config
    rc.pop("announcement_speakers", None)
    rc["satellite_pairings"] = "not json"
    out = await _routing(hass, caplog)
    assert out["speakers"] == "Announcement speakers (panel): None"
    assert out["pairings"] == "Satellite pairings (panel): None"


async def test_test_routing_fails_for_loaded_entry_without_runtime(hass, caplog):
    from custom_components.nova.runtime import NovaRuntimeUnavailable
    entry = await _setup(hass)
    _drift_bridge(hass, entry, {"announcement_speakers": ["media_player.stale"]})
    _drop_runtime(entry)
    with pytest.raises(NovaRuntimeUnavailable):
        await hass.services.async_call(DOMAIN, "test_routing", {}, blocking=True)
