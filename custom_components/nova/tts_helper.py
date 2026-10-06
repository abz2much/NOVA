"""
Nova — shared TTS announcement helper.

Supports **hybrid TTS routing**: use a cheap local voice for routine chat,
and a premium voice (e.g. ElevenLabs) for "cinematic" moments like briefings,
doorbell announcements, or sentinel alerts.

Configuration flow:
  - CONF_TTS_ENGINE (default tts_helper auto-pick): used for all routine replies
  - CONF_TTS_PREMIUM_ENGINE (optional): used for high-impact contexts
  - CONF_TTS_PREMIUM_CONTEXTS (list): which contexts route to premium, e.g.
    ["briefing", "doorbell", "camera", "sentinel"]

When a context isn't in the premium list, we use the regular TTS engine.
When the premium engine isn't set at all, everything uses regular.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Optional, Sequence

from homeassistant.core import HomeAssistant

from .speech_text import speech_text

_LOGGER = logging.getLogger(__name__)

# Seconds to wait for a speaker to accept an announcement before treating it
# as delivered (it is then still busy fetching or playing the audio).
_PLAY_MEDIA_TIMEOUT = 15


def _is_sonos(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the entity comes from the Sonos integration — the only one
    whose play_media understands `extra: {volume}`. Cast forwards unknown
    extras straight to pychromecast, which rejects `volume` with a TypeError,
    so sending it there made every announcement fail silently."""
    try:
        from homeassistant.helpers import entity_registry as er
        ent = er.async_get(hass).async_get(entity_id)
    except Exception:
        return False
    return ent is not None and ent.platform == "sonos"


# Context labels — passed by each calling service to tell us what it is.
# Services pass one of these strings via the 'context' parameter.
KNOWN_CONTEXTS = {
    "chat",        # normal conversation
    "briefing",    # morning / on-demand briefing
    "camera",      # camera analysis (doorbell included)
    "doorbell",    # explicitly a doorbell event
    "sentinel",    # proactive alerts
    "reminder",    # reminder announcements
    "routine",     # routine narration (goodnight, goodmorning, etc.)
    "recognition", # face recognition announcement
    "summary",     # conversation summary
    "appliance",   # appliance cycle complete (v5.7.00)
    "reply",       # direct conversation reply (v5.7.00)
}


# ─── TTS entity discovery ────────────────────────────────────────────────────

def find_best_tts_entity(hass: HomeAssistant) -> str | None:
    """
    Auto-discover the best free/local TTS entity.
    Priority: piper > edge_tts > any tts.*
    """
    states = hass.states.async_all("tts")
    for state in states:
        if "piper" in state.entity_id.lower():
            return state.entity_id
    for state in states:
        if "edge" in state.entity_id.lower():
            return state.entity_id
    if states:
        return states[0].entity_id
    return None


def find_premium_tts_entity(hass: HomeAssistant) -> str | None:
    """
    Auto-discover a premium TTS entity.
    Priority: elevenlabs > openai > azure > any non-local.
    Explicitly EXCLUDES home_assistant_cloud — that's Nabu Casa's basic
    TTS, not premium quality. It also doesn't support Piper voice options.
    """
    states = hass.states.async_all("tts")
    for preferred in ("elevenlabs", "eleven_labs", "openai", "azure"):
        for state in states:
            if preferred in state.entity_id.lower():
                return state.entity_id
    return None


# Backward compat alias
find_piper_entity = find_best_tts_entity


def tts_use_ha_voice(hass: HomeAssistant) -> bool:
    """Whether the user has switched on "use Home Assistant's configured TTS
    voice" in the panel (config key ``tts_use_ha_voice``, read from the live
    runtime_config the same way :func:`async_announce` already did inline).
    Shared by :func:`resolve_tts_entity` and :func:`async_announce` so both
    agree on the same flag. runtime_config belongs to the Nova entry's
    NovaRuntime."""
    from .runtime import domain_runtime_config
    return bool(domain_runtime_config(hass).get("tts_use_ha_voice"))


