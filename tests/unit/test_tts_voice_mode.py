"""TTS voice mode — use the Nova Piper voice, or Home Assistant's default.

Default keeps the Nova voice; with tts_use_ha_voice on (read from
runtime_config), Nova omits the `voice` option so the TTS entity uses its
configured default (e.g. a French Piper voice), resolving issue #16.

Delivery is media_player.play_media (v7.86.0): the requested voice travels
as a `tts_options` query param on the media_content_id URL, not as a
tts.speak `options` dict — so these tests inspect the play_media call.
"""
import json
import sys
import types
import urllib.parse

import pytest

DOMAIN = "nova"

# tts_helper lazily imports bootstrap (for resolve_installed_quality), which
# imports aiohttp at module level — stub it the same way test_bootstrap.py
# does, so that import succeeds regardless of which test file collects first.
if "aiohttp" not in sys.modules:
    _aiohttp = types.ModuleType("aiohttp")
    _aiohttp.ClientTimeout = lambda **kw: None
    _aiohttp.ClientSession = object
    sys.modules["aiohttp"] = _aiohttp


@pytest.fixture
def tts(load):
    return load("tts_helper")


@pytest.fixture
def bootstrap(load):
    return load("bootstrap")


@pytest.fixture(autouse=True)
def _isolate_piper_dir(tmp_path, monkeypatch, bootstrap):
    from pathlib import Path
    monkeypatch.setattr(bootstrap, "PIPER_DIR", Path(tmp_path / "piper"))


def _install_voice(bootstrap, quality):
    bootstrap.PIPER_DIR.mkdir(parents=True, exist_ok=True)
    (bootstrap.PIPER_DIR / f"en_GB-nova-{quality}.onnx").write_bytes(
        b"x" * (bootstrap.MIN_ONNX_SIZE + 10))
    (bootstrap.PIPER_DIR / f"en_GB-nova-{quality}.onnx.json").write_text("{}")


def _set_ha_voice(hass, on):
    hass.data.setdefault(DOMAIN, {})["e1"] = {"runtime_config": {"tts_use_ha_voice": on}}


def _play_media_call(hass):
    calls = [c for c in hass.service_calls if c[0] == "media_player" and c[1] == "play_media"]
    assert calls, "expected a media_player.play_media call"
    return calls[-1][2]


def _requested_voice(hass):
    """The `voice` from the play_media call's tts_options query param, or
    None if no tts_options were sent at all."""
    content_id = _play_media_call(hass)["media_content_id"]
    query = urllib.parse.urlparse(content_id).query
    params = urllib.parse.parse_qs(query)
    raw = params.get("tts_options")
    if not raw:
        return None
    return json.loads(raw[0]).get("voice")


async def test_default_requests_nova_voice_on_piper(tts, bootstrap, fake_hass):
    _install_voice(bootstrap, "high")
    ok = await tts.async_announce(fake_hass, "hello", "tts.piper", ["media_player.x"])
    assert ok is True
    assert _requested_voice(fake_hass) == "en_GB-nova-high"


async def test_ha_voice_mode_omits_voice(tts, bootstrap, fake_hass):
    _install_voice(bootstrap, "high")
    _set_ha_voice(fake_hass, True)
    ok = await tts.async_announce(fake_hass, "bonjour", "tts.piper", ["media_player.x"])
    assert ok is True
    assert _requested_voice(fake_hass) is None


async def test_ha_voice_off_keeps_nova_voice(tts, bootstrap, fake_hass):
    _install_voice(bootstrap, "high")
    _set_ha_voice(fake_hass, False)
    await tts.async_announce(fake_hass, "hi", "tts.piper", ["media_player.x"])
    assert _requested_voice(fake_hass) == "en_GB-nova-high"


async def test_non_piper_never_forces_voice(tts, fake_hass):
    await tts.async_announce(fake_hass, "hi", "tts.google_ai_tts", ["media_player.x"])
    assert _requested_voice(fake_hass) is None


async def test_non_piper_fish_audio_never_resolves_or_injects_nova_voice(tts, bootstrap, fake_hass, monkeypatch):
    """A non-Piper engine (e.g. a Fish Audio TTS entity) must never trigger
    Nova's voice-quality resolution at all — not merely end up with no voice
    by coincidence. Spies on resolve_installed_quality rather than relying on
    the final tts_options value, so this fails if is_piper detection regresses
    to calling the resolver unconditionally."""
    _install_voice(bootstrap, "high")  # even with a Nova voice installed...
    calls = []

    def _spy(preferred="high"):
        calls.append(preferred)
        return preferred
    monkeypatch.setattr(bootstrap, "resolve_installed_quality", _spy)

    ok = await tts.async_announce(fake_hass, "hi", "tts.fish_audio", ["media_player.x"])
    assert ok is True
    assert calls == []  # ...the resolver is never even called for a non-Piper entity
    assert _requested_voice(fake_hass) is None


# ─── on-demand announcement quality resolution (v7.102.x) ────────────────────
#
# tts_options used to hardcode "en_GB-nova-high" unconditionally. If bootstrap
# had fallen back to medium (high not hosted, or not yet downloaded), every
# single announcement requested a voice file that doesn't exist. It's now
# resolved from what's actually on disk, the same way bootstrap's own
# pipeline setup is.

async def test_announce_omits_voice_when_none_installed(tts, bootstrap, fake_hass):
    ok = await tts.async_announce(fake_hass, "hi", "tts.piper", ["media_player.x"])
    assert ok is True
    assert _requested_voice(fake_hass) is None


async def test_announce_requests_installed_fallback_quality(tts, bootstrap, fake_hass):
    _install_voice(bootstrap, "medium")  # high not installed
    ok = await tts.async_announce(fake_hass, "hi", "tts.piper", ["media_player.x"])
    assert ok is True
    assert _requested_voice(fake_hass) == "en_GB-nova-medium"


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
