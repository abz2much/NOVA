"""Phase 3B Final: proactive audio owned by the loaded entry, against a real
Home Assistant (PHACC).

Drives the real setup, the real nova.speak / nova.process_intent services
and the real unload to prove:

* the intent router, state ledger, entity-lock registry and alert buffer are
  NovaRuntime fields, built lazily and reused within one loaded entry,
* the domain-level service handlers resolve the loaded entry's runtime and
  fail with NovaRuntimeUnavailable when it is gone, without building
  anything outside the entry,
* nova.speak buffers into the entry's own alert buffer until ready,
* the audit timers' unsubs belong to NovaResources, and the audit flag is
  the runtime's,
* a damaged bridge has no effect,
* unload releases every object, repeated unload is safe, and a reload
  builds new ones.

Announcements and intent routing are replaced with recorders: no speaker or
device is operated, and there are no lock, alarm or cover entities here.
"""
from contextlib import contextmanager

import pytest

from .test_runtime_data import (  # noqa: F401  (autouse fixtures)
    DOMAIN,
    LEGACY_AUDIO_KEYS,
    PROACTIVE_FIELDS,
    _no_real_config,
    _restore_nova_config,
    _setup,
)


@pytest.fixture
def spoken(monkeypatch):
    """Record announcements instead of resolving or driving any speaker."""
    from custom_components.nova import proactive_audio
    calls = []

    async def _announce(hass, runtime, message, area_id, critical):
        calls.append((runtime, message, area_id, critical))

    monkeypatch.setattr(proactive_audio, "_announce", _announce)
    monkeypatch.setattr(proactive_audio, "_resolve_area_id", lambda hass, target: target)
    return calls


@pytest.fixture
def routed(monkeypatch):
    """Record intent routing on the router instance used, touching nothing."""
    from custom_components.nova.intent import LocalIntentRouter
    calls = []

    async def _voice(self, phrase):
        calls.append(("voice", self, phrase))
        return {"handled": False}

    async def _route(self, phrase, target_area, *, user_id=None):
        calls.append(("route", self, phrase))
        return {"ok": True}

    monkeypatch.setattr(LocalIntentRouter, "handle_voice_response", _voice)
    monkeypatch.setattr(LocalIntentRouter, "route", _route)
    return calls


@contextmanager
def _runtime_missing(entry):
    """Simulate a loaded entry that lost its runtime, then put it back."""
    runtime = entry.runtime_data
    object.__delattr__(entry, "runtime_data")
    try:
        yield runtime
    finally:
        entry.runtime_data = runtime


def _no_domain_level_objects(hass):
    for key in LEGACY_AUDIO_KEYS:
        assert key not in hass.data.get(DOMAIN, {}), key


async def _intent(hass, phrase="lights on"):
    await hass.services.async_call(
        DOMAIN, "process_intent", {"phrase": phrase, "target_area": "office"},
        blocking=True)


async def _speak(hass, message="hello"):
    await hass.services.async_call(
        DOMAIN, "speak", {"message": message, "target_area": "office"}, blocking=True)


# ── Ownership and laziness ──────────────────────────────────────────────────

async def test_objects_are_built_lazily_on_the_entry_runtime(hass, routed):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    # Setup itself needs the buffer (boot guard) and the ledger (recovery).
    assert runtime.alert_buffer is not None and runtime.alert_buffer.ready is True
    assert runtime.state_ledger is not None
    # The router and its lock registry wait for first use.
    assert runtime.intent_router is None and runtime.entity_locks is None

    await _intent(hass)
    assert runtime.intent_router is not None
    assert runtime.entity_locks is not None
    assert routed[-1][1] is runtime.intent_router
    _no_domain_level_objects(hass)


async def test_objects_are_reused_within_one_loaded_entry(hass, routed):
    from custom_components.nova import proactive_audio
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await _intent(hass, "one")
    router, ledger, locks = runtime.intent_router, runtime.state_ledger, runtime.entity_locks
    await _intent(hass, "two")
    assert {id(c[1]) for c in routed} == {id(router)}
    assert proactive_audio._intent_router(hass, runtime) is router
    assert proactive_audio._state_ledger(runtime) is ledger
    assert proactive_audio._entity_locks(runtime) is locks
    assert proactive_audio._alert_buffer(runtime) is runtime.alert_buffer
    # The router uses the entry's own ledger and lock registry.
    assert router.ledger is ledger and router.mutex is locks


async def test_speak_buffers_in_the_entry_buffer_until_ready(hass, spoken):
    from custom_components.nova import proactive_audio
    entry = await _setup(hass)
    runtime = entry.runtime_data
    runtime.alert_buffer.begin()                  # re-gate, as a reload does

    await _speak(hass, "queued")
    assert spoken == []
    assert runtime.alert_buffer._queue.qsize() == 1

    await proactive_audio.mark_boot_ready(hass, runtime)
    assert [(c[0], c[1]) for c in spoken] == [(runtime, "queued")]
    assert runtime.alert_buffer._queue.qsize() == 0


