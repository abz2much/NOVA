"""Reply delivery must survive a failing play_media call.

async_announce reports whether it delivered, so the conversation layer only
silences the satellite on success, and it falls back from play_media to
tts.speak when the first call is rejected. No voice is requested: the TTS
engine uses Home Assistant's default voice.
"""
from __future__ import annotations


import pytest


@pytest.fixture
def tts(load):
    return load("tts_helper")


class _State:
    def __init__(self):
        self.last_updated = 0
        self.attributes = {}


class _StatesMixin:
    """Minimal hass.states.get() so async_announce can read a (nonexistent)
    volume_level and check whether a target's state moved after play_media."""
    def __init__(self):
        self._state = _State()
    @property
    def states(self):
        s = type("States", (), {})()
        s.get = lambda eid: self._state
        return s
    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _OKHass(_StatesMixin):
    """Accepts the very first play_media call — the announcement plays first try."""
    def __init__(self):
        super().__init__()
        self.calls = []           # list of (call_type, has_voice)
        self.services = self
    async def async_call(self, domain, service, data, target=None, blocking=False):
        if domain == "media_player" and service == "play_media":
            has_voice = "tts_options" in data.get("media_content_id", "")
            self.calls.append(("play_media", has_voice))
            self._state.last_updated += 1  # the device visibly responded
            return
        raise AssertionError(f"unexpected call {domain}.{service}")


class _PlayMediaFailHass(_StatesMixin):
    """play_media is rejected; tts.speak accepts."""
    def __init__(self):
        super().__init__()
        self.calls = []
        self.services = self
    async def async_call(self, domain, service, data, target=None, blocking=False):
        if domain == "media_player" and service == "play_media":
            self.calls.append(("play_media", False))
            raise RuntimeError("play_media rejected")
        if domain == "tts" and service == "speak":
            self.calls.append(("tts.speak", "options" in data))
            return
        raise AssertionError(f"unexpected call {domain}.{service}")


class _AllFailHass(_StatesMixin):
    def __init__(self):
        super().__init__()
        self.services = self
    async def async_call(self, *a, **k):
        raise RuntimeError("tts engine down")


async def test_returns_true_on_success(tts):
    hass = _OKHass()
    ok = await tts.async_announce(hass, "hello", "tts.piper",
                                  ["media_player.kitchen"], context="reply")
    assert ok is True
    assert hass.calls and hass.calls[0][1] is False     # no voice option sent


async def test_noop_returns_false(tts):
    hass = _OKHass()
    assert await tts.async_announce(hass, "", "tts.piper", ["m"]) is False
    assert await tts.async_announce(hass, "hi", None, ["m"]) is False
    assert await tts.async_announce(hass, "hi", "tts.piper", []) is False
    assert hass.calls == []                             # never called for a no-op


async def test_play_media_failure_falls_back_to_tts_speak(tts):
    hass = _PlayMediaFailHass()
    ok = await tts.async_announce(hass, "hello", "tts.piper",
                                  ["media_player.kitchen"], context="reply")
    assert ok is True
    assert hass.calls == [("play_media", False), ("tts.speak", False)]


async def test_returns_false_when_all_fail(tts):
    assert await tts.async_announce(_AllFailHass(), "hi", "tts.piper",
                                    ["media_player.x"], context="reply") is False
