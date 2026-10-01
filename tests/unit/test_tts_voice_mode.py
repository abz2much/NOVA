"""TTS voice mode: Nova never requests a voice, so the engine uses Home
Assistant's default voice. tts_use_ha_voice only decides which TTS entity is
used (the preferred Assist pipeline's, instead of the free local pick).

Delivery is media_player.play_media, so these tests inspect that call.
"""
import urllib.parse

import pytest

DOMAIN = "nova"


@pytest.fixture
def tts(load):
    return load("tts_helper")


def _set_ha_voice(hass, on):
    """The flag lives in the Nova entry's NovaRuntime.runtime_config."""
    from conftest import _install_nova_runtime
    _install_nova_runtime(hass, {"tts_use_ha_voice": on})


def _play_media_call(hass):
    calls = [c for c in hass.service_calls if c[0] == "media_player" and c[1] == "play_media"]
    assert calls, "expected a media_player.play_media call"
    return calls[-1][2]


def _query(hass):
    content_id = _play_media_call(hass)["media_content_id"]
    return urllib.parse.parse_qs(urllib.parse.urlparse(content_id).query)


@pytest.mark.parametrize("entity", ["tts.piper", "tts.google_ai_tts", "tts.fish_audio"])
@pytest.mark.parametrize("ha_voice", [False, True])
async def test_announce_never_requests_a_voice(tts, fake_hass, entity, ha_voice):
    _set_ha_voice(fake_hass, ha_voice)
    ok = await tts.async_announce(fake_hass, "hello", entity, ["media_player.x"])
    assert ok is True
    assert "tts_options" not in _query(fake_hass)


# ─── resolve_tts_entity — routing to HA's configured Assist pipeline voice ───
#
# Issue: tts_use_ha_voice ("use Home Assistant's configured TTS voice") only
# ever suppressed Nova's own Piper voice option — it never actually changed
# *which* TTS entity got used, so a user with a cloud voice-clone engine set
# on their Assist pipeline (e.g. tts.custom_voice) still got Piper for every
# proactive announcement, because "auto" always prefers a Piper entity first.
# resolve_tts_entity() now checks the flag and, when it's on, prefers the
# preferred Assist pipeline's tts_engine before falling back to the old
# free/local auto-pick.

import sys as _sys
import types as _types


def _install_fake_pipeline(tts_engine: str | None):
    """Install a fake homeassistant.components.assist_pipeline exposing
    async_get_pipeline(hass, pipeline_id=None) -> object with .tts_engine.
    Returns a cleanup callable."""
    components = _sys.modules["homeassistant.components"]
    fake_mod = _types.ModuleType("homeassistant.components.assist_pipeline")
    fake_mod.async_get_pipeline = (
        lambda hass, pipeline_id=None: _types.SimpleNamespace(tts_engine=tts_engine)
    )
    _sys.modules["homeassistant.components.assist_pipeline"] = fake_mod
    components.assist_pipeline = fake_mod

    def _cleanup():
        del _sys.modules["homeassistant.components.assist_pipeline"]
        del components.assist_pipeline

    return _cleanup


def test_resolve_auto_prefers_piper_when_ha_voice_off(tts, fake_hass):
    fake_hass.states.set("tts.piper", "idle")
    fake_hass.states.set("tts.custom_voice", "idle")
    assert tts.resolve_tts_entity(fake_hass, "auto") == "tts.piper"


def test_resolve_auto_uses_pipeline_voice_when_ha_voice_on(tts, fake_hass):
    _set_ha_voice(fake_hass, True)
    fake_hass.states.set("tts.piper", "idle")
    fake_hass.states.set("tts.custom_voice", "idle")
    cleanup = _install_fake_pipeline("tts.custom_voice")
    try:
        assert tts.resolve_tts_entity(fake_hass, "auto") == "tts.custom_voice"
    finally:
        cleanup()


def test_resolve_auto_ha_voice_on_falls_back_if_pipeline_entity_missing(tts, fake_hass):
    _set_ha_voice(fake_hass, True)
    fake_hass.states.set("tts.piper", "idle")
    # Pipeline points at an entity that no longer exists (removed integration).
    cleanup = _install_fake_pipeline("tts.gone")
    try:
        assert tts.resolve_tts_entity(fake_hass, "auto") == "tts.piper"
    finally:
        cleanup()


def test_resolve_auto_ha_voice_on_falls_back_when_pipeline_unavailable(tts, fake_hass):
    _set_ha_voice(fake_hass, True)
    fake_hass.states.set("tts.piper", "idle")
    # No assist_pipeline module installed at all — must not raise.
    assert tts.resolve_tts_entity(fake_hass, "auto") == "tts.piper"


def test_explicit_configured_entity_wins_regardless_of_ha_voice_flag(tts, fake_hass):
    _set_ha_voice(fake_hass, True)
    fake_hass.states.set("tts.google_ai_tts", "idle")
    fake_hass.states.set("tts.custom_voice", "idle")
    assert tts.resolve_tts_entity(fake_hass, "tts.google_ai_tts") == "tts.google_ai_tts"
