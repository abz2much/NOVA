"""Natural turn-taking for voice satellites.

Decides whether a reply invites an answer (so the mic stays open without a
new wake word), reads the feature flags and finds a device's assist
satellite. For a mic-only satellite whose reply plays on a separate speaker
it schedules a reopen that waits for the speaker to finish first; with
multi-satellite handoff on, the reopen moves to the room the person walked
into, but only when the starting room is clearly empty. It also remembers,
per device for a minute, which conversation a Nova-driven reopen belongs to
so the next turn can carry that history once. Off by default; memory only.
"""
from __future__ import annotations

import re
from typing import Optional

_INVITE_PHRASES = (
    "let me know",
    "which would you",
    "which one would you",
    "shall i",
    "would you like",
    "do you want me to",
    "should i",
    "want me to",
)
_INVITE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _INVITE_PHRASES) + r")\b",
    re.IGNORECASE,
)

_PLAYING_STATES = {"playing", "buffering"}
_DONE_STATES = {"idle", "paused", "standby", "off", "unavailable", "unknown"}

REOPEN_TTL_SECONDS = 60.0
_REOPENED: dict = {}

_POLL = 0.25
_START_POLLS = 16
_FINISH_GRACE = 8.0
_SETTLE = 0.5


def _flag(key: str, default: bool) -> bool:
    try:
        from . import nova_config
        return bool(nova_config.get(key, default))
    except Exception:  # noqa: BLE001
        return default


def enabled() -> bool:
    """Whether continued conversation is switched on."""
    return _flag("continued_conversation_enabled", False)


def speaker_reopen_enabled() -> bool:
    """Whether a mic-only satellite reopens after a separate speaker."""
    return _flag("continued_conversation_speaker_reopen", True)


def _multi_satellite_enabled() -> bool:
    return _flag("continued_conversation_multi_satellite", False)


def should_continue(text: str) -> bool:
    """True when the reply asks something or invites an answer."""
    reply = (text or "").strip()
    if not reply:
        return False
    if reply.endswith("?"):
        return True
    return _INVITE.search(reply) is not None


def satellite_for_device(hass, device_id: str) -> Optional[str]:
    """The assist_satellite entity belonging to ``device_id``, if any."""
    if not device_id:
        return None
    try:
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(hass)
        for entry in registry.entities.values():
            if entry.device_id == device_id and entry.domain == "assist_satellite":
                return entry.entity_id
    except Exception:  # noqa: BLE001
        return None
    return None


def _speech_seconds(text: str) -> float:
    """Rough time to speak ``text`` aloud."""
    return min(20.0, max(1.5, len(text or "") / 12.0 + 0.8))


def _pick_reopen_target(hass, original_satellite: str) -> str:
    """Satellite to reopen on: the person's new room when they clearly moved."""
    if not _multi_satellite_enabled():
        return original_satellite
    try:
        from . import audio_routing
        start = audio_routing.entity_area(hass, original_satellite)
        if not start or audio_routing.is_area_occupied(hass, start):
            return original_satellite
        for area in audio_routing.currently_occupied_areas(hass):
            if area == start:
                continue
            found = audio_routing.satellites_in_area(hass, area)
            if found:
                return found[0]
    except Exception:  # noqa: BLE001
        return original_satellite
    return original_satellite


def _speaker_state(hass, speaker_entity_id: str) -> Optional[str]:
    state = hass.states.get(speaker_entity_id)
    return state.state if state is not None else None


async def _reopen_after_speaker(hass, satellite_entity_id: str,
                                speaker_entity_id: str, reply_text: str,
                                follow: bool = True) -> None:
    """Wait for the speaker to finish the reply, then reopen the mic."""
    import asyncio
    import time

    est = _speech_seconds(reply_text)

    started = False
    for _ in range(_START_POLLS):
        await asyncio.sleep(_POLL)
        if _speaker_state(hass, speaker_entity_id) in _PLAYING_STATES:
            started = True
            break

    if started:
        deadline = time.monotonic() + est + _FINISH_GRACE
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL)
            current = _speaker_state(hass, speaker_entity_id)
            if current is None or current in _DONE_STATES:
                break
        await asyncio.sleep(_SETTLE)
    else:
        await asyncio.sleep(est)

    target = _pick_reopen_target(hass, satellite_entity_id) if follow \
        else satellite_entity_id
    try:
        payload = {"entity_id": target, "start_message": ""}
        await hass.services.async_call("assist_satellite", "start_conversation",
                                       payload, blocking=False)
    except Exception:  # noqa: BLE001
        pass


def schedule_reopen(hass, satellite_entity_id: str, speaker_entity_id: str,
                    reply_text: str, *, follow: bool = True) -> bool:
    """Start the speaker-aware reopen in the background. False if not possible."""
    if not satellite_entity_id or not speaker_entity_id:
        return False
    hass.async_create_task(
        _reopen_after_speaker(hass, satellite_entity_id, speaker_entity_id,
                              reply_text, follow=follow)
    )
    return True


def remember_reopen(device_id: str, conversation_id: str, *, now=None) -> None:
    """Note which conversation a reopen on ``device_id`` continues."""
    if not device_id or not conversation_id:
        return
    if now is None:
        import time
        now = time.monotonic()
    _REOPENED[str(device_id)] = (str(conversation_id), now + REOPEN_TTL_SECONDS)


def take_reopened(device_id: str, *, now=None) -> Optional[str]:
    """Consume the remembered conversation for ``device_id`` if still fresh."""
    if not device_id:
        return None
    entry = _REOPENED.pop(str(device_id), None)
    if entry is None:
        return None
    import time
    current = now or time.monotonic()
    conversation_id, expiry = entry
    return conversation_id if current < expiry else None
