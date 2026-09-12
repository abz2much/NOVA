"""TTS voice mode — use the Nova Piper voice, or Home Assistant's default.

Default keeps the Nova voice; with tts_use_ha_voice on (read from
runtime_config), Nova omits the `voice` option so the TTS entity uses its
configured default (e.g. a French Piper voice), resolving issue #16.
"""
import pytest

DOMAIN = "nova"


@pytest.fixture
def tts(load):
    return load("tts_helper")


def _set_ha_voice(hass, on):
    hass.data.setdefault(DOMAIN, {})["e1"] = {"runtime_config": {"tts_use_ha_voice": on}}


def _speak_call(hass):
    calls = [c for c in hass.service_calls if c[0] == "tts" and c[1] == "speak"]
    assert calls, "expected a tts.speak call"
    return calls[-1][2]


async def test_default_requests_nova_voice_on_piper(tts, fake_hass):
    ok = await tts.async_announce(fake_hass, "hello", "tts.piper", ["media_player.x"])
    assert ok is True
    assert _speak_call(fake_hass).get("options", {}).get("voice") == "en_GB-nova-high"


async def test_ha_voice_mode_omits_voice(tts, fake_hass):
    _set_ha_voice(fake_hass, True)
    ok = await tts.async_announce(fake_hass, "bonjour", "tts.piper", ["media_player.x"])
    assert ok is True
    data = _speak_call(fake_hass)
    assert "options" not in data or "voice" not in data.get("options", {})


async def test_ha_voice_off_keeps_nova_voice(tts, fake_hass):
    _set_ha_voice(fake_hass, False)
    await tts.async_announce(fake_hass, "hi", "tts.piper", ["media_player.x"])
    assert _speak_call(fake_hass).get("options", {}).get("voice") == "en_GB-nova-high"


async def test_non_piper_never_forces_voice(tts, fake_hass):
    await tts.async_announce(fake_hass, "hi", "tts.google_ai_tts", ["media_player.x"])
    assert "options" not in _speak_call(fake_hass)


# ─── resolve_tts_entity — routing to HA's configured Assist pipeline voice ───
#
# Issue: tts_use_ha_voice ("use Home Assistant's configured TTS voice") only
# ever suppressed Nova's own Piper voice option — it never actually changed
# *which* TTS entity got used, so a user with a cloud voice-clone engine set
# on their Assist pipeline (e.g. tts.jarvis_jarvis) still got Piper for every
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
    fake_hass.states.set("tts.jarvis_jarvis", "idle")
    assert tts.resolve_tts_entity(fake_hass, "auto") == "tts.piper"


def test_resolve_auto_uses_pipeline_voice_when_ha_voice_on(tts, fake_hass):
    _set_ha_voice(fake_hass, True)
    fake_hass.states.set("tts.piper", "idle")
    fake_hass.states.set("tts.jarvis_jarvis", "idle")
    cleanup = _install_fake_pipeline("tts.jarvis_jarvis")
    try:
        assert tts.resolve_tts_entity(fake_hass, "auto") == "tts.jarvis_jarvis"
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
    fake_hass.states.set("tts.jarvis_jarvis", "idle")
    assert tts.resolve_tts_entity(fake_hass, "tts.google_ai_tts") == "tts.google_ai_tts"
