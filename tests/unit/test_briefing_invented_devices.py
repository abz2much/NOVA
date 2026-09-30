"""Briefings must not name a device or a time the gathered facts don't give
(v7.127.3).

A welcome-home briefing said a front door had been left unlocked and a garage
door had been open for hours, in a home with no garage and no front door lock.
Both lines matched example sentences in Nova's persona, which the model copied
despite the grounding rule. The persona no longer carries device examples, and
a generated briefing that names a device kind or clock time missing from the
facts is replaced whole by the plain facts.
"""
import asyncio
import sys
import types

import pytest

INVENTED = (
    "Welcome home, ma'am. The front door was left unlocked at 2:14 PM. "
    "The garage door has been open since 1:47 PM. "
    "Outside temperature is 17 degrees; partly cloudy."
)


def test_persona_has_no_device_example_lines(load):
    persona = load("const").NOVA_PERSONA.lower()
    for phrase in ("garage door is open", "front door is still unlocked",
                   "adjusting the thermostat"):
        assert phrase not in persona


def test_invented_garage_is_caught(load):
    check = load("briefing").ungrounded_briefing_fact
    context = "It is Wednesday September 30, 4:37 PM.\nWeather: partlycloudy, currently 17.1°C."
    assert check(INVENTED, context) != ""
    assert check("The garage door is open.", context) == "garage"
    assert check("The back door is open.", context) == "door"
    assert check("The front door is unlocked.", "Recent events: 13:29: Front Door opened.") == "lock"


def test_invented_time_is_caught(load):
    check = load("briefing").ungrounded_briefing_fact
    context = "It is Wednesday, 4:37 PM.\nRecent events: 13:29: Front Door opened."
    assert check("The front door opened at 1:29 PM.", context) == ""
    assert check("The front door opened at 13:29.", context) == ""
    assert check("The front door opened at 2:14 PM.", context) == "time 2:14"


def test_grounded_briefing_passes(load):
    check = load("briefing").ungrounded_briefing_fact
    context = (
        "It is Wednesday, 4:37 PM.\nWeather: partlycloudy, currently 17.1°C.\n"
        "Open/unlocked: Back Gate Lock is unlocked, Kitchen Window is open."
    )
    text = ("Welcome home, sir. The back gate lock is unlocked and the kitchen "
            "window is open. It is 17 degrees and partly cloudy.")
    assert check(text, context) == ""
    assert check("Welcome home. Nothing notable to report.", context) == ""


# ── End to end: the arrival briefing speaks the plain facts instead ─────────

@pytest.fixture
def pb(load, monkeypatch, fake_hass):
    honorific_mod = load("honorific")
    mod = load("proactive_briefing")
    mod._STATE.hass = fake_hass
    mod._STATE.config = {}
    mod._STATE.running = True
    mod._STATE.last_briefing_time = 0.0
    monkeypatch.setattr(honorific_mod, "effective_honorific", lambda hass: "ma'am")
    monkeypatch.setattr(sys.modules["jc.directive_helper"], "build_system_prompt",
                        lambda hass, honorific, task: task)
    provider = types.SimpleNamespace(chat=lambda messages, *a, **k: {"text": INVENTED})
    llm_stub = types.ModuleType("jc.llm_provider")
    llm_stub.create_provider = lambda *a, **k: provider
    llm_stub.create_tier_provider = lambda config, tier: provider
    monkeypatch.setitem(sys.modules, "jc.llm_provider", llm_stub)

    pushed = []

    async def _push(hass, config, message, reason, *, request_id=None):
        pushed.append(message)
    monkeypatch.setattr(mod, "_push_to_phone", _push)
    mod._pushed = pushed
    return mod


def test_arrival_briefing_with_invented_devices_falls_back(pb, fake_hass):
    fake_hass.states.set("weather.home", "partlycloudy", temperature=17.1,
                         temperature_unit="°C")
    asyncio.run(pb._trigger_briefing("arrival", person_name="Alex"))
    assert pb._pushed, "briefing was not delivered"
    text = pb._pushed[-1]
    assert text.startswith("Welcome home, ma'am.")
    assert "garage" not in text.lower()
    assert "unlocked" not in text.lower()
    assert "17.1" in text