async def test_speak_dispatches_with_the_entry_runtime(hass, spoken):
    entry = await _setup(hass)
    await _speak(hass, "now")
    assert [(c[0], c[1]) for c in spoken] == [(entry.runtime_data, "now")]


@pytest.mark.parametrize("service", ["speak", "process_intent"])
async def test_services_fail_visibly_without_the_runtime(hass, spoken, routed, service):
    from custom_components.nova.runtime import NovaRuntimeUnavailable
    entry = await _setup(hass)
    with _runtime_missing(entry) as runtime:
        runtime.alert_buffer.begin()
        with pytest.raises(NovaRuntimeUnavailable):
            if service == "speak":
                await _speak(hass)
            else:
                await _intent(hass)
        # Nothing was buffered, routed or built anywhere else.
        assert runtime.alert_buffer._queue.qsize() == 0
        assert spoken == [] and routed == []
        assert runtime.intent_router is None
        _no_domain_level_objects(hass)


async def test_damaged_bridge_has_no_effect(hass, spoken, routed):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    bridge = hass.data[DOMAIN][entry.entry_id]
    hass.data[DOMAIN][entry.entry_id] = "damaged"
    try:
        await _speak(hass, "still works")
        await _intent(hass)
    finally:
        hass.data[DOMAIN][entry.entry_id] = bridge
    assert [(c[0], c[1]) for c in spoken] == [(runtime, "still works")]
    assert routed[-1][1] is runtime.intent_router


# ── Timers and the audit flag ───────────────────────────────────────────────

async def test_audit_unsubs_are_owned_by_resources(hass):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    mirrored = hass.data[DOMAIN][entry.entry_id]["proactive_audio_unsubs"]
    assert len(mirrored) == 2
    for unsub in mirrored:
        assert unsub in runtime.resources._unsubs


async def test_audit_tick_uses_the_runtime_flag(hass, monkeypatch):
    """The audit callback is run directly (no clock jump, so no other timer
    fires): it holds the runtime's flag while it runs and skips a tick while
    one is in progress. A bridge flag has no effect."""
    from custom_components.nova import proactive_audio
    seen = []
    ticks = []
    real_call_later = proactive_audio.async_call_later

    def _capture(hass_, delay, action):
        ticks.append(action)
        return real_call_later(hass_, delay, action)

    class _Triage:
        def __init__(self, hass_, honorific=""):
            pass

        def evaluate(self):
            seen.append(runtime.audit_running)
            return {"alert_required": False}

    async def _no_predictor(hass_, predictor):
        return None

    monkeypatch.setattr(proactive_audio, "async_call_later", _capture)
    monkeypatch.setattr(proactive_audio, "InfrastructureTriage", _Triage)
    monkeypatch.setattr(proactive_audio, "_run_predictor", _no_predictor)
    entry = await _setup(hass)
    runtime = entry.runtime_data
    hass.data[DOMAIN][entry.entry_id]["_audit_running"] = True
    (run_audit,) = ticks

    await run_audit()
    assert seen == [True]                         # flag held during the tick
    assert runtime.audit_running is False         # and released after it

    runtime.audit_running = True                  # a tick still in progress
    await run_audit()
    assert seen == [True]                         # the next tick did not overlap
    runtime.audit_running = False

    from custom_components.nova.runtime import NovaRuntimeUnavailable
    with _runtime_missing(entry):
        with pytest.raises(NovaRuntimeUnavailable):
            await run_audit()
    assert seen == [True]


# ── Unload and reload ───────────────────────────────────────────────────────

async def test_unload_releases_everything_and_repeats_safely(hass, routed):
    from custom_components.nova import proactive_audio
    from custom_components.nova.runtime import clear_runtime
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await _intent(hass)
    assert all(getattr(runtime, f) is not None for f in PROACTIVE_FIELDS)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    for field in PROACTIVE_FIELDS:
        assert getattr(runtime, field) is None, field
    assert runtime.audit_running is False
    assert runtime.resources._unsubs == []        # audit timers cancelled
    assert not hass.services.has_service(DOMAIN, "speak")
    assert not hass.services.has_service(DOMAIN, "process_intent")
    assert not hasattr(entry, "runtime_data")
    _no_domain_level_objects(hass)

    # Repeating the release is safe.
    await proactive_audio.async_unload_proactive_audio(hass, entry)
    clear_runtime(hass, entry)
    _no_domain_level_objects(hass)


async def test_reload_builds_new_objects(hass, routed):
    entry = await _setup(hass)
    old = entry.runtime_data
    await _intent(hass)
    old_objects = [getattr(old, f) for f in PROACTIVE_FIELDS]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    new = entry.runtime_data
    assert new is not old
    await _intent(hass)
    for field, before in zip(PROACTIVE_FIELDS, old_objects):
        assert getattr(new, field) is not None, field
        assert getattr(new, field) is not before, field
    assert routed[-1][1] is new.intent_router
