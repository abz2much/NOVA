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
import json
import logging
import urllib.parse
from typing import Optional, Sequence

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

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


def resolve_tts_entity(hass: HomeAssistant, configured: str) -> str | None:
    """Resolve the regular TTS entity. Preserved for backward compat."""
    if configured and configured != "auto":
        if hass.states.get(configured):
            return configured
        _LOGGER.warning("Nova: TTS entity '%s' not found — falling back to auto", configured)
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


# ─── The announce primitive ──────────────────────────────────────────────────

async def async_announce(
    hass: HomeAssistant,
    text: str,
    tts_entity: str | None,
    speakers: Sequence[str],
    use_announce: bool = True,
    context: str = "chat",
) -> bool:
    """
    Speak text via tts_entity to the given speaker list. No-op if either empty.

    Returns True if the reply was handed to at least one speaker, False if it
    could not be delivered at all — callers that silence a satellite when they
    route a reply here rely on this so a delivery failure doesn't turn into
    total silence.

    Delivery (v7.86.0): `media_player.play_media` per speaker, with
    `announce: true` and `extra: {volume: <that speaker's current volume>}`.
    An announcement plays at whatever the speaker is already set to and never
    changes it — the same pattern Abi's own `system_presence_based_announcement`
    script already uses for Sonos. One call per speaker (not a single batched
    call) because each speaker can have a different current volume.

    v5.9.11 tried plain `media_player.play_media` (no pinned volume) and
    reverted it: on some targets — notably Cast groups — `play_media` +
    `announce` can succeed *silently*, with no error and no audio, because
    those targets don't honor the `announce` flag. To guard against that
    regression resurfacing here, each call is verified — if the target's
    state hasn't moved within ~2 seconds, we fall back to the old `tts.speak`
    delivery for that one speaker, which is proven to play everywhere. This
    is a best-effort check (an unrelated state refresh could count as
    "moved"), not a guarantee.

    The nova voice is requested via `tts_options` on the media-source URL. We
    deliberately do NOT send a `language` field alongside it: with some
    Piper/Wyoming builds, a language hint makes the engine fall back to a
    language-default voice instead of honoring the explicit `voice`. The voice
    string already encodes its language (en_GB), so Piper infers it correctly.

    `context` is accepted for logging; callers resolve the entity beforehand.
    """
    if not text or not tts_entity or not speakers:
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

    is_piper = "piper" in tts_entity.lower()

    # When the user prefers Home Assistant's configured voice, don't force the
    # Nova Piper voice — omit the `voice` option entirely so the TTS entity
    # uses its own default (e.g. a French fr_FR-tom voice on a French install).
    # Default off keeps the Nova voice for everyone who has it. Read from the
    # live runtime_config (seeded from config.json at setup, updated by the
    # panel) so we don't import nova_config on this path.
    use_ha_voice = False
    try:
        from .const import DOMAIN
        for _ed in (hass.data.get(DOMAIN) or {}).values():
            if isinstance(_ed, dict) and (
                    _ed.get("runtime_config") or {}).get("tts_use_ha_voice"):
                use_ha_voice = True
                break
    except Exception:
        use_ha_voice = False

    # Request the Nova Piper voice. If it isn't installed (VoiceNotFoundError),
    # the fallback below retries without it, using the engine's default voice,
    # so a missing custom voice is never fatal.
    tts_options: dict | None = None
    if is_piper and not use_ha_voice:
        tts_options = {"voice": "en_GB-nova-high"}

    def _media_content_id(message: str) -> str:
        params = {"message": message, "cache": "true"}
        if tts_options:
            params["tts_options"] = json.dumps(tts_options, separators=(",", ":"))
        return f"media-source://tts/{tts_entity}?{urllib.parse.urlencode(params)}"

    async def _fallback_speak(spk: str) -> bool:
        """Old tts.speak delivery — proven to play everywhere, used when the
        volume-pinned play_media delivery doesn't visibly do anything."""
        for opts in ([tts_options, None] if tts_options is not None else [None]):
            try:
                one = {"media_player_entity_id": [spk], "message": text, "cache": True}
                if opts:
                    one["options"] = opts
                await hass.services.async_call(
                    "tts", "speak", one, target={"entity_id": tts_entity}, blocking=False,
                )
                return True
            except Exception as sub:
                last_err = sub
        _LOGGER.warning("Nova TTS fallback failed on %s (%s): %s", spk, context, last_err)
        return False

    media_content_id = _media_content_id(text)
    delivered, failed = 0, []
    for spk in list(speakers):
        st = hass.states.get(spk)
        before = st.last_updated if st is not None else None
        vol = st.attributes.get("volume_level") if st is not None else None
        vol = float(vol) if isinstance(vol, (int, float)) else None

        data = {
            "media_content_id": media_content_id,
            "media_content_type": "music",
            "announce": True,
        }
        if vol is not None:
            data["extra"] = {"volume": vol}

        ok = False
        try:
            await hass.services.async_call(
                "media_player", "play_media", data,
                target={"entity_id": spk}, blocking=False,
            )
            # v5.9.11 regression guard: that path can succeed silently on
            # targets that don't honor `announce`. Give it a moment, then
            # check something actually happened.
            await asyncio.sleep(2)
            st2 = hass.states.get(spk)
            moved = st2 is not None and (before is None or st2.last_updated != before)
            if moved:
                ok = True
            else:
                _LOGGER.info(
                    "Nova TTS: %s didn't respond to play_media/announce — "
                    "falling back to tts.speak (context=%s)", spk, context)
                ok = await _fallback_speak(spk)
        except Exception as exc:
            _LOGGER.warning(
                "Nova TTS: play_media failed on %s (%s): %s — falling back to tts.speak",
                spk, context, exc)
            ok = await _fallback_speak(spk)

        if ok:
            delivered += 1
        else:
            failed.append(spk)

    try:
        from .diagnostics.service_health import record_usage
        record_usage("tts", delivered > 0, None if delivered else "all speakers failed")
    except Exception:
        pass

    if delivered:
        _LOGGER.info("Nova TTS delivered to %d/%d speaker(s); failed: %s",
                     delivered, len(list(speakers)), failed or "none")
    else:
        _LOGGER.warning("Nova TTS failed on every speaker (%s): %s", context, failed)
    return delivered > 0
