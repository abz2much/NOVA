"""'Home Assistant' is the platform, not a presence question (Phase 8).

A bare, unanchored "home" pattern once sent "Can you reach Home Assistant?"
to the who's-home shortcut, so Nova answered with household occupancy.
Presence now needs presence grammar, "home" followed by "assistant" is never
presence, and questions about reaching Home Assistant get a bounded status
answer from what the running instance can observe. The local engine is
shared by every channel (voice, Telegram, panel chat), so the result must
not depend on who asked or from which device.

Focused run:
    python -m pytest tests/unit/test_local_engine_platform_vs_presence.py -q
"""
import re

import pytest

from fakes import FakeHass

PLATFORM = [
    "Can you reach Home Assistant?",
    "Are you connected to Home Assistant?",
    "Can you access Home Assistant?",
    "Is Home Assistant available?",
    "Check the Home Assistant connection.",
    "is homeassistant online",
    "Is Home Assistant down?",
]
PRESENCE = {
    "Who is home?": "who_home",
    "Who's home?": "who_home",
    "Is anyone home?": "who_home",
    "Is Abi home?": "person_home",
    "How many people are home?": "count_home",
    "who is at home right now": "who_home",
    "is anybody still home": "who_home",
}
NOT_PRESENCE = ["welcome home", "I'm home", "going home now", "restart home assistant",
                "open the home assistant app", "is the home assistant update ready"]
CHANNELS = [None, "telegram-chat-1", "voice-satellite-kitchen", "panel-browser"]
HONORIFICS = ["sir", "", "Abi"]


@pytest.fixture
def le(load):
    return load("local_engine")


def _home():
    hass = FakeHass()
    hass.states.set("person.abi", "home", friendly_name="Abi")
    hass.states.set("person.sam", "not_home", friendly_name="Sam")
    hass.states.set("light.porch", "on", friendly_name="Porch Light")
    return hass


def _routes(le, text):
    n = le._normalize(text)
    return [q for p, q in le._QUERY_PATTERNS if re.search(p, n)]


_OCCUPANCY = ("is currently home", "are currently home", "No one appears to be home",
              "people are home", "person is home")


@pytest.mark.parametrize("text", PLATFORM)
@pytest.mark.parametrize("device_id", CHANNELS)
@pytest.mark.parametrize("honorific", HONORIFICS)
async def test_platform_questions_never_take_the_presence_route(le, text, device_id,
                                                                honorific):
    assert _routes(le, text)[:1] == ["platform_status"]
    assert not {"who_home", "count_home", "person_home"} & set(_routes(le, text))
    hass = _home()
    out = await le.try_local(hass, text, honorific, device_id=device_id)
    assert out is not None and out.success
    assert out.text.startswith("Home Assistant") and "running and responding" in out.text
    assert "can see 3 entities" in out.text
    assert not any(o in out.text for o in _OCCUPANCY) and "Abi" not in out.text.split(",")[0]
    assert hass.service_calls == []


async def test_the_status_answer_is_the_same_on_every_channel(le):
    answers = set()
    for device_id in CHANNELS:
        hass = _home()
        out = await le.try_local(hass, "Can you reach Home Assistant?", "sir",
                                 device_id=device_id)
        answers.add(out.text)
    assert len(answers) == 1


async def test_the_status_answer_reports_a_platform_that_is_not_running(le):
    hass = _home()
    hass.state = "starting"
    out = await le.try_local(hass, "Is Home Assistant available?", "sir")
    assert "starting right now" in out.text and "responding" not in out.text


@pytest.mark.parametrize("text,route", sorted(PRESENCE.items()))
@pytest.mark.parametrize("device_id", CHANNELS)
async def test_genuine_presence_questions_still_use_the_presence_route(le, text, route,
                                                                       device_id):
    assert _routes(le, text)[0] == route
    hass = _home()
    out = await le.try_local(hass, text, "sir", device_id=device_id)
    assert out is not None and "Abi" in out.text and "Home Assistant" not in out.text
    assert "Sam" not in out.text
    assert hass.service_calls == []


async def test_person_presence_answers_for_that_person(le):
    hass = _home()
    assert (await le.try_local(hass, "Is Sam home?", "")).text == "Sam is away."
    assert (await le.try_local(hass, "How many people are home?", "")).text == \
        "One person is home: Abi."
    # Not a known person: no presence answer is invented.
    assert "person_home" in _routes(le, "is the dog home")
    out = await le.try_local(hass, "is the dog home", "")
    assert out is None or "home" not in getattr(out, "text", "").lower()
    assert hass.service_calls == []


@pytest.mark.parametrize("text", NOT_PRESENCE)
def test_home_in_other_sentences_is_not_presence(le, text):
    assert not {"who_home", "count_home", "person_home"} & set(_routes(le, text))


def test_no_presence_pattern_can_match_home_assistant(le):
    """Grammar-level guarantee, not a list of phrasings: every presence
    pattern refuses "home" followed by "assistant"."""
    for pattern, qtype in le._QUERY_PATTERNS:
        if qtype in ("who_home", "count_home", "person_home"):
            for text in ("who is home assistant", "is anyone home assistant",
                         "how many people are home assistant", "is abi home assistant"):
                assert not re.search(pattern, text), (qtype, text)
