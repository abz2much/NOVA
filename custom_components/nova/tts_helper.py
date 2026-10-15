"""Speaking through Home Assistant's TTS: choosing the voice and announcing.

Two voices can be in play. The regular one (``tts_engine``, "auto" by
default) speaks everyday replies; an optional premium one
(``tts_premium_engine``, e.g. ElevenLabs) speaks the contexts listed in
``tts_premium_contexts``, such as briefings or the doorbell. With no
premium voice set, or none found, everything uses the regular voice.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Optional, Sequence

from homeassistant.core import HomeAssistant

from .speech_text import speech_text

_LOGGER = logging.getLogger(__name__)

# How long a speaker gets to accept an announcement. Past this it counts as
# delivered: it is still fetching or playing the audio.
_PLAY_MEDIA_TIMEOUT = 15

# The context each caller passes in, for logging and premium routing.
KNOWN_CONTEXTS = {
    "chat", "briefing", "camera", "doorbell", "sentinel", "reminder",
    "routine", "recognition", "summary", "appliance", "reply",
}

# A voice in this state cannot speak, so the automatic choice passes it by.
# "unknown" is fine: a TTS entity reads unknown until it is first used.
_DOWN = "unavailable"

# Spoken History's label for each context. Anything not listed is recorded
# under its own name; None means not recorded at all.
_HISTORY_SOURCE_MAP = {
    "chat": None,
    "routine": "routine",
    "sentinel": "alert",
    "hazard": "alert",
    "package": "alert",
    "appliance": "alert",
    "test": "manual",
    "doorbell": "camera",
    "recognition": "camera",
    "summary": "briefing",
}


def _is_sonos(hass: HomeAssistant, entity_id: str) -> bool:
    """Only Sonos understands a volume in play_media's ``extra``; Cast passes
    it on to pychromecast, which fails the whole call."""
    try:
        from homeassistant.helpers import entity_registry as er
        entry = er.async_get(hass).async_get(entity_id)
    except Exception:
        return False
    return entry is not None and entry.platform == "sonos"


# ── choosing a voice ────────────────────────────────────────────────────────

def _usable_voices(hass: HomeAssistant) -> list:
    return [st for st in hass.states.async_all("tts") if st.state != _DOWN]


def _first_named(voices: list, *needles: str) -> str | None:
    for needle in needles:
        for st in voices:
            if needle in st.entity_id.lower():
                return st.entity_id
    return None


def find_best_tts_entity(hass: HomeAssistant) -> str | None:
    """The free or local voice to use: Piper, then Edge, then any TTS
    entity. A voice that is unavailable is passed over."""
    voices = _usable_voices(hass)
    found = _first_named(voices, "piper") or _first_named(voices, "edge")
    if found:
        return found
    return voices[0].entity_id if voices else None


def find_premium_tts_entity(hass: HomeAssistant) -> str | None:
    """A premium voice if one is installed and working: ElevenLabs, then
    OpenAI, then Azure. Home Assistant Cloud's voice is not premium, and
    nothing else is guessed at."""
    return _first_named(_usable_voices(hass), "elevenlabs", "eleven_labs",
                        "openai", "azure")


find_piper_entity = find_best_tts_entity   # older name, still imported


def tts_use_ha_voice(hass: HomeAssistant) -> bool:
    """The panel's "use Home Assistant's configured voice" switch
    (``tts_use_ha_voice``), from the live runtime config of Nova's entry."""
    from .runtime import domain_runtime_config
    return bool(domain_runtime_config(hass).get("tts_use_ha_voice"))


def _ha_pipeline_tts_entity(hass: HomeAssistant) -> str | None:
    """The voice on Home Assistant's preferred Assist pipeline, if that
    pipeline exists and its TTS entity still does."""
    try:
        from homeassistant.components import assist_pipeline
        pipeline = assist_pipeline.async_get_pipeline(hass)
        engine = getattr(pipeline, "tts_engine", None) if pipeline else None
    except Exception:
        return None
    return engine if engine and hass.states.get(engine) else None


