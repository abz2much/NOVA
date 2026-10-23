"""Phase 4: Nova's services live for the process lifetime (PHACC).

async_setup registers all 27 nova.* services once, at integration scope.
Config-entry setup, unload, reload and setup failure never add or remove
them. Every call resolves the loaded entry and its NovaRuntime at call
time, so this file drives the real lifecycle to prove:

* registration happens once, survives reload, unload and setup failure,
  and repeated cycles leave service and listener counts stable,
* handlers use the current runtime's client, sentinel and configuration
  after a reload, never the first ones,
* each lifecycle state rejects calls with its translated error, before any
  delegate, bus event or proactive object is touched,
* a LOADED entry without a runtime still raises NovaRuntimeUnavailable,
* safety paths (lockdown) are unchanged,
* the shipped blueprints validate while nova.analyze_on_event is
  registered, and no Nova state appears in hass.data.

Nothing here actuates a device: delegates are replaced with recorders, and
there are no lock, alarm or cover entities.
"""
from __future__ import annotations

import contextlib
import pathlib
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component

from .conftest import MockConfigEntry
from .test_runtime_data import (  # noqa: F401  (autouse fixtures)
    DOMAIN,
    _add_entry,
    _no_real_config,
    _restore_nova_config,
    _setup,
    assert_no_nova_state_in_hass_data,
)
from .test_wiring_smoke import _make_entry
import logging
import shutil
from pathlib import Path
import voluptuous as vol

SERVICE_COUNT = 27

# Minimal valid data for every service, so a rejection is the lifecycle
# check's and never schema validation's.
VALID_DATA = {
    "add_reminder": {"label": "bins", "trigger_at": "08:00"},
    "analyze_camera": {"entity_id": "camera.front"},
    "analyze_on_event": {"entity_id": "camera.front"},
    "backup": {},
    "briefing": {},
    "check_packages": {},
    "conversation_summary": {},
    "create_automation": {"alias": "a", "trigger": [], "action": []},
    "database_purge": {},
    "database_stats": {},
    "diagnose_doorbell": {},
    "forget": {"key": "k"},
    "lockdown": {"state": "on"},
    "nap": {},
    "observer_start": {},
    "observer_status": {},
    "observer_stop": {},
    "remember": {"key": "k", "value": "v"},
    "restore": {},
    "sentinel_start": {},
    "sentinel_stop": {},
    "shush": {},
    "test_notify": {},
    "test_routing": {},
    "test_tts": {},
    "train_doorbell_backlog": {},
    "unshush": {},
}

REJECTED_STATES = [
    (ConfigEntryState.NOT_LOADED, "not_loaded"),
    (ConfigEntryState.SETUP_IN_PROGRESS, "reloading"),
    (ConfigEntryState.UNLOAD_IN_PROGRESS, "reloading"),
    (ConfigEntryState.SETUP_ERROR, "setup_failed"),
    (ConfigEntryState.SETUP_RETRY, "setup_failed"),
    (ConfigEntryState.MIGRATION_ERROR, "setup_failed"),
    (ConfigEntryState.FAILED_UNLOAD, "setup_failed"),
]


def _services(hass) -> dict:
    return dict(hass.services.async_services().get(DOMAIN, {}))


def _handler_ids(hass) -> dict:
    return {name: id(svc.job.target) for name, svc in _services(hass).items()}


async def _call(hass, service, data=None):
    return await hass.services.async_call(
        DOMAIN, service, VALID_DATA[service] if data is None else data, blocking=True)


async def _rejected(hass, service) -> str:
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, service)
    assert err.value.translation_domain == DOMAIN
    return err.value.translation_key