def _ha_pipeline_tts_entity(hass: HomeAssistant) -> str | None:
    """The TTS entity configured on HA's preferred Assist pipeline (Settings
    → Voice Assistants), e.g. ``tts.custom_voice`` on a cloud voice-clone
    install. Returns None if assist_pipeline isn't available, has no
    preferred pipeline, or that pipeline's TTS entity no longer exists —
    any of which sends the caller back to the free/local auto-pick."""
    try:
        from homeassistant.components import assist_pipeline
        pipeline = assist_pipeline.async_get_pipeline(hass)
        engine = getattr(pipeline, "tts_engine", None) if pipeline else None
        if engine and hass.states.get(engine):
            return engine
    except Exception:
        pass
    return None


def resolve_tts_entity(hass: HomeAssistant, configured: str) -> str | None:
    """Resolve the regular TTS entity. Preserved for backward compat.

    Nova has no UI to set ``tts_engine`` explicitly (it's config-only), so in
    practice ``configured`` is always "auto" here. Before falling back to the
    free/local auto-pick — which always prefers a Piper entity, regardless of
    what's actually configured for Assist — honour "use Home Assistant's
    configured TTS voice" if that's on, so the toggle actually does what its
    label says instead of only suppressing Nova's own Piper voice option.
    """
    if configured and configured != "auto":
        if hass.states.get(configured):
            return configured
        _LOGGER.warning("Nova: TTS entity '%s' not found — falling back to auto", configured)
    if tts_use_ha_voice(hass):
        pipeline_entity = _ha_pipeline_tts_entity(hass)
        if pipeline_entity:
            _LOGGER.debug("Nova: auto TTS → HA's configured pipeline voice %s", pipeline_entity)
            return pipeline_entity
    found = find_best_tts_entity(hass)
    if found:
        return found
    _LOGGER.debug("Nova: no TTS entity found — broadcast disabled")
    return None


