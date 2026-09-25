"""Phase 3B.1 observer state and unload on NovaRuntime, against a real
Home Assistant (PHACC).

Drives the real setup, services, WebSocket commands and unload to prove:

* setup records the observer state on the runtime (on when enabled, off when
  disabled), and a failed observer start is cleaned up with no stale state,
* nova.observer_start / nova.observer_stop and nova/update_config's
  observer_enabled toggle update the runtime,
* nova/get_panel_data reports the runtime's state, and fails loudly for a
  loaded entry with no runtime instead of reporting "off",
* the hass.data bridge copy matches the runtime after every transition,
* unload tears down the runtime's own sentinel, reminder watcher, resources
  and observer even when the bridge is missing or damaged,
* repeated unload and unload after a partial setup stay safe,
* reload builds a fresh runtime with fresh observer state.

The observer is faked: no state listener, classifier or LLM runs. No device
is touched: there are no lock, alarm or cover entities here.
"""
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState

from .test_runtime_data import (  # noqa: F401  (autouse fixtures)
    DOMAIN,
    _add_entry,
    _assert_released,
    _no_real_config,
    _restore_nova_config,
    _setup,
)


@pytest.fixture
def fake_observer(monkeypatch):
    """Record observer start/stop calls without running the real observer.
    stop() still stops the cognitive core, as the real one does, so its
    lockdown listener never outlives a test."""
    from custom_components.nova import cognitive_core, observer
    calls: list[str] = []

    async def _start(hass, config):
        calls.append("start")

    async def _stop():
        calls.append("stop")
        await cognitive_core.stop()

    monkeypatch.setattr(observer, "start", _start)
    monkeypatch.setattr(observer, "stop", _stop)
    return calls


@pytest.fixture
def no_panel_databases(monkeypatch):
    """nova/get_panel_data also reads stats from SQLite files at fixed
    /config paths. Those tiles are not under test here, so stub them and
    keep this file off the real /config."""
    from custom_components.nova import websocket
    for name, value in (("_get_knowledge_stats", {}), ("_get_observer_stats", {}),
                        ("_get_announcements_today", 0), ("_get_memory_stats", {}),
                        ("_get_suggestions", []), ("_get_goals", [])):
        monkeypatch.setattr(websocket, name, lambda value=value: value)


def _assert_state(hass, entry, running: bool):
    """The runtime holds the state; the bridge copy matches it."""
    assert entry.runtime_data.observer_running is running
    assert hass.data[DOMAIN][entry.entry_id]["observer_running"] is running


async def _panel_observer_state(hass, hass_ws_client) -> dict:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/get_panel_data"})
    resp = await client.receive_json()
    assert resp["success"], resp
    return resp["result"]["status"]["observer"]


async def _update_config(hass, hass_ws_client, key, value) -> dict:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "nova/update_config", "key": key, "value": value})
    return await client.receive_json()


# ── Setup ───────────────────────────────────────────────────────────────────

async def test_observer_disabled_at_setup_stays_off(hass, fake_observer):
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    _assert_state(hass, entry, False)
    assert fake_observer == []


async def test_observer_enabled_at_setup_turns_runtime_on(hass, fake_observer):
    entry = await _setup(hass, observer_enabled=True)
    _assert_state(hass, entry, True)
    assert fake_observer == ["start"]


async def test_failed_observer_start_leaves_no_state(hass, monkeypatch):
    """The partial start is stopped by unload, then runtime and bridge go."""
    from custom_components.nova import cognitive_core, observer
    calls, seen = [], []

    async def _boom(hass_, config):
        seen.append(entry.runtime_data)
        raise RuntimeError("observer failed to start")

    async def _stop():
        calls.append(entry.runtime_data.observer_running)
        await cognitive_core.stop()

    monkeypatch.setattr(observer, "start", _boom)
    monkeypatch.setattr(observer, "stop", _stop)
    entry = await _add_entry(hass, observer_enabled=True)
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    await hass.async_block_till_done()

    assert calls == [True]          # unload stopped the partial start
    runtime = seen[0]
    assert runtime.observer_running is False
    _assert_released(hass, entry, runtime)


# ── Services ────────────────────────────────────────────────────────────────

async def test_observer_services_update_runtime(hass, fake_observer):
    entry = await _setup(hass)
    _assert_state(hass, entry, False)

    await hass.services.async_call(DOMAIN, "observer_start", {}, blocking=True)
    _assert_state(hass, entry, True)
    await hass.services.async_call(DOMAIN, "observer_stop", {}, blocking=True)
    _assert_state(hass, entry, False)
    await hass.services.async_call(DOMAIN, "observer_start", {}, blocking=True)
    _assert_state(hass, entry, True)
    assert fake_observer == ["start", "stop", "start"]


