"""Failure-injection tests for the three late, unguarded steps in
async_setup_entry (__init__.py): async_forward_entry_setups, sentinel.
async_start(), and reminder_watcher.async_start(). By the time any of these
three runs, __init__.py has already: registered camera/Eufy bus listeners,
scheduled every periodic sweep on NovaScheduler, stored the resource
registry + scheduler on the entry's NovaRuntime, and registered the
services set up by _register_services() (analyze_camera, briefing, routine,
etc.) — but NOT the "speak" service, which async_setup_proactive_audio()
registers even later, after all three of these steps. None of it was
covered by tests/unit/'s fakes (they don't exercise __init__.py at all).

These tests were first written to OBSERVE the failure behaviour before any
fix existed: a raised exception at any of the three points left every one
of the above completely un-torn-down, because HA does not call
async_unload_entry for you when async_setup_entry raises — only an
explicit unload/remove does that. __init__.py now wraps exactly those three
calls in a try/except that runs async_unload_entry (already fail-safe and
idempotent — that's what NovaResources.close_all() is for) before
re-raising, so the entry still ends up SETUP_ERROR (unchanged from HA's
point of view) but nothing is left behind. These tests now lock in that
corrected contract; do not weaken them back to describing the leak.
"""
from contextlib import contextmanager
from unittest.mock import patch

from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import DOMAIN, _make_entry

_CLEAN = {
    "runtime_data_present": False,
    "nova_hass_data_present": False,
    "service_registered": False,
    "camera_listeners": 0,
    "automation_trigger_listeners": 0,
    "resource_unsubs": 0,
    "resource_closeables": 0,
    "scheduler_jobs": [],
}


@contextmanager
def _capture_owners():
    """Record the NovaResources and NovaScheduler setup builds, so the
    snapshot can inspect them after the failed setup has released the
    runtime that held them."""
    from custom_components.nova.resources import NovaResources
    from custom_components.nova.scheduler import NovaScheduler
    owners: dict[str, list] = {"resources": [], "scheduler": []}

    def _recording(cls, key):
        real = cls.__init__

        def _init(self, *a, **kw):
            real(self, *a, **kw)
            owners[key].append(self)
        return _init

    with patch.object(NovaResources, "__init__", _recording(NovaResources, "resources")), \
         patch.object(NovaScheduler, "__init__", _recording(NovaScheduler, "scheduler")):
        yield owners


def _left_behind(hass, entry, owners) -> dict:
    """Snapshot of everything __init__.py registers before the three late
    steps, so each test can confirm none of it survives a failure there."""
    assert len(owners["resources"]) == 1 and len(owners["scheduler"]) == 1
    resources = owners["resources"][0]
    sched = owners["scheduler"][0]
    return {
        "runtime_data_present": hasattr(entry, "runtime_data"),
        "nova_hass_data_present": DOMAIN in hass.data,
        "service_registered": hass.services.has_service(DOMAIN, "analyze_camera"),
        "camera_listeners": hass.bus.async_listeners().get("nest_event", 0),
        # Phase 3: the automation probation listener registers alongside the
        # camera listeners, before all three late steps below — it must be
        # torn down by the same async_unload_entry-on-failure path.
        "automation_trigger_listeners": hass.bus.async_listeners().get("automation_triggered", 0),
        "resource_unsubs": len(resources._unsubs),
        "resource_closeables": len(resources._closeables),
        "scheduler_jobs": sched.task_names(),
    }


async def test_forward_entry_setups_failure_leaves_nothing_behind(hass):
    """Inject a failure in the platform-forward step (the conversation
    platform), the first of the three late calls."""
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)

    with patch.object(hass.config_entries, "async_forward_entry_setups",
                       side_effect=RuntimeError("boom")), _capture_owners() as owners:
        result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert result is False
    assert _left_behind(hass, entry, owners) == _CLEAN


async def test_sentinel_async_start_failure_leaves_nothing_behind(hass):
    """Inject a failure in sentinel.async_start() — runs after the platform
    forward succeeds, so this additionally proves the conversation entity
    itself gets torn down too, not just what
    test_forward_entry_setups_failure_leaves_nothing_behind already covers."""
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)

    with patch("custom_components.nova.sentinel.NovaSentinel.async_start",
               side_effect=RuntimeError("boom")), _capture_owners() as owners:
        result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert result is False
    assert _left_behind(hass, entry, owners) == _CLEAN

    # The conversation platform, forwarded just before this step, must be
    # unloaded too. Its entity-registry record legitimately survives unload
    # (HA keeps registry entries across reloads by design — dropped only on
    # entry removal), and normal HA unload leaves a restorable entity's
    # state as "unavailable" rather than removing it outright — the same
    # thing that happens to any integration's entities on a plain unload —
    # so that, not a vanished state, is the real signal it was torn down.
    from homeassistant.const import STATE_UNAVAILABLE
    entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, entry.entry_id)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None and state.state == STATE_UNAVAILABLE


async def test_reminder_watcher_async_start_failure_leaves_nothing_behind(hass):
    """Inject a failure in reminder_watcher.async_start() — the last of the
    three late steps, running after sentinel.async_start() has already
    succeeded, so this additionally proves sentinel gets stopped too."""
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)

    sentinels: list = []
    from custom_components.nova.sentinel import NovaSentinel
    real_init = NovaSentinel.__init__

    def _capture_init(self, *a, **kw):
        real_init(self, *a, **kw)
        sentinels.append(self)

    with patch("custom_components.nova.reminders.ReminderWatcher.async_start",
               side_effect=RuntimeError("boom")), \
         patch.object(NovaSentinel, "__init__", _capture_init), \
         _capture_owners() as owners:
        result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert result is False
    assert _left_behind(hass, entry, owners) == _CLEAN

    # Sentinel, started just before this step, must be stopped too.
    assert sentinels and sentinels[0]._active is False
