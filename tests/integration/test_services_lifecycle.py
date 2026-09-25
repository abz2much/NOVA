"""Phase 4: Nova's services live for the process lifetime (PHACC).

async_setup registers all 32 nova.* services once, at integration scope.
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
* nova.speak buffers only in the narrow SETUP_IN_PROGRESS boot window and
  replays in order, and nothing outside it creates a buffer,
* safety paths (routine confirmation and audit, lockdown) are unchanged,
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
    PROACTIVE_FIELDS,
    _add_entry,
    _no_real_config,
    _restore_nova_config,
    _setup,
    assert_no_nova_state_in_hass_data,
)
from .test_wiring_smoke import _make_entry

SERVICE_COUNT = 32

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
    "process_intent": {"phrase": "lights on", "target_area": "office"},
    "remember": {"key": "k", "value": "v"},
    "replay_policy": {"kind": "doorbell"},
    "restore": {},
    "routine": {"name": "goodnight"},
    "scene_by_intent": {"intent": "relax"},
    "sentinel_start": {},
    "sentinel_stop": {},
    "shush": {},
    "speak": {"message": "hello", "target_area": "office"},
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
        proactive_audio, replay, services, sleep_detection, tts_helper,
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
        (services, "async_briefing"), (services, "async_activate_by_intent"),
        (services, "async_run_routine"), (services, "async_add_reminder_service"),
        (services, "async_summarise"), (cognitive_core, "request_lockdown"),
        (automation_creator, "create_automation"), (observer, "start"), (observer, "stop"),
        (notify_targets, "async_send_configured_notifications"),
        (doorbell_training, "scan_backlog"), (package_monitor, "periodic_check"),
        (proactive_audio, "_dispatch_speak"), (tts_helper, "async_announce"),
    ]
    sync_targets = [
        (services, "purge_old_records"), (services, "get_stats"),
        (cognitive_core, "_ensure_lockdown_mgr"), (knowledge, "remember"),
        (knowledge, "forget"), (output_gate, "shush"), (output_gate, "unshush"),
        (output_gate, "status"), (sleep_detection, "set_nap"),
        (backup, "create_backup"), (backup, "restore_backup"), (action_log, "start"),
        (replay, "replay_kind"), (proactive_audio, "_intent_router"),
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
    for name in ("AlertBuffer", "LocalIntentRouter", "StateLedger", "EntityLockRegistry"):
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
    for name in ("analyze_camera", "speak", "lockdown"):
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


async def test_reloaded_speak_uses_the_new_runtime(hass, spoken):
    entry = await _setup(hass)
    first = entry.runtime_data
    await _call(hass, "speak")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _call(hass, "speak")
    assert [r for r, _ in spoken] == [first, entry.runtime_data]
    assert entry.runtime_data is not first


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
        for service in ("routine", "speak", "process_intent", "lockdown"):
            assert await _rejected(hass, service) == "multiple_entries", service
    assert delegates == []


async def test_unloaded_entry_raises_not_loaded(hass):
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert await _rejected(hass, "routine") == "not_loaded"


async def test_failed_setup_raises_setup_failed(hass):
    entry = await _add_entry(hass)
    with patch("custom_components.nova.reminders.ReminderWatcher.async_start",
               side_effect=RuntimeError("boom")):
        assert await hass.config_entries.async_setup(entry.entry_id) is False
    await hass.async_block_till_done()
    assert await _rejected(hass, "routine") == "setup_failed"


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
            for service in ("nap", "process_intent", "routine"):
                seen[service] = await _rejected(hass, service)
        seen["delegates"] = delegates
        return []

    entry = await _add_entry(hass)
    with patch.object(proactive_audio, "_reconcile_state_ledger", _during_setup):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert seen == {"state": ConfigEntryState.SETUP_IN_PROGRESS, "nap": "reloading",
                    "process_intent": "reloading", "routine": "reloading",
                    "delegates": []}


async def test_real_unload_in_progress_rejects_services(hass):
    from custom_components import nova
    seen = {}
    real = nova.async_unload_proactive_audio

    async def _during_unload(hass_, entry_):
        seen["state"] = entry.state
        with _delegates_recorded() as delegates:
            seen["speak"] = await _rejected(hass, "speak")
            seen["lockdown"] = await _rejected(hass, "lockdown")
        seen["delegates"] = delegates
        await real(hass_, entry_)

    entry = await _setup(hass)
    with patch.object(nova, "async_unload_proactive_audio", _during_unload):
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert seen == {"state": ConfigEntryState.UNLOAD_IN_PROGRESS,
                    "speak": "reloading", "lockdown": "reloading", "delegates": []}


async def test_loaded_entry_without_runtime_raises_runtime_unavailable(hass):
    from custom_components.nova.runtime import NovaRuntimeUnavailable
    entry = await _setup(hass)
    runtime = entry.runtime_data
    object.__delattr__(entry, "runtime_data")
    try:
        assert entry.state is ConfigEntryState.LOADED
        with _delegates_recorded() as delegates:
            for service in ("nap", "routine", "lockdown", "speak", "process_intent"):
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


# ── Safety paths ────────────────────────────────────────────────────────────

async def test_protected_routine_step_still_needs_confirmation_and_audit(hass, monkeypatch):
    from custom_components.nova import action_log, policy, routines
    unlocked, gates, audit = [], [], []

    async def _unlock(call):
        unlocked.append(dict(call.data))
    hass.services.async_register("lock", "unlock", _unlock)

    async def _gate(hass_, domain, svc, entity, label):
        gates.append((domain, svc, entity))
        return False, "not approved", "denied"

    monkeypatch.setattr(routines, "_load_routines", lambda: {
        "secure": [{"service": "lock.unlock", "data": {"entity_id": "lock.front_door"}}]})
    monkeypatch.setattr(policy, "requires_confirmation", lambda *a, **k: True)
    monkeypatch.setattr(policy, "confirm_gate", _gate)
    monkeypatch.setattr(action_log, "start_many",
                        lambda *a, **k: audit.append(("start_many", a[1], a[2])) or {0: 7})
    monkeypatch.setattr(action_log, "mark_awaiting_approval",
                        lambda rid: audit.append(("awaiting", rid)))
    monkeypatch.setattr(action_log, "set_approval",
                        lambda rid, ar, **k: audit.append(("approval", rid, ar)))
    monkeypatch.setattr(action_log, "set_execution",
                        lambda rid, result, **k: audit.append(("execution", rid, result)))

    entry = await _setup(hass)
    for _ in range(2):                  # before and after a reload
        audit.clear()
        assert await _call(hass, "routine", {"name": "secure"}) is None
        assert audit == [("start_many", "routine:secure", "routine"), ("awaiting", 7),
                         ("approval", 7, "denied"), ("execution", 7, "blocked")]
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert gates == [("lock", "unlock", "lock.front_door")] * 2
    assert unlocked == []


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


async def test_speak_and_intent_after_unload_rebuild_nothing(hass, spoken, built):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    built.clear()
    for service in ("speak", "process_intent"):
        assert await _rejected(hass, service) == "not_loaded"
    for field in PROACTIVE_FIELDS:
        assert getattr(runtime, field) is None, field
    assert built == [] and spoken == []
    assert_no_nova_state_in_hass_data(hass)


# ── nova.speak boot buffer ──────────────────────────────────────────────────

async def test_speak_buffers_in_the_boot_window_and_replays_in_order(hass, spoken):
    """A real setup: calls made after the boot gate opens its buffer and
    before setup marks it ready are queued and replayed in order."""
    from custom_components.nova import proactive_audio
    during = {}

    async def _during_setup(hass_, runtime):
        during["state"] = entry.state
        during["runtime"] = runtime
        for message in ("first", "second", "third"):
            assert await _call(hass, "speak", {"message": message,
                                               "target_area": "office"}) is None
        during["queued"] = runtime.alert_buffer._queue.qsize()
        during["spoken"] = list(spoken)
        return []

    entry = await _add_entry(hass)
    with patch.object(proactive_audio, "_reconcile_state_ledger", _during_setup):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    assert during["state"] is ConfigEntryState.SETUP_IN_PROGRESS
    assert during["runtime"] is runtime
    assert during["queued"] == 3 and during["spoken"] == []
    assert spoken == [(runtime, "first"), (runtime, "second"), (runtime, "third")]
    assert runtime.alert_buffer.ready and runtime.alert_buffer._queue.qsize() == 0


async def test_speak_before_the_runtime_exists_is_rejected(hass, spoken, built):
    from custom_components import nova
    seen = {}

    async def _recognition(hass_):
        seen["runtime"] = hasattr(entry, "runtime_data")
        seen["key"] = await _rejected(hass, "speak")
        seen["built"] = list(built)
        return []

    entry = await _add_entry(hass)
    with patch.object(nova, "register_recognition_listener", _recognition):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert seen == {"runtime": False, "key": "reloading", "built": []}
    assert spoken == []


async def test_speak_before_the_buffer_exists_creates_none(hass, spoken, built):
    from custom_components import nova
    seen = {}
    real_panel = nova.async_register_panel

    async def _panel(hass_):
        runtime = entry.runtime_data
        seen["buffer_before"] = runtime.alert_buffer
        seen["key"] = await _rejected(hass, "speak")
        seen["buffer_after"] = runtime.alert_buffer
        seen["built"] = list(built)
        await real_panel(hass_)

    entry = await _add_entry(hass)
    with patch.object(nova, "async_register_panel", _panel):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert seen["buffer_before"] is None and seen["buffer_after"] is None
    assert seen["key"] == "reloading"
    assert "AlertBuffer" not in seen["built"]
    assert spoken == []


async def test_speak_with_a_ready_buffer_during_setup_is_rejected(hass, spoken):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    assert runtime.alert_buffer.ready
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    try:
        assert await _rejected(hass, "speak") == "reloading"
    finally:
        entry.mock_state(hass, ConfigEntryState.LOADED)
    assert runtime.alert_buffer._queue.qsize() == 0 and spoken == []


@pytest.mark.parametrize("state,key", [
    (ConfigEntryState.UNLOAD_IN_PROGRESS, "reloading"),
    (ConfigEntryState.SETUP_ERROR, "setup_failed"),
    (ConfigEntryState.NOT_LOADED, "not_loaded"),
])
async def test_speak_never_buffers_outside_setup_in_progress(hass, spoken, state, key):
    """Even with the runtime present and its buffer gated, only
    SETUP_IN_PROGRESS may queue."""
    entry = await _setup(hass)
    runtime = entry.runtime_data
    runtime.alert_buffer.begin()
    entry.mock_state(hass, state)
    try:
        assert await _rejected(hass, "speak") == key
    finally:
        entry.mock_state(hass, ConfigEntryState.LOADED)
    assert runtime.alert_buffer._queue.qsize() == 0 and spoken == []


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
