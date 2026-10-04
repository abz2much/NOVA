"""Porch motion and mailbox sensors bringing a package check forward (8.7.6),
against a real Home Assistant (PHACC). The listener is a closure inside
async_setup_entry, so only a real setup can prove it: which sensors it
watches, that only a real off -> on change fires it, that the live
package_detection flag gates it, and that unload removes it and cancels a
check that is still waiting.

package_monitor's own behaviour (20 second wait, debounce, cooldown) is
covered in tests/unit/test_package_delivery_mail.py; here the two entry points
are replaced by recorders. Nothing is spoken and no device is touched.
"""
import asyncio

import pytest

from .test_runtime_data import (  # noqa: F401  (autouse fixtures)
    _add_entry,
    _no_real_config,
    _restore_nova_config,
)

PORCH = "binary_sensor.porch_motion"
BOX = "binary_sensor.mailbox"


@pytest.fixture
def triggers(monkeypatch):
    from custom_components.nova import package_monitor as pm
    calls: list[tuple[str, str]] = []

    async def _motion(hass, client, ctx, sensor_id):
        calls.append(("motion", sensor_id))

    async def _mailbox(hass, client, ctx, sensor_id):
        calls.append(("mailbox", sensor_id))

    monkeypatch.setattr(pm, "note_from_motion", _motion)
    monkeypatch.setattr(pm, "note_from_mailbox", _mailbox)
    return calls


async def _setup(hass):
    # Discovery runs once at setup, so the sensors exist first.
    hass.states.async_set(PORCH, "off", {"device_class": "motion"})
    hass.states.async_set("binary_sensor.front_yard_motion", "off", {"device_class": "motion"})
    hass.states.async_set("binary_sensor.front_door_contact", "off", {"device_class": "door"})
    hass.states.async_set(BOX, "off", {"device_class": "opening"})
    entry = await _add_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _flip(hass, entity_id, state):
    hass.states.async_set(entity_id, state, hass.states.get(entity_id).attributes)
    await hass.async_block_till_done()
    await asyncio.sleep(0)                   # let a background trigger task start


async def test_porch_motion_and_mailbox_fire_the_right_entry_point(hass, triggers):
    await _setup(hass)
    await _flip(hass, PORCH, "on")
    await _flip(hass, BOX, "on")
    assert triggers == [("motion", PORCH), ("mailbox", BOX)]


async def test_wide_views_and_wrong_device_classes_are_not_watched(hass, triggers):
    await _setup(hass)
    await _flip(hass, "binary_sensor.front_yard_motion", "on")
    await _flip(hass, "binary_sensor.front_door_contact", "on")
    assert triggers == []


async def test_only_a_real_off_to_on_change_fires(hass, triggers):
    await _setup(hass)
    await _flip(hass, PORCH, "unavailable")
    await _flip(hass, PORCH, "on")           # unavailable -> on: a reconnect
    assert triggers == []
    await _flip(hass, PORCH, "off")
    await _flip(hass, PORCH, "on")           # off -> on: someone is there
    assert triggers == [("motion", PORCH)]
    await _flip(hass, PORCH, "on")           # on -> on (attribute refresh)
    assert triggers == [("motion", PORCH)]


async def test_the_package_detection_flag_is_live(hass, triggers):
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config
    rc["package_detection"] = False
    await _flip(hass, PORCH, "on")
    await _flip(hass, BOX, "on")
    assert triggers == []
    rc["package_detection"] = True
    await _flip(hass, PORCH, "off")
    await _flip(hass, PORCH, "on")
    assert triggers == [("motion", PORCH)]


async def test_unload_removes_the_listener_and_cancels_a_waiting_check(hass, monkeypatch):
    from custom_components.nova import package_monitor as pm
    state = {"started": 0, "cancelled": 0}

    async def _waiting(hass_, client, ctx, sensor_id):
        state["started"] += 1
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] += 1
            raise

    monkeypatch.setattr(pm, "note_from_motion", _waiting)
    entry = await _setup(hass)
    await _flip(hass, PORCH, "on")
    assert state == {"started": 1, "cancelled": 0}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert state["cancelled"] == 1

    await _flip(hass, PORCH, "off")
    await _flip(hass, PORCH, "on")
    assert state["started"] == 1             # no listener left
