"""Phase 0B lifecycle cleanup against a real Home Assistant (PHACC).

What survives unload or reload is only provable with the real setup and
unload path, so these drive async_setup_entry / async_unload_entry for real:

* the lockdown alarm listener (cognitive_core.ensure_lockdown runs even with
  the observer off) is removed on unload, and cognitive_core.stop() runs
  exactly once whether or not the observer is running,
* the voice-recognition provider is cleared on unload, and a reload leaves
  exactly the new one,
* nova.test_routing exists after setup and is gone after unload,
* the bootstrap task is cancelled on unload and a reload doesn't stack a
  second one,
* repeated unload and a failed observer start leave nothing behind.

No device is touched: there are no lock, alarm or cover entities here, and
nothing arms, disarms, unlocks or opens anything.
"""
import asyncio
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import DOMAIN, _make_entry


@pytest.fixture(autouse=True)
def _restore_nova_config(hass):
    """Setup writes the entry's options into <config dir>/nova/config.json,
    and PHACC's testing_config dir is shared by every test in the run. Put
    the file back afterwards so observer_enabled here can't leak into other
    tests, and drop nova_config's cached copy."""
    import os
    from custom_components.nova import nova_config
    path = hass.config.path("nova", "config.json")
    before = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
    yield
    if before is None:
        if os.path.exists(path):
            os.remove(path)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(before)
    nova_config._cache = {}
    nova_config._loaded = False


def _live_bootstrap_tasks() -> list:
    return [t for t in asyncio.all_tasks()
            if t.get_name() == "nova_bootstrap" and not t.done()]


@pytest.fixture
def stop_calls(monkeypatch):
    """Count cognitive_core.stop() calls while still running the real one."""
    from custom_components.nova import cognitive_core
    real_stop = cognitive_core.stop
    calls = []

    async def _counting_stop():
        calls.append(1)
        await real_stop()

    monkeypatch.setattr(cognitive_core, "stop", _counting_stop)
    return calls


async def _setup(hass, **options):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    if options:
        entry = type(entry)(domain=entry.domain, data=dict(entry.data), options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_observer_disabled_unload_removes_lockdown_listener(hass, stop_calls):
    from custom_components.nova import cognitive_core
    entry = await _setup(hass)
    assert hass.data[DOMAIN][entry.entry_id]["observer_running"] is False
    assert cognitive_core._CORE.alarm_unsub is not None
    listeners_loaded = hass.bus.async_listeners().get("state_changed", 0)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert cognitive_core._CORE.alarm_unsub is None
    assert hass.bus.async_listeners().get("state_changed", 0) < listeners_loaded
    assert len(stop_calls) == 1


async def test_observer_enabled_unload_stops_core_once(hass, stop_calls):
    from custom_components.nova import observer

    async def _fake_start(hass_, config):
        observer._STATE.running = True

    with patch.object(observer, "start", _fake_start):
        entry = await _setup(hass, observer_enabled=True)
    assert hass.data[DOMAIN][entry.entry_id]["observer_running"] is True

    with patch.object(observer, "stop", wraps=observer.stop) as obs_stop:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert obs_stop.await_count == 1
    assert len(stop_calls) == 1   # observer.stop owned it; unload didn't repeat it


async def test_reload_rebuilds_lockdown_with_new_config(hass, stop_calls):
    """Observer disabled. The alarm entity named here doesn't exist, so the
    startup alarm sync finds nothing to act on; no lockdown action runs."""
    from custom_components.nova import cognitive_core
    entry = await _setup(hass, security_alarm_entity="alarm_control_panel.first")
    mgr_first = cognitive_core._CORE.lockdown_mgr
    assert mgr_first.config.get("security_alarm_entity") == "alarm_control_panel.first"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert cognitive_core._CORE.lockdown_mgr is None
    assert cognitive_core._CORE.hass is None
    listeners_unloaded = hass.bus.async_listeners().get("state_changed", 0)

    # Changed the way the panel changes it: config.json is Nova's source of
    # truth and wins over entry options (nova_config.py).
    from custom_components.nova import nova_config
    await hass.async_add_executor_job(
        nova_config.set, "security_alarm_entity", "alarm_control_panel.second")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mgr_second = cognitive_core._CORE.lockdown_mgr
    assert mgr_second is not mgr_first
    assert mgr_second.hass is hass and cognitive_core._CORE.hass is hass
    assert mgr_second.config.get("security_alarm_entity") == "alarm_control_panel.second"
    assert cognitive_core._CORE.alarm_unsub is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.bus.async_listeners().get("state_changed", 0) == listeners_unloaded
    assert cognitive_core._CORE.lockdown_mgr is None
    assert len(stop_calls) == 2   # once per unload


async def test_repeated_unload_is_safe(hass, stop_calls):
    from custom_components.nova import async_unload_entry, cognitive_core
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # A second direct unload of the same entry must not raise.
    await async_unload_entry(hass, entry)
    assert cognitive_core._CORE.alarm_unsub is None
    assert not hass.services.has_service(DOMAIN, "test_routing")


async def test_failed_observer_start_leaves_nothing_behind(hass, stop_calls):
    from custom_components.nova import identity, observer

    async def _boom(hass_, config):
        raise RuntimeError("observer failed to start")

    assert await async_setup_component(hass, "homeassistant", {})
    base = _make_entry()
    entry = type(base)(domain=base.domain, data=dict(base.data),
                       options={"observer_enabled": True})
    entry.add_to_hass(hass)
    with patch.object(observer, "start", _boom):
        result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert result is False
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    assert not hass.services.has_service(DOMAIN, "test_routing")
    assert not hass.services.has_service(DOMAIN, "analyze_camera")
    assert not identity.has_voice_provider()
    assert _live_bootstrap_tasks() == []


async def test_voice_provider_cleared_on_unload_and_single_after_reload(hass):
    from custom_components.nova import identity
    entry = await _setup(hass)
    assert identity.has_voice_provider()
    first = identity._VOICE_PROVIDER

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not identity.has_voice_provider()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert identity.has_voice_provider()
    assert identity._VOICE_PROVIDER is not first   # the new one, not a stale one

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not identity.has_voice_provider()


async def test_test_routing_registered_then_removed(hass):
    entry = await _setup(hass)
    assert hass.services.has_service(DOMAIN, "test_routing")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "test_routing")


async def test_bootstrap_task_cancelled_on_unload_and_not_stacked(hass):
    """The pipeline-agent repair normally returns at once under PHACC, so it's
    held open here to stand in for the real 60-second agent wait (or a
    Supervisor voice download) still running when Nova unloads."""
    from custom_components.nova import bootstrap
    started = []

    async def _slow_repair(hass_):
        started.append(1)
        await asyncio.Event().wait()

    with patch.object(bootstrap, "async_ensure_pipeline_agent", _slow_repair):
        entry = await _setup(hass)
        assert len(_live_bootstrap_tasks()) == 1

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert len(started) == 2
        assert len(_live_bootstrap_tasks()) == 1   # reload cancelled the first

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert _live_bootstrap_tasks() == []
