"""proactive_briefing.py's _trigger_briefing() is a single-shot LLM text
completion with no device-control tools at all (`provider.chat(..., tools=None,
...)`). Nothing stops the model writing in-character flavor text that CLAIMS
a device action happened ("I've nudged the thermostat up two degrees") when
nothing was ever executed — the 2026-09-18 08:54 incident.

Device-action QUESTIONS/OFFERS ("Would you like me to raise the
thermostat?") are rejected too, not just completed/promised claims — this
branch has no pending-offer system to turn a "yes" reply into a real
action, so an offer here is a dead end. Both are caught deterministically
(contains_unsupported_device_action_claim) and the whole generated briefing
is discarded for a safe, context-only fallback — never edited
sentence-by-sentence.
"""
import sys
import types

import pytest


# ── Unit tests: the deterministic validator ──────────────────────────────────

def test_straight_apostrophe_claim_is_rejected(load):
    mod = load("proactive_briefing")
    assert mod.contains_unsupported_device_action_claim(
        "I've adjusted the thermostat."
    ) is True


def test_curly_apostrophe_claim_is_rejected(load):
    mod = load("proactive_briefing")
    assert mod.contains_unsupported_device_action_claim(
        "I’ve adjusted the thermostat."
    ) is True


def test_future_claim_is_rejected(load):
    mod = load("proactive_briefing")
    assert mod.contains_unsupported_device_action_claim(
        "I'll lock the door."
    ) is True


def test_progressive_claim_is_rejected(load):
    mod = load("proactive_briefing")
    assert mod.contains_unsupported_device_action_claim(
        "I'm turning off the lights."
    ) is True


def test_nudged_thermostat_claim_is_rejected(load):
    mod = load("proactive_briefing")
    assert mod.contains_unsupported_device_action_claim(
        "Twelve degrees and cloudy outside — I've nudged the thermostat up "
        "two degrees to take the edge off."
    ) is True


def test_turned_off_lights_claim_is_rejected(load):
    mod = load("proactive_briefing")
    assert mod.contains_unsupported_device_action_claim(
        "I turned off the lights before you got home."
    ) is True


def test_will_lock_door_claim_is_rejected(load):
    mod = load("proactive_briefing")
    assert mod.contains_unsupported_device_action_claim(
        "I will lock the door in a moment."
    ) is True


@pytest.mark.parametrize("sentence", [
    "Would you like me to raise the thermostat?",
    "Shall I turn off the lights?",
    "Do you want me to lock the door?",
])
def test_device_action_questions_and_offers_are_rejected(load, sentence):
    mod = load("proactive_briefing")
    assert mod.contains_unsupported_device_action_claim(sentence) is True, sentence


def test_calendar_summary_question_remains_allowed(load):
    mod = load("proactive_briefing")
    for sentence in (
        "Shall I go over your calendar for the day?",
        "Would you like a summary of your calendar now?",
        "Do you want me to hold your calendar for the day, or read it now?",
    ):
        assert mod.contains_unsupported_device_action_claim(sentence) is False, sentence


def test_factual_thermostat_state_report_is_allowed(load):
    mod = load("proactive_briefing")
    assert mod.contains_unsupported_device_action_claim(
        "The thermostat is set to 18 degrees."
    ) is False


def test_observation_verbs_are_not_treated_as_control_actions(load):
    mod = load("proactive_briefing")
    for sentence in (
        "I've checked the thermostat and it's holding steady.",
        "I noticed the lights were off overnight.",
        "I've seen no unusual activity.",
        "I confirmed the doors are locked.",
    ):
        assert mod.contains_unsupported_device_action_claim(sentence) is False, sentence


def test_safe_multi_sentence_briefing_passes(load):
    mod = load("proactive_briefing")
    text = (
        "Welcome home, sir. Twelve degrees and cloudy outside. No security "
        "alerts overnight, all entry points remain secure. The thermostat "
        "is holding at 18 degrees. Shall I go over your calendar for the "
        "day?"
    )
    assert mod.contains_unsupported_device_action_claim(text) is False


# ── Fallback text itself must never contain a claim or offer ────────────────

def test_fallback_contains_no_device_action_claim_or_offer(load):
    mod = load("proactive_briefing")
    fallback = mod._deterministic_fallback_briefing(
        honorific="sir", greeting="Good morning", reason="arrival",
        weather="Twelve degrees and cloudy",
        open_things=["Front Door Lock"],
        events=["Front door opened at 08:54"],
    )
    assert mod.contains_unsupported_device_action_claim(fallback) is False
    # And, since this is the exact incident text, spot-check the specific
    # invented phrase never appears in the safe fallback.
    assert "nudged" not in fallback.lower()
    assert "raise the thermostat" not in fallback.lower()


# ── End-to-end: _trigger_briefing() never lets an unsafe claim through ──────

def _stub_module(monkeypatch, name, **attrs):
    mod = types.ModuleType(f"jc.{name}")
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, f"jc.{name}", mod)
    # `from . import X` inside proactive_briefing.py resolves via
    # getattr(jc_package, "X") when that attribute is already cached on the
    # `jc` package object (set by an earlier test file's real import of the
    # same submodule) — overriding sys.modules alone isn't enough once that
    # attribute exists, so it must be overridden too.
    monkeypatch.setattr(sys.modules["jc"], name, mod, raising=False)
    return mod


class _Services:
    def __init__(self):
        self.calls = []  # (domain, service, data)

    async def async_call(self, domain, service, data, target=None, blocking=False):
        self.calls.append((domain, service, dict(data)))


class _PersonState:
    def __init__(self, state):
        self.state = state