def resolve_tts_for_context(
    hass: HomeAssistant,
    context: str,
    regular_configured: str,
    premium_configured: Optional[str] = None,
    premium_contexts: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """
    Pick the right TTS entity for the given context.

    If context is in premium_contexts AND a premium engine is available,
    return the premium engine. Otherwise return the regular engine.

    premium_contexts is sanitized here — unknown strings are dropped and
    an empty list falls through to regular TTS for everything.

    This means a user can set:
      premium_engine = "tts.elevenlabs"
      premium_contexts = ["briefing", "doorbell", "camera", "recognition"]

    And then:
      - Normal chat replies use Piper (free)
      - Morning briefing uses ElevenLabs (cinematic)
      - Doorbell announcements use ElevenLabs (important)
      - Camera analysis uses ElevenLabs (important)
      - Sentinel "door left open" uses Piper (routine)
      - Face recognition uses ElevenLabs (impressive)
    """
    # Sanitize premium_contexts: keep only recognised context names so a
    # typo in the addon UI doesn't silently break routing.
    cleaned = set()
    for c in (premium_contexts or []):
        c = (c or "").strip().lower()
        if c in KNOWN_CONTEXTS:
            cleaned.add(c)
        elif c:
            _LOGGER.debug(
                "Nova TTS: unknown premium context '%s' ignored", c
            )
    premium_contexts = cleaned
    premium_contexts = set(premium_contexts or [])

    # If this context qualifies for premium treatment...
    if context in premium_contexts:
        # Try the explicitly configured premium engine first
        if premium_configured and premium_configured != "auto":
            if hass.states.get(premium_configured):
                _LOGGER.debug("Nova: context '%s' → premium TTS %s",
                              context, premium_configured)
                return premium_configured
            _LOGGER.debug(
                "Nova: configured premium TTS '%s' not found — trying auto",
                premium_configured,
            )
        # Fall through to auto-discover
        found = find_premium_tts_entity(hass)
        if found:
            _LOGGER.debug("Nova: context '%s' → auto-picked premium TTS %s",
                          context, found)
            return found
        # No premium TTS available — fall through to regular (no warning log,
        # this is expected when running free-only)

    # Regular path — the normal TTS engine
    return resolve_tts_entity(hass, regular_configured)


# ─── Spoken History source mapping ───────────────────────────────────────────
# async_announce is the single recorder for every path that goes through it —
# no other module may call spoken_history.record for the same delivery. A
# context not listed here is recorded under its own name unchanged (kept
# inclusive rather than silently dropping something unanticipated); "chat"
# and the two voice-confirmation contexts are the only ones ever suppressed.
_HISTORY_SOURCE_MAP = {
    "chat": None,        # plain/unlabeled — never one of Nova's own announcements
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


def _history_source(context: str) -> Optional[str]:
    """Map an async_announce `context` to the Spoken History `source` shown
    in the panel, or None to skip recording entirely for that context."""
    return _HISTORY_SOURCE_MAP.get(context, context)


# ─── The announce primitive ──────────────────────────────────────────────────


async def async_announce(
    hass: HomeAssistant,
    text: str,
    tts_entity: str | None,
    speakers: Sequence[str],
    use_announce: bool = True,
    context: str = "chat",
    repeat_of_id: Optional[int] = None,
    *,
    action_request_id: Optional[str] = None,
) -> bool:
    """
    Speak text via tts_entity to the given speaker list. No-op if either empty.

    Returns True if the reply was handed to at least one speaker, False if it
    could not be delivered at all — callers that silence a satellite when they
    route a reply here rely on this so a delivery failure doesn't turn into
    total silence.

    Spoken History (v7.104.0): on any successful delivery, records the text,
    mapped source, and the speakers that actually succeeded to spoken_history
    — this is the ONLY place that happens for anything routed through here;
    a direct speech path that bypasses this function (observer.py,
    proactive_audio.py) records itself instead, never both. `repeat_of_id`
    lets a caller (the panel's Repeat button, or a voice "repeat that" that
    happened to route through here) attribute the new entry back to the
    original it repeated. Recording never raises into this function — a
    history-store failure must never turn a delivered announcement into a
    failed one.

    Delivery (v7.86.0): `media_player.play_media` per speaker, with
    `announce: true` and `extra: {volume: <that speaker's current volume>}`.
    An announcement plays at whatever the speaker is already set to and never
    changes it — the same pattern the `system_presence_based_announcement`
    script already uses for Sonos. One call per speaker (not a single batched
    call) because each speaker can have a different current volume.

    v5.9.11 tried plain `media_player.play_media` (no pinned volume) and
    reverted it: on some targets — notably Cast groups — `play_media` +
    `announce` can succeed *silently*, with no error and no audio, because
    those targets don't honor the `announce` flag. v7.86.0-v7.87.0 tried to
    guard against that by polling the target's state and falling back to
    `tts.speak` if it never visibly moved. Removed again here: the signal was
    wrong, not just slow — Sonos plays an announcement correctly without
    reliably touching any state Home Assistant can see, so the guard fired on
    every Sonos announcement and played it a second time at the wrong,
    un-pinned volume. We now trust `play_media`/`announce` the same way
    `system_presence_based_announcement` already does for Sonos, and only
    fall back to `tts.speak` if the service call itself raises.

    v7.127.1: the call is blocking (with a timeout) so it CAN raise — it was
    fire-and-forget before, so a failure never reached the fallback and was
    still recorded in Spoken History as sent. The pinned volume is sent to
    Sonos speakers only (see :func:`_is_sonos`), and an unavailable speaker
    counts as failed, since Home Assistant skips it without an error.

    No voice is requested: the TTS engine speaks with its own default voice,
    which is whatever Home Assistant (for Piper, the add-on) is set up to use.

    `context` is accepted for logging; callers resolve the entity beforehand.
    """
    if not text or not tts_entity or not speakers:
        return False

    # Whatever wrote this text (the model, a briefing, an alert), a speaker
    # and Spoken History show it literally, so markdown is removed here once
    # for every caller. Plain text passes through unchanged.
    text = speech_text(text)
    if not text:
        return False

    # Final safety net: never speak through a TV/movie player, whatever routing
    # produced this list. Logs (with context) if it has to drop one.
    try:
        from .audio_routing import drop_display_targets
        speakers = drop_display_targets(hass, speakers, context)
    except Exception:
        pass
    if not speakers:
        return False

    _LOGGER.debug("Nova announce [%s]: %s → %s", context, tts_entity, speakers)

    def _media_content_id(message: str) -> str:
        params = {"message": message, "cache": "true"}
        return f"media-source://tts/{tts_entity}?{urllib.parse.urlencode(params)}"

    async def _fallback_speak(spk: str) -> bool:
        """Old tts.speak delivery — proven to play everywhere, used when the
        volume-pinned play_media delivery doesn't visibly do anything."""
        last_err: Exception | None = None
        try:
            one = {"media_player_entity_id": [spk], "message": text, "cache": True}
            await asyncio.wait_for(
                hass.services.async_call(
                    "tts", "speak", one, target={"entity_id": tts_entity}, blocking=True,
                ),
                timeout=_PLAY_MEDIA_TIMEOUT,
            )
            return True
        except asyncio.TimeoutError:
            return True
        except Exception as sub:
            last_err = sub
        _LOGGER.warning("Nova TTS fallback failed on %s (%s): %s", spk, context, last_err)
        return False

    async def _deliver(spk: str) -> bool:
        st = hass.states.get(spk)
        # Home Assistant silently skips an unavailable entity in a service
        # call, so play_media would "succeed" with nothing played. Count it
        # as failed instead of recording it in Spoken History as sent.
        if getattr(st, "state", None) in ("unavailable", "unknown"):
            _LOGGER.warning("Nova TTS: %s is %s (%s) — not speaking there",
                            spk, st.state, context)
            return False
        vol = st.attributes.get("volume_level") if st is not None else None
        vol = float(vol) if isinstance(vol, (int, float)) else None

        data = {
            "media_content_id": media_content_id,
            "media_content_type": "music",
            "announce": True,
        }
        if vol is not None and _is_sonos(hass, spk):
            data["extra"] = {"volume": vol}

        try:
            await asyncio.wait_for(
                hass.services.async_call(
                    "media_player", "play_media", data,
                    target={"entity_id": spk}, blocking=True,
                ),
                timeout=_PLAY_MEDIA_TIMEOUT,
            )
            return True
        except asyncio.TimeoutError:
            return True  # accepted, still fetching or playing the audio
        except Exception as exc:
            _LOGGER.warning(
                "Nova TTS: play_media failed on %s (%s): %s — falling back to tts.speak",
                spk, context, exc)
            return await _fallback_speak(spk)

    media_content_id = _media_content_id(text)
    speakers = list(speakers)
    # Every speaker at once, so a slow one doesn't hold up the rest.
    results = await asyncio.gather(*(_deliver(spk) for spk in speakers))
    succeeded = [spk for spk, ok in zip(speakers, results) if ok]
    failed = [spk for spk, ok in zip(speakers, results) if not ok]
    delivered = len(succeeded)

    try:
        from .diagnostics.service_health import record_usage
        record_usage("tts", delivered > 0, None if delivered else "all speakers failed")
    except Exception:
        pass

    if delivered:
        _LOGGER.info("Nova TTS delivered to %d/%d speaker(s); failed: %s",
                     delivered, len(list(speakers)), failed or "none")
        source = _history_source(context)
        if source:
            try:
                from . import spoken_history
                await hass.async_add_executor_job(
                    lambda: spoken_history.record(
                        text, source, succeeded, repeat_of_id,
                        action_request_id=action_request_id,
                    )
                )
            except Exception:
                pass  # recording must never turn a delivered announcement into a failed one
    else:
        _LOGGER.warning("Nova TTS failed on every speaker (%s): %s", context, failed)
    return delivered > 0
