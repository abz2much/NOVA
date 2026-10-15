"""Two delivery fixes in tts_helper (8.49.40).

A speaker Home Assistant does not know (renamed or removed) is skipped by
Home Assistant without an error, so it used to count as delivered: a reply
sent there was lost and still written to Spoken History. And the automatic
voice choice picked an unavailable voice (Piper with its add-on down, say)
ahead of a working one, so every announcement failed.
"""
import sys
import types

import pytest


@pytest.fixture
def tts(load, monkeypatch):
    mod = load("tts_helper")
    recorded = []
    history = types.ModuleType("spoken_history")
    history.record = lambda text, source, speakers, *a, **k: recorded.append(list(speakers))
    monkeypatch.setitem(sys.modules, mod.__name__.rsplit(".", 1)[0] + ".spoken_history",
                        history)
    monkeypatch.setattr(mod, "_is_sonos", lambda hass, eid: False)
    mod._recorded = recorded
    return mod


async def test_a_speaker_that_does_not_exist_is_not_delivered(tts, fake_hass):
    ok = await tts.async_announce(fake_hass, "hello", "tts.piper",
                                  ["media_player.renamed_away"], context="reply")
    assert ok is False
    assert fake_hass.service_calls == []
    assert tts._recorded == []


async def test_only_speakers_that_exist_are_recorded(tts, fake_hass):
    fake_hass.states.set("media_player.kitchen", "idle")
    ok = await tts.async_announce(fake_hass, "hello", "tts.piper",
                                  ["media_player.gone", "media_player.kitchen"],
                                  context="reply")
    assert ok is True
    assert tts._recorded == [["media_player.kitchen"]]


def _voices(hass, **states):
    for name, state in states.items():
        hass.states.set(f"tts.{name}", state)


def test_an_unavailable_piper_gives_way_to_a_working_voice(tts, fake_hass):
    _voices(fake_hass, piper="unavailable", edge_neural="unknown")
    assert tts.find_best_tts_entity(fake_hass) == "tts.edge_neural"
    assert tts.resolve_tts_entity(fake_hass, "auto") == "tts.edge_neural"


def test_a_voice_never_used_yet_still_counts(tts, fake_hass):
    # A TTS entity reads "unknown" until its first use; that is not down.
    _voices(fake_hass, piper="unknown", edge_neural="2026-10-15T07:00:00")
    assert tts.find_best_tts_entity(fake_hass) == "tts.piper"


def test_no_working_voice_means_none(tts, fake_hass):
    _voices(fake_hass, piper="unavailable", other="unavailable")
    assert tts.find_best_tts_entity(fake_hass) is None


def test_an_unavailable_premium_voice_gives_way_to_the_next(tts, fake_hass):
    _voices(fake_hass, elevenlabs="unavailable", openai_tts="unknown", piper="unknown")
    assert tts.find_premium_tts_entity(fake_hass) == "tts.openai_tts"
    assert tts.resolve_tts_for_context(
        fake_hass, "briefing", "auto", None, ["briefing"]) == "tts.openai_tts"


def test_no_working_premium_voice_falls_back_to_the_regular_one(tts, fake_hass):
    _voices(fake_hass, elevenlabs="unavailable", piper="unknown")
    assert tts.resolve_tts_for_context(
        fake_hass, "briefing", "auto", None, ["briefing"]) == "tts.piper"
