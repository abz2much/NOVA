"""The relevance gate and the offer answer (8.39.1).

conversation.py needs Home Assistant's conversation component to import, so
these pull the two pure functions and their word lists straight out of the
source and run them.

The gate used to pass any sentence with no command signal, so a TV in the
room got answers. The lines in TV_LINES were all transcribed and answered
in a real home before this change; REQUESTS are phrasings that must still
reach Nova.
"""
import pathlib
import re

import pytest

SRC = (pathlib.Path(__file__).resolve().parents[2]
       / "custom_components" / "nova" / "conversation.py").read_text(encoding="utf-8")


def _section(start: str, end: str) -> str:
    return SRC[SRC.index(start):SRC.index(end)]


_NS: dict = {"re": re}
exec(_section("# Verbs that make an imperative", "def _current_time_context("), _NS)
exec(_section("_OFFER_YES = frozenset((", "def _turn_user_id("), _NS)
exec(_section("def _is_addressed_to_nova(", "def _matches_voice_phrase("), _NS)
addressed = _NS["_is_addressed_to_nova"]
offer_answer = _NS["_offer_answer"]

TV_LINES = [
    "Isn't what I'm hoping I'm sad is like my own.",
    "Give the line.",
    "pass over you, and the flesh shall not be upon to destroy yo…",
    "Conversation on that point was second year.",
    "Here in your heart. Good.",
    "Yeah, I went through about five thousand.",
    "Why did you do that to me after everything?",
    "Are you sure that's what he said to her?",
    "Make a wish.",
    "So, um, I mean",
]

REQUESTS = [
    "turn off the lights", "please turn on the lamp", "what's the weather",
    "who's home", "is anyone home", "what time is it", "how are you",
    "what can you do", "is it going to rain tomorrow",
    "how much energy did we use today", "what's on my calendar today",
    "what's the temperature upstairs", "good night", "good morning Nova",
    "I'm cold", "i'm going to bed", "I'm home", "thank you", "thanks",
    "hello", "tell me a joke", "play some music", "play radio", "stop",
    "cancel the timer", "set a timer for ten minutes", "start a timer",
    "remind me to call the dentist", "open the garage", "show me the cameras",
    "give me the weather", "make it warmer", "lock up", "read my messages",
    "check the doors", "close the blinds", "run the movie scene",
    "yes", "no", "yes please", "the kitchen one", "twenty minutes",
    "Nova, I went through about five thousand", "can you tell me a joke",
    "could you turn the heating up", "would you remind me at six",
    "read the news", "play the radio", "add milk to the shopping list",
    "check the post", "it is nippy in here", "it's a bit cold in here",
]


@pytest.mark.parametrize("text", TV_LINES)
def test_background_speech_is_ignored(text):
    assert addressed(text) is False


@pytest.mark.parametrize("text", REQUESTS)
def test_real_requests_still_reach_nova(text):
    assert addressed(text) is True


@pytest.mark.parametrize("text", ["the wifi password is hunter2",
                                  "Yeah, I went through about five thousand.",
                                  "Why did you do that to me after everything?"])
def test_a_typed_turn_with_no_satellite_keeps_the_old_default(text):
    # Only a satellite can hear a TV, so only voice turns are strict.
    assert addressed(text, voice=False) is True


def test_rambling_and_filler_are_still_ignored_without_a_satellite():
    assert addressed("um, I mean", voice=False) is False
    assert addressed("He left. She stayed. Then they all went home together.",
                     voice=False) is False


def test_empty_input_is_ignored():
    assert addressed("") is False and addressed("  ...  ") is False


@pytest.mark.parametrize("text,expected", [
    ("yes", "yes"), ("Yes.", "yes"), ("yes please", "yes"), ("Nova, yes", "yes"),
    ("no", "no"), ("No thanks!", "no"), ("never mind", "no"),
    ("yes I went through about five thousand", None), ("give the line", None),
    ("", None), (None, None),
])
def test_offer_answer_is_only_a_plain_yes_or_no(text, expected):
    assert offer_answer(text) == expected


def test_only_a_plain_answer_to_a_bound_offer_skips_the_gate():
    assert "relevant = bool(offer_answer) or not gate_enabled or is_addressed" in SRC
    assert "offer_answer = _offer_answer(user_input.text) if pending_offer else None" in SRC
    assert ("pending_offer = (cognitive_core.pending_offer_for(self.hass, device_id)\n"
            "                             if device_id and not is_chat else None)") in SRC


def test_no_follow_up_mic_during_quiet_hours_except_for_a_held_request():
    block = _section("held = bool(device_id and not is_chat and _pc.get(device_id))",
                     "sat_ent = (_cc.satellite_for_device")
    assert "and not self._in_quiet_hours())" in block
    assert "if not is_chat and (held or follow_up):" in block
