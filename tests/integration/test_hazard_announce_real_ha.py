"""A leak alarm is announced inside a real Home Assistant (8.85.x).

HA runs Nova's plain state_changed listener on a worker thread and refuses
hass.async_create_task from there. In 8.84.1 Nova did exactly that, so leak,
smoke, CO, gas and alarm-going-off announcements were silently dropped on HA
2026.11 (the unit tests used a lenient fake and missed it). This test drives
the whole path through a real HA: a moisture sensor turns on, and Nova's core
alert path is asked to announce it.
"""
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import _make_entry


async def test_a_leak_turning_on_is_announced(hass, monkeypatch):
    from custom_components.nova import core_delivery

    announced = []

    async def _emit(hass_, config, action, sleeping, **kwargs):
        announced.append((action["type"], action["entity_id"]))
        return ()
    monkeypatch.setattr(core_delivery, "_emit_action", _emit)

    assert await async_setup_component(hass, "homeassistant", {})
    hass.states.async_set("binary_sensor.kitchen_leak", "off",
                          {"device_class": "moisture", "friendly_name": "Kitchen leak"})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set("binary_sensor.kitchen_leak", "on",
                          {"device_class": "moisture", "friendly_name": "Kitchen leak"})
    await hass.async_block_till_done(wait_background_tasks=True)

    assert announced == [("critical_hazard", "binary_sensor.kitchen_leak")]
    assert await hass.config_entries.async_unload(entry.entry_id)