class _States:
    def async_all(self, domain=None):
        if domain == "person":
            return [_PersonState("home")]
        return []


class _FakeHass:
    def __init__(self):
        self.services = _Services()
        self.states = _States()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _FakeProvider:
    def __init__(self, text):
        self._text = text
        self.chat_calls = []

    def chat(self, messages, tools=None, max_tokens=300, temperature=0.6):
        self.chat_calls.append({"messages": messages, "tools": tools})
        return {"text": self._text}


@pytest.fixture
def briefing_mod(load, monkeypatch):
    _stub_module(
        monkeypatch, "briefing",
        _gather_weather=lambda hass: "Twelve degrees and cloudy",
        _gather_open_things=lambda hass: [],
        _gather_overnight_events=lambda hass, hrs: [],
        _gather_calendar=lambda hass: "",
        _gather_energy_anomalies=lambda hass: "",
        _time_greeting=lambda: "Good morning",
    )
    async_announce_calls = []

    async def fake_async_announce(hass, text, tts_entity, targets, context=""):
        async_announce_calls.append({"text": text, "targets": targets})

    _stub_module(
        monkeypatch, "tts_helper",
        resolve_tts_for_context=lambda *a, **k: "tts.piper",
        async_announce=fake_async_announce,
    )
    _stub_module(
        monkeypatch, "audio_routing",
        observer_speak_target=lambda *a, **k: (["media_player.entry_entry_speaker"], "local"),
    )
    _stub_module(
        monkeypatch, "sleep_detection",
        is_sleeping=lambda *a, **k: (False, ""),
    )
    mod = load("proactive_briefing")
    mod._async_announce_calls = async_announce_calls
    return mod


def _wire_provider(monkeypatch, briefing_mod, text):
    provider = _FakeProvider(text)
    _stub_module(
        monkeypatch, "llm_provider",
        create_tier_provider=lambda config, tier: provider,
        create_provider=lambda *a, **k: provider,
    )
    return provider


def test_unsafe_claim_uses_deterministic_fallback_for_speech_and_push(briefing_mod, monkeypatch):
    provider = _wire_provider(
        monkeypatch, briefing_mod,
        "Welcome home, sir. I've nudged the thermostat up two degrees.",
    )
    hass = _FakeHass()
    briefing_mod._STATE.hass = hass
    briefing_mod._STATE.config = {"notify_service": "notify.mobile_app_abi_s26"}
    briefing_mod._STATE.last_briefing_time = 0

    import asyncio
    asyncio.run(
        briefing_mod._trigger_briefing("arrival", person_name="Abi")
    )

    spoken = briefing_mod._async_announce_calls
    assert len(spoken) == 1
    assert "nudged" not in spoken[0]["text"].lower()
    assert "thermostat" not in spoken[0]["text"].lower()

    pushes = [c for c in hass.services.calls if c[0] == "notify"]
    assert len(pushes) == 1
    assert "nudged" not in pushes[0][2]["message"].lower()

    # No tool-calling capability at all: the LLM call itself never had any
    # tools to invoke a device with, and the fallback path issues no HA
    # service beyond speech/notification (no climate/light/lock call, no
    # pending-offer or approval flow of any kind).
    assert provider.chat_calls[0]["tools"] is None
    domains_called = {c[0] for c in hass.services.calls}
    assert domains_called <= {"notify"}
    assert not hasattr(briefing_mod._STATE, "pending_offer")


def test_unsafe_offer_uses_deterministic_fallback_for_speech_and_push(briefing_mod, monkeypatch):
    provider = _wire_provider(
        monkeypatch, briefing_mod,
        "Welcome home, sir. Twelve degrees and cloudy. Would you like me "
        "to raise the thermostat by two degrees?",
    )
    hass = _FakeHass()
    briefing_mod._STATE.hass = hass
    briefing_mod._STATE.config = {"notify_service": "notify.mobile_app_abi_s26"}
    briefing_mod._STATE.last_briefing_time = 0

    import asyncio
    asyncio.run(
        briefing_mod._trigger_briefing("arrival", person_name="Abi")
    )

    spoken = briefing_mod._async_announce_calls
    assert len(spoken) == 1
    assert "raise the thermostat" not in spoken[0]["text"].lower()

    pushes = [c for c in hass.services.calls if c[0] == "notify"]
    assert len(pushes) == 1
    assert "raise the thermostat" not in pushes[0][2]["message"].lower()

    domains_called = {c[0] for c in hass.services.calls}
    assert domains_called <= {"notify"}
    assert not hasattr(briefing_mod._STATE, "pending_offer")


def test_safe_generated_text_passes_through_unchanged(briefing_mod, monkeypatch):
    safe_text = (
        "Welcome home, sir. Twelve degrees and cloudy outside. No security "
        "alerts overnight. The thermostat is holding at 18 degrees."
    )
    provider = _wire_provider(monkeypatch, briefing_mod, safe_text)
    hass = _FakeHass()
    briefing_mod._STATE.hass = hass
    briefing_mod._STATE.config = {"notify_service": "notify.mobile_app_abi_s26"}
    briefing_mod._STATE.last_briefing_time = 0

    import asyncio
    asyncio.run(
        briefing_mod._trigger_briefing("arrival", person_name="Abi")
    )

    spoken = briefing_mod._async_announce_calls
    assert len(spoken) == 1
    assert spoken[0]["text"] == safe_text

    pushes = [c for c in hass.services.calls if c[0] == "notify"]
    assert len(pushes) == 1
    assert pushes[0][2]["message"] == safe_text
    assert provider.chat_calls[0]["tools"] is None