@contextlib.contextmanager
def _delegates_recorded():
    """Replace everything a service could hand work to with a recorder, for
    the duration of the block only (setup and unload keep the real ones)."""
    from custom_components.nova import (
        action_log, automation_creator, backup, cognitive_core, doorbell_training,
        knowledge, notify_targets, observer, output_gate, package_monitor,
        proactive_audio, services, sleep_detection, tts_helper,
    )
    from custom_components.nova.sentinel import NovaSentinel
    calls: list[str] = []

    def _rec(name, result=None):
        def _sync(*a, **k):
            calls.append(name)
            return result

        async def _async(*a, **k):
            calls.append(name)
            return result
        return _sync, _async

    async_targets = [
        (services, "async_analyze_camera"), (services, "async_auto_analyze_on_event"),
        (services, "async_briefing"), (services, "async_add_reminder_service"),
        (services, "async_summarise"), (cognitive_core, "request_lockdown"),
        (automation_creator, "create_automation"), (observer, "start"), (observer, "stop"),
        (notify_targets, "async_send_configured_notifications"),
        (doorbell_training, "scan_backlog"), (package_monitor, "periodic_check"),
        (tts_helper, "async_announce"),
    ]
    sync_targets = [
        (services, "purge_old_records"), (services, "get_stats"),
        (cognitive_core, "_ensure_lockdown_mgr"), (knowledge, "remember"),
        (knowledge, "forget"), (output_gate, "shush"), (output_gate, "unshush"),
        (output_gate, "status"), (sleep_detection, "set_nap"),
        (backup, "create_backup"), (backup, "restore_backup"), (action_log, "start"),
        (proactive_audio, "_alert_buffer"),
    ]
    with pytest.MonkeyPatch.context() as mp:
        for mod, name in async_targets:
            mp.setattr(mod, name, _rec(f"{mod.__name__}.{name}")[1])
        for mod, name in sync_targets:
            mp.setattr(mod, name, _rec(f"{mod.__name__}.{name}")[0])
        mp.setattr(NovaSentinel, "async_start", _rec("sentinel.async_start")[1])
        mp.setattr(NovaSentinel, "async_stop", _rec("sentinel.async_stop")[1])
        yield calls


@pytest.fixture
def nova_events(hass):
    seen = []

    @callback
    def _listener(event):
        if event.event_type.startswith("nova_"):
            seen.append(event.event_type)
    unsub = hass.bus.async_listen("*", _listener)
    yield seen
    unsub()


@pytest.fixture
def spoken(monkeypatch):
    """Record announcements instead of resolving or driving any speaker."""
    from custom_components.nova import proactive_audio
    calls = []

    async def _announce(hass, runtime, message, area_id, critical):
        calls.append((runtime, message))

    monkeypatch.setattr(proactive_audio, "_announce", _announce)
    monkeypatch.setattr(proactive_audio, "_resolve_area_id", lambda hass, target: target)
    return calls


@pytest.fixture
def built(monkeypatch):
    """Count every proactive-audio object construction."""
    from custom_components.nova import proactive_audio
    made: list[str] = []
    for name in ("AlertBuffer",):
        real = getattr(proactive_audio, name)

        def _make(*a, _real=real, _name=name, **k):
            made.append(_name)
            return _real(*a, **k)
        monkeypatch.setattr(proactive_audio, name, _make)
    return made


# ── Registration ────────────────────────────────────────────────────────────