def resolve_tts_entity(hass: HomeAssistant, configured: str) -> str | None:
    """The regular voice: the configured entity if it exists, else (with the
    panel switch on) Home Assistant's own pipeline voice, else the automatic
    choice. None when there is no voice at all."""
    if configured and configured != "auto":
        if hass.states.get(configured):
            return configured
        _LOGGER.warning("Nova: TTS entity '%s' not found — falling back to auto", configured)
    if tts_use_ha_voice(hass):
        pipeline_voice = _ha_pipeline_tts_entity(hass)
        if pipeline_voice:
            _LOGGER.debug("Nova: auto TTS → HA's configured pipeline voice %s", pipeline_voice)
            return pipeline_voice
    found = find_best_tts_entity(hass)
    if not found:
        _LOGGER.debug("Nova: no TTS entity found — broadcast disabled")
    return found


def _premium_set(contexts: Optional[Sequence[str]]) -> set[str]:
    """The known context names out of the premium list; anything else (a
    typo, say) is dropped with a debug line."""
    wanted = set()
    for raw in contexts or []:
        name = (raw or "").strip().lower()
        if name in KNOWN_CONTEXTS:
            wanted.add(name)
        elif name:
            _LOGGER.debug("Nova TTS: unknown premium context '%s' ignored", name)
    return wanted


def resolve_tts_for_context(hass: HomeAssistant, context: str, regular_configured: str,
                            premium_configured: Optional[str] = None,
                            premium_contexts: Optional[Sequence[str]] = None) -> Optional[str]:
    """The voice for one context. A premium context gets the configured
    premium voice, else a premium voice found automatically; every other
    context, or a premium one with no premium voice, gets the regular one.
    """
    if context in _premium_set(premium_contexts):
        if premium_configured and premium_configured != "auto":
            if hass.states.get(premium_configured):
                _LOGGER.debug("Nova: context '%s' → premium TTS %s",
                              context, premium_configured)
                return premium_configured
            _LOGGER.debug("Nova: configured premium TTS '%s' not found — trying auto",
                          premium_configured)
        found = find_premium_tts_entity(hass)
        if found:
            _LOGGER.debug("Nova: context '%s' → auto-picked premium TTS %s",
                          context, found)
            return found
        # No premium voice: the regular one, quietly. Expected on free setups.
    return resolve_tts_entity(hass, regular_configured)


def _history_source(context: str) -> Optional[str]:
    return _HISTORY_SOURCE_MAP.get(context, context)


# ── announcing ──────────────────────────────────────────────────────────────

async def _bounded(call) -> None:
    """Await a blocking service call, giving up waiting after the timeout. A
    timeout is not an error: the speaker accepted and is still busy."""
    try:
        await asyncio.wait_for(call, timeout=_PLAY_MEDIA_TIMEOUT)
    except asyncio.TimeoutError:
        pass


