"""Reply delivery must survive a broken/missing Piper voice — v7.50.0.

The conversation layer silences the satellite whenever it routes a reply to a
Cast speaker, so if delivery is rejected (e.g. a custom Piper voice removed or
renamed by a Piper update) the reply used to vanish entirely. async_announce now
(a) reports whether it delivered, so the caller only silences the satellite on
success, and (b) retries without the voice — falling back to the engine's
default voice — so a missing custom voice can't cause total silence.

Delivery (v7.86.0) is media_player.play_media with the voice requested via a
`tts_options` query param on the media-source URL, not a `tts.speak` options
dict — see test_announce_resilience.py for the primary play_media path. This
file exercises the voice-fallback behaviour specifically: a play_media call
carrying the voice is rejected (mimicking a missing custom voice), so delivery
falls back — first to play_media without the voice, then to tts.speak — until
one succeeds.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def tts(load):
    return load("tts_helper")


@pytest.fixture(autouse=True)
def _no_real_sleep(tts, monkeypatch):
    async def _instant(_seconds):
        return None
    monkeypatch.setattr(tts.asyncio, "sleep", _instant)


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


class _VoiceFailHass(_StatesMixin):
    """Rejects any delivery call (play_media or tts.speak) that carries the
    missing custom voice; accepts it once the voice is dropped (default voice)."""
    def __init__(self):
        super().__init__()
        self.calls = []
        self.services = self
    async def async_call(self, domain, service, data, target=None, blocking=False):
        if domain == "media_player" and service == "play_media":
            has_voice = "tts_options" in data.get("media_content_id", "")
            self.calls.append(("play_media", has_voice))
            if has_voice:
                raise RuntimeError("Invalid options: voice 'en_GB-nova-high' not found")
            self._state.last_updated += 1
            return
        if domain == "tts" and service == "speak":
            has_opts = "options" in data
            self.calls.append(("tts.speak", has_opts))
            if has_opts:
                raise RuntimeError("Invalid options: voice 'en_GB-nova-high' not found")
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
    ok = await tts.async_announce(hass, "hello", "tts.piper_nova",
                                  ["media_player.kitchen"], context="reply")
    assert ok is True
    assert hass.calls and hass.calls[0][1] is True     # sent the piper voice option


async def test_noop_returns_false(tts):
    hass = _OKHass()
    assert await tts.async_announce(hass, "", "tts.piper_nova", ["m"]) is False
    assert await tts.async_announce(hass, "hi", None, ["m"]) is False
    assert await tts.async_announce(hass, "hi", "tts.piper_nova", []) is False
    assert hass.calls == []                             # never called for a no-op


async def test_missing_voice_falls_back_to_default(tts):
    hass = _VoiceFailHass()
    ok = await tts.async_announce(hass, "hello", "tts.piper_nova",
                                  ["media_player.kitchen"], context="reply")
    assert ok is True                                   # delivered via default voice
    # it tried with the voice option (failed) and again without it (succeeded)
    assert any(has for _, has in hass.calls)
    assert any(not has for _, has in hass.calls)


async def test_returns_false_when_all_fail(tts):
    assert await tts.async_announce(_AllFailHass(), "hi", "tts.piper_nova",
                                    ["media_player.x"], context="reply") is False


async def test_non_piper_has_no_voice_option(tts):
    hass = _OKHass()
    ok = await tts.async_announce(hass, "hello", "tts.home_assistant_cloud",
                                  ["media_player.kitchen"], context="briefing")
    assert ok is True
    assert hass.calls[0][1] is False                   # no piper voice option for Cloud