async def test_integration_setup_registers_all_services_without_an_entry(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert set(_services(hass)) == set(VALID_DATA)
    assert len(_services(hass)) == SERVICE_COUNT
    for name in ("analyze_camera", "nap", "lockdown"):
        assert await _rejected(hass, name) == "no_entry"


async def test_repeated_integration_setup_keeps_the_same_handlers(hass):
    from custom_components.nova.services import async_setup_services
    assert await async_setup_component(hass, DOMAIN, {})
    before = _handler_ids(hass)
    async_setup_services(hass)        # a defensive second call is a no-op
    assert _handler_ids(hass) == before


async def test_reloading_twice_keeps_handlers_and_count(hass):
    entry = await _setup(hass)
    before = _handler_ids(hass)
    assert len(before) == SERVICE_COUNT
    for _ in range(2):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert _handler_ids(hass) == before


async def test_all_services_survive_unload(hass):
    entry = await _setup(hass)
    before = _handler_ids(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _handler_ids(hass) == before
    assert await _rejected(hass, "analyze_camera") == "not_loaded"


async def test_all_services_survive_a_setup_that_returns_false(hass):
    entry = await _add_entry(hass)
    with patch("custom_components.nova.create_provider", side_effect=RuntimeError("boom")):
        assert await hass.config_entries.async_setup(entry.entry_id) is False
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_services(hass)) == SERVICE_COUNT
    assert await _rejected(hass, "briefing") == "setup_failed"


async def test_all_services_survive_a_setup_that_raises(hass):
    entry = await _add_entry(hass)
    with patch("custom_components.nova.sentinel.NovaSentinel.async_start",
               side_effect=RuntimeError("boom")):
        assert await hass.config_entries.async_setup(entry.entry_id) is False
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not hasattr(entry, "runtime_data")
    assert len(_services(hass)) == SERVICE_COUNT
    assert await _rejected(hass, "sentinel_start") == "setup_failed"


async def test_three_setup_unload_cycles_leave_stable_counts(hass):
    entry = await _add_entry(hass)
    loaded, unloaded = [], []
    for _ in range(3):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        loaded.append((len(_services(hass)), dict(hass.bus.async_listeners())))
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        unloaded.append((len(_services(hass)), dict(hass.bus.async_listeners())))
    assert loaded[0][0] == unloaded[0][0] == SERVICE_COUNT
    assert loaded[0] == loaded[1] == loaded[2]
    assert unloaded[0] == unloaded[1] == unloaded[2]
    assert_no_nova_state_in_hass_data(hass)


# ── Handlers follow the current runtime ─────────────────────────────────────

async def test_reloaded_handlers_use_the_new_client(hass, monkeypatch):
    from custom_components.nova import services
    seen = []

    async def _record(hass_, call, client, *a, **k):
        seen.append(client)
    monkeypatch.setattr(services, "async_analyze_camera", _record)

    entry = await _setup(hass)
    first = entry.runtime_data.client
    await _call(hass, "analyze_camera")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    second = entry.runtime_data.client
    await _call(hass, "analyze_camera")
    assert second is not first
    assert seen == [first, second]


async def test_reloaded_handlers_use_the_new_sentinel(hass, monkeypatch):
    from custom_components.nova.sentinel import NovaSentinel
    stopped = []
    real_stop = NovaSentinel.async_stop

    async def _stop(self):
        stopped.append(self)
        await real_stop(self)
    monkeypatch.setattr(NovaSentinel, "async_stop", _stop)

    entry = await _setup(hass)
    first = entry.runtime_data.sentinel
    await _call(hass, "sentinel_stop")
    assert stopped == [first]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    second = entry.runtime_data.sentinel
    stopped.clear()                    # the reload's own unload stop
    await _call(hass, "sentinel_stop")
    assert second is not first
    assert stopped == [second]


async def test_a_replaced_entry_is_used_not_the_first(hass, monkeypatch):
    """Remove the entry and add a new one: handlers resolve the new entry
    (its data and runtime), never the one they first saw."""
    from custom_components.nova import notify_targets
    seen = []

    async def _send(hass_, config, payload, **kwargs):
        seen.append(config.get("notify_service"))
        return 1
    monkeypatch.setattr(notify_targets, "async_send_configured_notifications", _send)

    base = _make_entry()
    first = await _add_entry(hass)
    hass.config_entries.async_update_entry(
        first, data={**base.data, "notify_service": "notify.first_entry"})
    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    first.runtime_data.runtime_config.pop("notify_service", None)
    await _call(hass, "test_notify")
    assert await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()
    assert len(_services(hass)) == SERVICE_COUNT
    assert await _rejected(hass, "test_notify") == "no_entry"

    second = MockConfigEntry(domain=DOMAIN,
                             data={**base.data, "notify_service": "notify.second_entry"})
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    second.runtime_data.runtime_config.pop("notify_service", None)
    await _call(hass, "test_notify")
    assert seen == ["notify.first_entry", "notify.second_entry"]


async def test_reloaded_handlers_use_the_new_runtime_config(hass, monkeypatch):
    from custom_components.nova import notify_targets
    seen = []

    async def _send(hass_, config, payload, **kwargs):
        seen.append(config.get("notify_service"))
        return 1
    monkeypatch.setattr(notify_targets, "async_send_configured_notifications", _send)

    entry = await _setup(hass)
    old = entry.runtime_data
    old.runtime_config["notify_service"] = "notify.first"
    await _call(hass, "test_notify")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    new = entry.runtime_data
    new.runtime_config["notify_service"] = "notify.second"
    old.runtime_config["notify_service"] = "notify.stale"   # must not be read
    await _call(hass, "test_notify")
    assert new is not old and new.runtime_config is not old.runtime_config
    assert seen == ["notify.first", "notify.second"]


# ── Lifecycle rejection ─────────────────────────────────────────────────────

async def test_no_entry_raises_no_entry(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    with _delegates_recorded() as delegates:
        for service in VALID_DATA:
            assert await _rejected(hass, service) == "no_entry", service
    assert delegates == []


async def test_multiple_entries_raise_multiple_entries(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    for _ in range(2):
        MockConfigEntry(domain=DOMAIN, data=dict(_make_entry().data)).add_to_hass(hass)
    with _delegates_recorded() as delegates:
        for service in ("nap", "lockdown"):
            assert await _rejected(hass, service) == "multiple_entries", service
    assert delegates == []


async def test_unloaded_entry_raises_not_loaded(hass):
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert await _rejected(hass, "nap") == "not_loaded"


async def test_failed_setup_raises_setup_failed(hass):
    entry = await _add_entry(hass)
    with patch("custom_components.nova.reminders.ReminderWatcher.async_start",
               side_effect=RuntimeError("boom")):
        assert await hass.config_entries.async_setup(entry.entry_id) is False
    await hass.async_block_till_done()
    assert await _rejected(hass, "nap") == "setup_failed"


@pytest.mark.parametrize("service", sorted(VALID_DATA))
async def test_every_state_rejects_before_any_delegate(
        hass, nova_events, built, service):
    """Every service, in every non-LOADED state: the translated error, and
    no delegate, bus event or proactive object."""
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    nova_events.clear()
    built.clear()
    with _delegates_recorded() as delegates:
        for state, key in REJECTED_STATES:
            entry.mock_state(hass, state)
            assert await _rejected(hass, service) == key, (service, state)
    assert delegates == [] and nova_events == [] and built == []
    assert not hasattr(entry, "runtime_data")
    assert_no_nova_state_in_hass_data(hass)


async def test_real_setup_in_progress_rejects_ordinary_services(hass):
    """During a real setup (after the runtime exists, before LOADED), an
    ordinary call reports reloading."""
    from custom_components.nova import proactive_audio
    seen = {}

    async def _during_setup(hass_, runtime):
        seen["state"] = entry.state
        with _delegates_recorded() as delegates:
            for service in ("nap", "lockdown"):
                seen[service] = await _rejected(hass, service)
        seen["delegates"] = delegates

    entry = await _add_entry(hass)
    with patch.object(proactive_audio, "mark_boot_ready", _during_setup):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert seen == {"state": ConfigEntryState.SETUP_IN_PROGRESS, "nap": "reloading",
                    "lockdown": "reloading",
                    "delegates": []}


async def test_real_unload_in_progress_rejects_services(hass):
    from custom_components import nova
    seen = {}
    real = nova.async_unload_proactive_audio

    async def _during_unload(hass_, entry_):
        seen["state"] = entry.state
        with _delegates_recorded() as delegates:
            seen["nap"] = await _rejected(hass, "nap")
            seen["lockdown"] = await _rejected(hass, "lockdown")
        seen["delegates"] = delegates
        await real(hass_, entry_)

    entry = await _setup(hass)
    with patch.object(nova, "async_unload_proactive_audio", _during_unload):
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert seen == {"state": ConfigEntryState.UNLOAD_IN_PROGRESS,
                    "nap": "reloading", "lockdown": "reloading", "delegates": []}


async def test_loaded_entry_without_runtime_raises_runtime_unavailable(hass):
    from custom_components.nova.runtime import NovaRuntimeUnavailable
    entry = await _setup(hass)
    runtime = entry.runtime_data
    object.__delattr__(entry, "runtime_data")
    try:
        assert entry.state is ConfigEntryState.LOADED
        with _delegates_recorded() as delegates:
            for service in ("nap", "lockdown"):
                with pytest.raises(NovaRuntimeUnavailable):
                    await _call(hass, service)
    finally:
        entry.runtime_data = runtime
    assert delegates == []


# ── Return values and events ────────────────────────────────────────────────

async def test_services_return_none_and_keep_their_events(hass):
    from custom_components.nova import sleep_detection
    await _setup(hass)
    events = []

    @callback
    def _record(event):
        events.append((event.event_type, dict(event.data)))
    for name in ("nova_observer_nap", "nova_observer_status"):
        hass.bus.async_listen(name, _record)
    try:
        assert await _call(hass, "nap", {"duration_minutes": 5}) is None
        assert await _call(hass, "observer_status") is None
        await hass.async_block_till_done()
    finally:
        sleep_detection.clear_nap()
    assert events[0] == ("nova_observer_nap", {"duration_minutes": 5})
    assert events[1][0] == "nova_observer_status"
    assert {"running", "sleeping", "sleep_reason", "bedroom_areas"} <= set(events[1][1])


async def test_a_retired_service_still_runs_and_raises_one_notice(hass, monkeypatch):
    """Nothing is retired right now, so mark one service retired for the test."""
    from homeassistant.helpers import issue_registry as ir
    from custom_components.nova import repair_notices
    monkeypatch.setattr(repair_notices, "_retired_seen", set())
    monkeypatch.setitem(repair_notices.RETIRED_SERVICES, "observer_status", "script.turn_on")
    await _setup(hass)
    events = []

    @callback
    def _record(event):
        events.append(dict(event.data))
    hass.bus.async_listen("nova_observer_status", _record)
    assert await _call(hass, "observer_status") is None
    assert await _call(hass, "observer_status") is None
    await hass.async_block_till_done()
    assert len(events) == 2                       # still works, twice
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "retired_service_observer_status")
    assert issue is not None and issue.translation_key == "retired_service"
    assert issue.translation_placeholders == {"service": "nova.observer_status",
                                              "replacement": "script.turn_on"}


async def test_ordinary_services_raise_no_retirement_notice(hass, monkeypatch):
    from homeassistant.helpers import issue_registry as ir
    from custom_components.nova import repair_notices
    monkeypatch.setattr(repair_notices, "_retired_seen", set())
    await _setup(hass)
    assert await _call(hass, "observer_status") is None
    await hass.async_block_till_done()
    issues = ir.async_get(hass).issues
    assert not [i for (d, i) in issues if d == DOMAIN and i.startswith("retired_service_")]


# ── Safety paths ────────────────────────────────────────────────────────────

async def test_lockdown_after_unload_does_not_rebuild_a_manager(hass, monkeypatch):
    from custom_components.nova import cognitive_core
    built, requested = [], []
    real_ensure = cognitive_core._ensure_lockdown_mgr

    def _ensure(hass_):
        built.append(1)
        return real_ensure(hass_)

    async def _request(on, reason="", hass=None):
        requested.append(on)
        return True
    monkeypatch.setattr(cognitive_core, "_ensure_lockdown_mgr", _ensure)
    monkeypatch.setattr(cognitive_core, "request_lockdown", _request)

    entry = await _setup(hass)
    await _call(hass, "lockdown", {"state": "on"})
    assert requested == [True]          # loaded: reaches the existing delegate
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert cognitive_core._CORE.lockdown_mgr is None
    built.clear()

    assert await _rejected(hass, "lockdown") == "not_loaded"
    assert cognitive_core._CORE.lockdown_mgr is None and cognitive_core._CORE.hass is None
    assert built == [] and requested == [True]


# ── Blueprints ──────────────────────────────────────────────────────────────

BLUEPRINTS = sorted((pathlib.Path(__file__).resolve().parents[2]
                     / "custom_components" / "nova" / "blueprints").glob("*.yaml"))


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.name)
async def test_blueprints_validate_while_services_are_registered(hass, path):
    from homeassistant.components.automation.config import (
        AUTOMATION_BLUEPRINT_SCHEMA,
        async_validate_config_item,
    )
    from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
    from homeassistant.util.yaml import load_yaml_dict

    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "analyze_on_event")

    blueprint = Blueprint(load_yaml_dict(str(path)), expected_domain="automation",
                          schema=AUTOMATION_BLUEPRINT_SCHEMA)
    assert blueprint.validate() is None
    inputs = {name: {"trigger_entity": "binary_sensor.doorbell",
                     "camera_entity": "camera.front"}.get(name, spec.get("default"))
              for name, spec in blueprint.inputs.items()}
    config = {"use_blueprint": {"path": path.name, "input": inputs}}
    substituted = BlueprintInputs(blueprint, config).async_substitute()
    substituted.pop("use_blueprint", None)
    validated = await async_validate_config_item(hass, "blueprint_test", substituted)
    assert validated is not None and validated.validation_error is None

    text = path.read_text(encoding="utf-8")
    assert "nova.analyze_on_event" in text


