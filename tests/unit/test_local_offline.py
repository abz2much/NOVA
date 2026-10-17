"""Exact requests get the same answer online and offline (8.49.0).

local_engine.try_local decides what Nova handles without the agent. Its
force=True call is the offline one. It used to skip the complexity check,
so a long sentence was acted on in part offline but handed to the agent
online. Now only the wording of a device that cannot be found differs.

Also here: the temperature with no room named, and the one yes/no list.
All state is fake.
"""
from __future__ import annotations

import pytest

from fakes import FakeHass

LONG = ("could you explain why the hallway light keeps going off and then turn on "
        "the hallway light because it is dark and I really cannot see anything here?")


@pytest.fixture
def le(load):
    return load("local_engine")


# ── the same decision online and offline ─────────────────────────────────────

async def test_a_request_that_needs_interpretation_is_not_acted_on_offline(le):
    hass = FakeHass()
    hass.states.set("light.hallway", "off", friendly_name="Hallway")
    assert le.score_complexity(LONG) >= 70
    online = await le.try_local(hass, LONG, "")
    offline = await le.try_local(hass, LONG, "", force=True)
    assert online is None and offline is None
    assert hass.service_calls == []


async def test_an_unknown_device_goes_to_the_agent_online_and_is_named_offline(le):
    hass = FakeHass()
    assert await le.try_local(hass, "turn on the gazebo lantern", "") is None
    offline = await le.try_local(hass, "turn on the gazebo lantern", "", force=True)
    assert offline is not None and offline.handled and not offline.success
    assert "gazebo lantern" in offline.text and "offline" in offline.text
    assert hass.service_calls == []


@pytest.mark.parametrize("force", [False, True])
async def test_repeat_that_works_either_way(le, load, force):
    sh = load("spoken_history")
    sh._last = {"id": 3, "text": "The back door is open.", "source": "alert",
                "speakers": [], "repeat_of_id": None}
    try:
        out = await le.try_local(FakeHass(), "repeat that", "", force=force)
    finally:
        sh._last = None
    assert out.handled and out.text == "The back door is open."


# ── the temperature with no room named ──────────────────────────────────────

@pytest.mark.parametrize("text", ["what's the temperature", "Nova, what's the temperature?",
                                  "what is the temperature inside", "temperature"])
async def test_the_temperature_with_no_room_reads_the_thermostats(le, text):
    hass = FakeHass()
    hass.states.set("climate.hall", "heat", friendly_name="Hall", current_temperature=20.5)
    hass.states.set("climate.study", "off", friendly_name="Study", current_temperature=18)
    out = await le.try_local(hass, text, "")
    assert out is not None and out.handled
    assert out.text == "Inside it's Hall 20.5°, Study 18°."


async def test_with_no_thermostat_it_gives_the_outdoor_temperature(le):
    hass = FakeHass()
    hass.states.set("weather.home", "cloudy", temperature=11, humidity=80)
    out = await le.try_local(hass, "what's the temperature", "")
    assert out.text == "Currently cloudy outside. Temperature is 11° with 80% humidity."


async def test_a_named_room_still_reads_that_room(le):
    hass = FakeHass()
    hass.states.set("sensor.kitchen_temperature", "21", friendly_name="Kitchen temperature",
                    device_class="temperature", unit_of_measurement="°C")
    out = await le.try_local(hass, "what's the kitchen temperature", "")
    assert out is not None and "21" in out.text and "Inside" not in out.text


# ── one yes/no list ──────────────────────────────────────────────────────────

@pytest.fixture
def answers(load):
    return load("answers")


@pytest.mark.parametrize("text,expected", [
    ("yes", "yes"), ("Yes.", "yes"), ("yeah do it", "yes"), ("Nova, go ahead", "yes"),
    ("proceed", "yes"), ("no", "no"), ("don't", "no"), ("never mind", "no"),
    ("abort", "no"), ("unlock it", None), ("open it", None),
    ("yes I went through about five thousand", None), ("", None), (None, None),
])
def test_the_shared_list(answers, text, expected):
    assert answers.yes_or_no(text) == expected


def test_every_yes_no_question_reads_the_shared_list(answers, load):
    vc = load("voice_confirm")
    assert answers.YES <= set(vc._YES) and answers.NO <= set(vc._NO)
    assert set(vc._YES) - answers.YES == {"unlock it", "open it"}


async def test_a_held_request_reads_the_shared_list(load):
    pc = load("pending_confirm")
    pc.clear_all()
    pc.put("dev1", pc.KIND_SCENE_SCRIPT, "script.bedtime")
    out = await pc.answer(FakeHass(), "dev1", "abort")
    assert out.text == pc.NO_REPLY and pc.get("dev1") is None