async def _speak_on(hass: HomeAssistant, speaker: str, text: str, voice: str,
                    content_id: str, context: str) -> bool:
    """Play the announcement on one speaker; True when it was taken.

    play_media with ``announce`` plays over whatever is on, at the volume
    the speaker is already at (pinned for Sonos, which supports it). Only
    when that call raises is the older tts.speak tried. A speaker Home
    Assistant does not know, or one that is unavailable or unknown, is
    skipped by Home Assistant without an error, so it counts as failed here
    rather than as delivered.
    """
    st = hass.states.get(speaker)
    if st is None:
        _LOGGER.warning("Nova TTS: %s does not exist (%s) — not speaking there",
                        speaker, context)
        return False
    reading = getattr(st, "state", None)
    if reading in ("unavailable", "unknown"):
        _LOGGER.warning("Nova TTS: %s is %s (%s) — not speaking there",
                        speaker, reading, context)
        return False

    data: dict = {"media_content_id": content_id, "media_content_type": "music",
                  "announce": True}
    volume = st.attributes.get("volume_level")
    if isinstance(volume, (int, float)) and _is_sonos(hass, speaker):
        data["extra"] = {"volume": float(volume)}
    try:
        await _bounded(hass.services.async_call(
            "media_player", "play_media", data,
            target={"entity_id": speaker}, blocking=True))
        return True
    except Exception as exc:
        _LOGGER.warning(
            "Nova TTS: play_media failed on %s (%s): %s — falling back to tts.speak",
            speaker, context, exc)
    try:
        await _bounded(hass.services.async_call(
            "tts", "speak",
            {"media_player_entity_id": [speaker], "message": text, "cache": True},
            target={"entity_id": voice}, blocking=True))
        return True
    except Exception as exc:
        _LOGGER.warning("Nova TTS fallback failed on %s (%s): %s", speaker, context, exc)
        return False


async def _record_spoken(hass, text: str, context: str, speakers: list,
                         repeat_of_id, action_request_id) -> None:
    """Add the delivery to Spoken History. Never raises: a history failure
    must not turn a delivered announcement into a failed one."""
    source = _history_source(context)
    if not source:
        return
    try:
        from . import spoken_history
        await hass.async_add_executor_job(lambda: spoken_history.record(
            text, source, speakers, repeat_of_id,
            action_request_id=action_request_id))
    except Exception:
        pass


async def async_announce(hass: HomeAssistant, text: str, tts_entity: str | None,
                         speakers: Sequence[str], use_announce: bool = True,
                         context: str = "chat", repeat_of_id: Optional[int] = None,
                         *, action_request_id: Optional[str] = None) -> bool:
    """Say ``text`` with ``tts_entity`` on every speaker at once.

    True when at least one speaker took it. Callers that quiet a voice
    satellite to reply here rely on False, so a failed delivery never ends
    in silence.

    The text is stripped of markdown first, TV and film players are dropped
    from the list whatever routing chose them, and each speaker is played
    on its own so one slow or broken speaker never holds up the rest (see
    _speak_on). Every successful delivery is recorded in Spoken History with
    the speakers that took it; this is the one place that happens for
    anything routed through here. ``repeat_of_id`` links a repeat to the
    entry it repeats. No voice option is sent: the engine speaks with its
    own default. ``context`` is for logging and history; the caller has
    already chosen the voice.
    """
    if not text or not tts_entity or not speakers:
        return False
    text = speech_text(text)
    if not text:
        return False
    try:   # never a TV or film player, whatever routing chose
        from .audio_routing import drop_display_targets
        speakers = list(drop_display_targets(hass, speakers, context))
    except Exception:
        speakers = list(speakers)
    if not speakers:
        return False

    _LOGGER.debug("Nova announce [%s]: %s → %s", context, tts_entity, speakers)
    query = urllib.parse.urlencode({"message": text, "cache": "true"})
    content_id = f"media-source://tts/{tts_entity}?{query}"
    outcomes = await asyncio.gather(*(
        _speak_on(hass, spk, text, tts_entity, content_id, context) for spk in speakers))
    took = [spk for spk, ok in zip(speakers, outcomes) if ok]
    missed = [spk for spk, ok in zip(speakers, outcomes) if not ok]

    try:
        from .diagnostics.service_health import record_usage
        record_usage("tts", bool(took), None if took else "all speakers failed")
    except Exception:
        pass

    if not took:
        _LOGGER.warning("Nova TTS failed on every speaker (%s): %s", context, missed)
        return False
    _LOGGER.info("Nova TTS delivered to %d/%d speaker(s); failed: %s",
                 len(took), len(speakers), missed or "none")
    await _record_spoken(hass, text, context, took, repeat_of_id, action_request_id)
    return True