# ── hass.data ───────────────────────────────────────────────────────────────

async def test_no_nova_state_or_registration_flag_in_hass_data(hass):
    keys_before = set(hass.data)
    entry = await _setup(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert_no_nova_state_in_hass_data(hass, entry.runtime_data)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert_no_nova_state_in_hass_data(hass)
    added = set(hass.data) - keys_before
    assert not [k for k in added if "nova" in str(k).lower()], added


# ── service field checks, live settings and the doorbell diagnostic ───────────

_BLUEPRINTS = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "blueprints"


async def _call_sf(hass, service, data=None):
    return await hass.services.async_call(DOMAIN, service, data or {}, blocking=True)


def _events(hass, event_type):
    seen = []

    @callback
    def _record(event):
        seen.append(dict(event.data))
    hass.bus.async_listen(event_type, _record)
    return seen


@pytest.fixture
def lockdown_requests(monkeypatch):
    from custom_components.nova import cognitive_core
    seen = []

    async def _request(on, reason="", hass=None):
        seen.append(on)
        return True
    monkeypatch.setattr(cognitive_core, "request_lockdown", _request)
    return seen


@pytest.mark.parametrize("state, engaged", [
    ("on", True), ("engage", True), ("lock", True), (True, True), ("true", True),
    ("off", False), ("lift", False), ("unlock", False), (False, False), ("false", False),
])
async def test_lockdown_accepts_on_off_and_the_older_words(hass, lockdown_requests,
                                                           state, engaged):
    await _setup(hass)
    await _call_sf(hass, "lockdown", {"state": state})
    assert lockdown_requests == [engaged]


@pytest.mark.parametrize("data", [{"state": "engaged"}, {"state": "maybe"}, {},
                                  {"enabled": "on"}])
async def test_lockdown_refuses_anything_else(hass, lockdown_requests, data):
    """Fixed: an unknown word (a typo such as "engaged") used to lift the
    lockdown, and a call without a state used to engage it."""
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await _call_sf(hass, "lockdown", data)
    assert lockdown_requests == []


@pytest.fixture
def knowledge_calls(monkeypatch):
    from custom_components.nova import knowledge
    seen = []
    monkeypatch.setattr(knowledge, "remember",
                        lambda *a, **k: seen.append(("remember", a, k)) or {"id": 1})
    monkeypatch.setattr(knowledge, "forget",
                        lambda **k: seen.append(("forget", k)) or 1)
    return seen


@pytest.mark.parametrize("service, data", [
    ("remember", {"key": "bin day"}),
    ("remember", {"key": "bin day", "value": "   "}),
    ("remember", {"key": "bin day", "value": "Tuesday", "kind": "rumour"}),
    ("remember", {"key": "bin day", "value": "Tuesday", "ttl_seconds": "soon"}),
    ("remember", {"key": "bin day", "value": "Tuesday", "ttl_seconds": -5}),
    ("forget", {"id": "abc"}),
    ("forget", {"id": -1}),
    ("train_doorbell_backlog", {"limit": 0}),
    ("train_doorbell_backlog", {"limit": 501}),
])
async def test_bad_input_is_an_error_not_a_silent_no_op(hass, knowledge_calls, service, data):
    """Fixed: these services had no schema, so a bad call did nothing and
    reported success."""
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await _call_sf(hass, service, data)
    assert knowledge_calls == []


async def test_good_knowledge_calls_still_store_and_forget(hass, knowledge_calls):
    await _setup(hass)
    await _call_sf(hass, "remember", {"key": " bin day ", "value": "Tuesday",
                                   "kind": "preference", "ttl_seconds": "60"})
    await _call_sf(hass, "forget", {"id": "7"})
    await _call_sf(hass, "forget", {"key": "bin day", "subject": "household"})
    kind, args, kwargs = knowledge_calls[0]
    assert (kind, args) == ("remember", ("bin day", "Tuesday"))
    assert kwargs["kind"] == "preference" and kwargs["ttl_seconds"] == 60.0
    assert knowledge_calls[1:] == [
        ("forget", {"fact_id": 7, "subject": None, "key": None}),
        ("forget", {"fact_id": None, "subject": "household", "key": "bin day"})]


async def test_backlog_limit_inside_the_range_reaches_the_scan(hass, monkeypatch):
    from custom_components.nova import doorbell_training
    limits = []

    async def _scan(hass_, analyze, honorific, limit=40):
        limits.append(limit)
        return {"ok": False, "reason": "nothing recorded"}
    monkeypatch.setattr(doorbell_training, "scan_backlog", _scan)
    await _setup(hass)
    await _call_sf(hass, "train_doorbell_backlog", {"limit": "500"})
    await _call_sf(hass, "train_doorbell_backlog")
    assert limits == [500, 40]


async def test_observer_status_reads_the_panel_settings(hass, monkeypatch):
    """Fixed: bedrooms and quiet hours came from the setup entry only, so a
    panel configured install reported the defaults."""
    from custom_components.nova import sleep_detection
    asked = []

    def _is_sleeping(hass_, **kwargs):
        asked.append(kwargs)
        return False, "awake"
    monkeypatch.setattr(sleep_detection, "is_sleeping", _is_sleeping)
    entry = await _setup(hass)
    entry.runtime_data.runtime_config.update({
        "bedroom_areas": ["loft"], "observer_quiet_start": "23:30",
        "observer_quiet_end": "06:15"})
    events = _events(hass, "nova_observer_status")
    await _call_sf(hass, "observer_status")
    await hass.async_block_till_done()
    assert asked == [{"bedroom_area_ids": ["loft"], "quiet_start": "23:30",
                      "quiet_end": "06:15"}]
    assert events[0]["bedroom_areas"] == ["loft"]


async def test_routing_diagnostic_reads_the_panel_settings(hass, caplog):
    """Fixed: the broadcast group and TTS engine came from the setup entry
    only."""
    entry = await _setup(hass)
    entry.runtime_data.runtime_config.update({
        "broadcast_group": "media_player.downstairs",
        "announcement_speakers": ["media_player.hall"],
        "satellite_pairings": '{"assist_satellite.hall": "media_player.hall"}'})
    with caplog.at_level(logging.WARNING, logger="custom_components.nova.services"):
        await _call_sf(hass, "test_routing")
    text = caplog.text
    assert "Broadcast group (config): 'media_player.downstairs'" in text
    assert "Announcement speakers (panel): ['media_player.hall']" in text
    assert "Satellite pairings (panel): {'assist_satellite.hall': 'media_player.hall'}" in text


async def _doorbell_automation(hass, folder: str, alias: str = "doorbell look"):
    target = Path(hass.config.path("blueprints", "automation", folder))
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(_BLUEPRINTS / "nova_doorbell_analyze.yaml", target)
    hass.states.async_set("event.front_doorbell", "unknown")
    assert await async_setup_component(hass, "automation", {"automation": {
        "alias": alias,
        "use_blueprint": {"path": f"{folder}/nova_doorbell_analyze.yaml", "input": {
            "trigger_entity": "event.front_doorbell", "camera_entity": "camera.door"}},
    }})
    await hass.async_block_till_done()


async def test_doorbell_diagnostic_finds_the_blueprint_automation(hass):
    """Fixed: the check looked for one fixed automation name, so it failed on
    every install that didn't use that exact name."""
    await _setup(hass)
    await _doorbell_automation(hass, "anyone")
    events = _events(hass, "nova_doorbell_diag")
    await _call_sf(hass, "diagnose_doorbell")
    await hass.async_block_till_done()
    checks = {c["check"]: c for c in events[0]["checks"]}
    assert checks["automation exists"]["ok"] is True
    assert checks["automation exists"]["entity_id"] == "automation.doorbell_look"
    assert checks["automation enabled"]["ok"] is True
    assert checks["analyze_on_event service"]["ok"] is True


async def test_doorbell_diagnostic_reports_a_disabled_automation(hass):
    await _setup(hass)
    await _doorbell_automation(hass, "elsewhere")
    await hass.services.async_call("automation", "turn_off",
                                   {"entity_id": "automation.doorbell_look"}, blocking=True)
    events = _events(hass, "nova_doorbell_diag")
    await _call_sf(hass, "diagnose_doorbell")
    await hass.async_block_till_done()
    assert events[0]["verdict"] == "Automation exists but is disabled"


async def test_doorbell_diagnostic_without_the_blueprint(hass):
    await _setup(hass)
    events = _events(hass, "nova_doorbell_diag")
    await _call_sf(hass, "diagnose_doorbell")
    await hass.async_block_till_done()
    assert events[0]["checks"][0]["ok"] is False
    assert events[0]["verdict"].startswith("Automation missing")
