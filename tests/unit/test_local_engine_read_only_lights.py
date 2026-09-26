"""Read-only light queries (Phase 8).

"List the lights that are currently on. Do not change anything." is
answered locally from the state machine: only lights that are on, by
friendly name (the area added when two share a name), and no service call.
Any request that says not to change anything never actuates locally.

Focused run:
    python -m pytest tests/unit/test_local_engine_read_only_lights.py -q
"""
import pytest

from fakes import FakeHass

REQUIRED = "List the lights that are currently on. Do not change anything."


@pytest.fixture
def le(load):
    return load("local_engine")


def _home():
    hass = FakeHass()
    hass.states.set("light.kitchen_ceiling", "on", friendly_name="Kitchen Ceiling")
    hass.states.set("light.porch", "on", friendly_name="Porch Light")
    hass.states.set("light.hall", "off", friendly_name="Hall Light")
    hass.states.set("sensor.kitchen_light_current", "0.4", friendly_name="Kitchen Light Current")
    hass.states.set("switch.garden_lights", "on", friendly_name="Garden Lights")
    return hass


async def test_the_required_phrasing_lists_only_lights_that_are_on(le):
    hass = _home()
    out = await le.try_local(hass, REQUIRED)
    assert out is not None and out.success
    assert "Kitchen Ceiling" in out.text and "Porch Light" in out.text
    for absent in ("Hall Light", "Kitchen Light Current", "Garden Lights", "light.", "switch."):
        assert absent not in out.text
    assert out.text.startswith("2 lights on:")
    assert hass.service_calls == []


@pytest.mark.parametrize("phrase", [
    "list the lights that are on", "show me the lights that are on",
    "tell me which lights are on", "which lights are on", "list all lights currently on",
])
async def test_other_read_only_phrasings(le, phrase):
    hass = _home()
    out = await le.try_local(hass, phrase)
    assert out is not None and "Porch Light" in out.text and "Hall Light" not in out.text
    assert hass.service_calls == []


async def test_names_that_collide_get_their_entity_or_area(le):
    hass = FakeHass()
    hass.states.set("light.lamp_a", "on", friendly_name="Lamp")
    hass.states.set("light.lamp_b", "on", friendly_name="Lamp")
    out = await le.try_local(hass, REQUIRED)
    assert "Lamp (light.lamp_a)" in out.text and "Lamp (light.lamp_b)" in out.text
    assert hass.service_calls == []


@pytest.mark.parametrize("phrase", [
    "turn off the porch light, actually do not change anything",
    "turn off all the lights without changing the hall",
    "don't touch anything, turn on the hall light",
])
async def test_a_read_only_request_never_actuates_locally(le, phrase):
    hass = _home()
    out = await le.try_local(hass, phrase)
    assert hass.service_calls == []
    assert out is None or not getattr(out, "actions", None)


async def test_ordinary_commands_still_run_locally(le):
    hass = _home()
    await le.try_local(hass, "turn off the porch light")
    assert hass.service_calls