async def test_observer_start_service_refuses_without_runtime(hass, fake_observer):
    """A loaded entry that lost its runtime is an internal error: the
    service fails and never starts an observer nobody owns."""
    from custom_components.nova.runtime import NovaRuntimeUnavailable
    entry = await _setup(hass)
    runtime = entry.runtime_data
    object.__delattr__(entry, "runtime_data")
    try:
        with pytest.raises(NovaRuntimeUnavailable):
            await hass.services.async_call(DOMAIN, "observer_start", {}, blocking=True)
        assert fake_observer == []
        assert hass.data[DOMAIN][entry.entry_id]["observer_running"] is False
    finally:
        entry.runtime_data = runtime


# ── WebSocket ───────────────────────────────────────────────────────────────

async def test_panel_status_reads_runtime(
    hass, hass_ws_client, fake_observer, no_panel_databases,
):
    from custom_components.nova.runtime import set_observer_running
    entry = await _setup(hass)
    assert (await _panel_observer_state(hass, hass_ws_client))["level"] != "live"

    set_observer_running(hass, entry, True)
    assert await _panel_observer_state(hass, hass_ws_client) == {
        "state": "RUNNING", "level": "live"}

    # The runtime wins over a drifted bridge copy (forced here, test only).
    hass.data[DOMAIN][entry.entry_id]["observer_running"] = False
    assert (await _panel_observer_state(hass, hass_ws_client))["state"] == "RUNNING"


async def test_panel_status_fails_loudly_without_runtime(
    hass, hass_ws_client, no_panel_databases,
):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    object.__delattr__(entry, "runtime_data")
    try:
        client = await hass_ws_client(hass)
        await client.send_json_auto_id({"type": "nova/get_panel_data"})
        resp = await client.receive_json()
        assert resp["success"] is False
        assert resp["error"]["code"] == "panel_data_failed"
    finally:
        entry.runtime_data = runtime


async def test_panel_toggle_updates_runtime(hass, hass_ws_client, fake_observer):
    entry = await _setup(hass)

    resp = await _update_config(hass, hass_ws_client, "observer_enabled", True)
    assert resp["success"], resp
    _assert_state(hass, entry, True)

    resp = await _update_config(hass, hass_ws_client, "observer_enabled", False)
    assert resp["success"], resp
    _assert_state(hass, entry, False)
    assert fake_observer == ["start", "stop"]


# ── Unload ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("damage", ["missing", "not_a_dict", "emptied", "no_bucket"])
async def test_unload_uses_runtime_when_bridge_is_damaged(hass, fake_observer, damage):
    entry = await _setup(hass, observer_enabled=True)
    runtime = entry.runtime_data
    assert runtime.sentinel._active is True
    if damage == "missing":
        del hass.data[DOMAIN][entry.entry_id]
    elif damage == "not_a_dict":
        hass.data[DOMAIN][entry.entry_id] = None
    elif damage == "emptied":
        hass.data[DOMAIN][entry.entry_id].clear()
    else:
        hass.data.pop(DOMAIN)

    with patch.object(runtime.reminder_watcher, "async_stop",
                      wraps=runtime.reminder_watcher.async_stop) as watcher_stop:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert watcher_stop.await_count == 1
    assert fake_observer == ["start", "stop"]
    assert runtime.observer_running is False
    _assert_released(hass, entry, runtime)   # sentinel, scheduler, resources


async def test_repeated_unload_does_not_repeat_teardown(hass, fake_observer):
    from custom_components.nova import async_unload_entry
    entry = await _setup(hass, observer_enabled=True)
    runtime = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with patch.object(runtime.sentinel, "async_stop") as sentinel_stop:
        await async_unload_entry(hass, entry)
        await async_unload_entry(hass, entry)
    sentinel_stop.assert_not_called()
    assert fake_observer == ["start", "stop"]
    _assert_released(hass, entry, runtime)


async def test_unload_after_partial_setup_without_runtime(hass, fake_observer):
    """Setup failed before the runtime was assigned: nothing to read but
    whatever the bridge holds (here: nothing, then a stray bridge dict)."""
    from custom_components.nova import async_unload_entry
    entry = await _add_entry(hass)
    assert not hasattr(entry, "runtime_data")

    await async_unload_entry(hass, entry)          # no bucket, no bridge
    assert fake_observer == []

    class _Stoppable:
        stopped = 0

        async def async_stop(self):
            self.stopped += 1

    sentinel = _Stoppable()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "sentinel": sentinel, "observer_running": True}
    await async_unload_entry(hass, entry)
    assert sentinel.stopped == 1
    assert fake_observer == ["stop"]
    assert not hasattr(entry, "runtime_data")
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


# ── Reload ──────────────────────────────────────────────────────────────────

async def test_reload_builds_fresh_observer_state(hass, fake_observer):
    entry = await _setup(hass)
    await hass.services.async_call(DOMAIN, "observer_start", {}, blocking=True)
    first = entry.runtime_data
    _assert_state(hass, entry, True)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    second = entry.runtime_data
    assert second is not first
    assert fake_observer == ["start", "stop"]   # the old entry's observer stopped
    assert first.observer_running is False
    _assert_state(hass, entry, False)           # observer disabled in config
