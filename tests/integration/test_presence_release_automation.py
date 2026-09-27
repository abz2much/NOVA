"""The generated presence-release automation, run by Home Assistant itself.

Each test loads the exact JSON Nova generates for a gated sequence with
"off when presence clears" into the real automation component and drives
the sensors and the clock."""
import asyncio
import json
from datetime import timedelta

import pytest
from homeassistant.core import ServiceCall, callback
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.nova.automation.models import DetectedPattern
from custom_components.nova.automation.suggestions import generate_automation

DOOR = "binary_sensor.front_door"
PRESENCE = "binary_sensor.hall_presence"
LIGHT = "light.hall"
SETTLE = 60


def _release_automation() -> dict:
    pattern = DetectedPattern(
        "sequence", "door then hall light", [DOOR, LIGHT], 0.9, 8,
        details={
            "trigger": {"entity": DOOR, "state": "on"},
            "action": {"entity": LIGHT, "state": "on"},
            "delay_seconds": 0,
            "condition": [{"condition": "state", "entity_id": PRESENCE,
                           "state": "on"}],
            "presence_gate": {"entity_id": PRESENCE, "area_id": "hall",
                              "area_name": "Hall"},
            "presence_release": {"entity_id": PRESENCE, "area_id": "hall",
                                 "area_name": "Hall",
                                 "settle_seconds": SETTLE},
        })
    auto = json.loads(generate_automation(pattern))
    assert auto["mode"] == "restart"
    return auto


async def _settle() -> None:
    """Let callbacks and the automation run advance. (async_block_till_done
    would wait for the automation run itself, which is parked in a wait.)"""
    for _ in range(20):
        await asyncio.sleep(0)


class _Light:
    """Records light service calls; optionally runs a side effect on turn_on."""

    def __init__(self, hass):
        self.calls: list[str] = []
        self.on_turn_on = None

        @callback
        def _handle(call: ServiceCall) -> None:
            self.calls.append(call.service)
            hass.states.async_set(LIGHT, "on" if call.service == "turn_on" else "off")
            if call.service == "turn_on" and self.on_turn_on:
                self.on_turn_on()

        hass.services.async_register("light", "turn_on", _handle)
        hass.services.async_register("light", "turn_off", _handle)


@pytest.fixture
async def home(hass, freezer):
    freezer.move_to("2026-09-27 12:00:00+00:00")
    hass.states.async_set(DOOR, "off")
    hass.states.async_set(PRESENCE, "on")
    hass.states.async_set(LIGHT, "off")
    light = _Light(hass)
    assert await async_setup_component(
        hass, "automation", {"automation": [_release_automation()]})
    await hass.async_block_till_done()

    async def advance(seconds: float) -> None:
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass, dt_util.utcnow())
        await _settle()

    async def set_state(entity_id: str, state: str) -> None:
        hass.states.async_set(entity_id, state)
        await _settle()

    async def open_door() -> None:
        await set_state(DOOR, "on")
        await set_state(DOOR, "off")

    return light, advance, set_state, open_door


async def test_turns_off_after_presence_clears_for_the_settling_time(home):
    light, advance, set_state, open_door = home
    await open_door()
    assert light.calls == ["turn_on"]
    await advance(600)
    await set_state(PRESENCE, "off")
    await advance(SETTLE - 10)
    assert light.calls == ["turn_on"]
    await advance(20)
    assert light.calls == ["turn_on", "turn_off"]


async def test_presence_returning_before_the_settling_time_keeps_the_light_on(home):
    light, advance, set_state, open_door = home
    await open_door()
    await set_state(PRESENCE, "off")
    await advance(SETTLE - 20)
    await set_state(PRESENCE, "on")
    await advance(SETTLE * 3)
    assert light.calls == ["turn_on"]
    await set_state(PRESENCE, "off")
    await advance(SETTLE + 5)
    assert light.calls == ["turn_on", "turn_off"]


async def test_sensor_dropping_out_before_clearing_still_turns_off(home):
    """on -> unavailable -> off never matched the 7.124.0 wait (from: on)."""
    light, advance, set_state, open_door = home
    await open_door()
    await set_state(PRESENCE, "unavailable")
    await advance(300)
    await set_state(PRESENCE, "off")
    await advance(SETTLE + 5)
    assert light.calls == ["turn_on", "turn_off"]


async def test_stuck_presence_never_turns_the_light_off(home):
    light, advance, set_state, open_door = home
    await open_door()
    for _ in range(5):
        await advance(3600)
    assert light.calls == ["turn_on"]
    # The run has ended at the four hour timeout; a later clear does nothing
    # until the next trigger re-arms it.
    await set_state(PRESENCE, "off")
    await advance(SETTLE + 5)
    assert light.calls == ["turn_on"]


async def test_timeout_turns_off_only_when_presence_is_off_then(home):
    light, advance, set_state, open_door = home
    await open_door()
    await set_state(PRESENCE, "unavailable")
    await advance(4 * 3600 - 30)
    await set_state(PRESENCE, "off")
    await advance(40)          # timeout reached before off lasted SETTLE
    assert light.calls == ["turn_on", "turn_off"]


async def test_presence_clearing_while_turning_on_waits_then_turns_off(home, hass):
    light, advance, set_state, open_door = home
    light.on_turn_on = lambda: hass.states.async_set(PRESENCE, "off")
    await open_door()
    await advance(SETTLE - 10)
    assert light.calls == ["turn_on"]
    await advance(20)
    assert light.calls == ["turn_on", "turn_off"]


async def test_presence_clearing_while_turning_on_then_returning_keeps_it_on(
        home, hass):
    """7.124.0's 'already clear' branch ended in a condition inside choose; a
    failed condition only ends that branch, so the light was turned off while
    presence was back on."""
    light, advance, set_state, open_door = home
    light.on_turn_on = lambda: hass.states.async_set(PRESENCE, "off")
    await open_door()
    light.on_turn_on = None
    await advance(SETTLE - 20)
    await set_state(PRESENCE, "on")
    await advance(SETTLE * 3)
    assert light.calls == ["turn_on"]
    await set_state(PRESENCE, "off")
    await advance(SETTLE + 5)
    assert light.calls == ["turn_on", "turn_off"]
