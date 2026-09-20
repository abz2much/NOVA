"""
Nova — Proactive Briefing System (v5.8.01).

Triggers briefings automatically when Nova determines they're warranted.
Collects camera detection snapshots for inclusion in briefings.
Pushes briefings to phone when user is away.

Triggers:
  - Arrival home (person.* → home): welcome briefing
  - Significant security events accumulated (3+ in 30 min)
  - Time-based (morning briefing, evening summary)
  - Unusual activity detected by cameras

Camera snapshots are stored in a ring buffer — the last N detection
snapshots with their analysis text. Briefings include a summary of
recent detections without re-analyzing images.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


# ── Camera snapshot ring buffer ─────────────────────────────────────────────

@dataclass
class CameraSnapshot:
    """A detection snapshot stored for briefing inclusion."""
    timestamp: float
    camera_name: str
    camera_entity: str
    analysis: str          # The LLM analysis text
    detection_type: str    # "person", "vehicle", "motion", "doorbell"
    urgency: str = "low"   # low, medium, high


# Ring buffer — last 50 snapshots
_SNAPSHOTS: deque[CameraSnapshot] = deque(maxlen=50)


def record_snapshot(
    camera_name: str,
    camera_entity: str,
    analysis: str,
    detection_type: str = "motion",
    urgency: str = "low",
) -> None:
    """Called by camera.py after analyzing a snapshot."""
    _SNAPSHOTS.append(CameraSnapshot(
        timestamp=time.time(),
        camera_name=camera_name,
        camera_entity=camera_entity,
        analysis=analysis,
        detection_type=detection_type,
        urgency=urgency,
    ))
    _LOGGER.debug(
        "Proactive: recorded snapshot from %s (%s, %s)",
        camera_name, detection_type, urgency,
    )


def get_recent_snapshots(hours: float = 12) -> list[CameraSnapshot]:
    """Get snapshots from the last N hours."""
    cutoff = time.time() - (hours * 3600)
    return [s for s in _SNAPSHOTS if s.timestamp > cutoff]


def get_snapshot_summary(hours: float = 12) -> str:
    """Build a text summary of recent camera detections for briefing inclusion."""
    recent = get_recent_snapshots(hours)
    if not recent:
        return ""

    # Group by camera
    by_camera: dict[str, list[CameraSnapshot]] = {}
    for s in recent:
        by_camera.setdefault(s.camera_name, []).append(s)

    lines = []
    for cam, snaps in by_camera.items():
        types = {}
        for s in snaps:
            types[s.detection_type] = types.get(s.detection_type, 0) + 1
        type_str = ", ".join(f"{count} {dtype}" for dtype, count in types.items())
        latest = snaps[-1]
        lines.append(
            f"  {cam}: {len(snaps)} detection(s) ({type_str}). "
            f"Latest: {latest.analysis[:120]}"
        )

    return "Camera detections:\n" + "\n".join(lines)


# ── Event accumulator for proactive triggers ────────────────────────────────

@dataclass
class _ProactiveState:
    hass: Optional[HomeAssistant] = None
    config: dict = field(default_factory=dict)
    unsub_presence: Optional[object] = None
    unsub_timer: Optional[object] = None
    security_events: list = field(default_factory=list)  # timestamps
    last_briefing_time: float = 0.0
    last_arrival_briefing: float = 0.0
    running: bool = False
    # Presence (GPS/zone) says someone's home, but the welcome briefing waits
    # for the configured front door to actually open (v7.101.9) — otherwise
    # it fires while they're still in the driveway/car, before they've
    # actually walked in. Cleared on a matching door-open or once stale.
    pending_arrival_person: str = ""
    pending_arrival_ts: float = 0.0

_STATE = _ProactiveState()

# Minimum time between proactive briefings (minutes)
BRIEFING_COOLDOWN = 30
ARRIVAL_COOLDOWN = 60  # Don't re-brief on every presence toggle
SECURITY_THRESHOLD = 3  # events in 30 min to trigger security briefing
ARRIVAL_DOOR_WINDOW_S = 600  # give up waiting for the door after 10 min


# ── Arrival detection ───────────────────────────────────────────────────────

def _anyone_home(hass) -> bool:
    """True if any registered person or tracked device is home."""
    try:
        for dom in ("person", "device_tracker"):
            for s in hass.states.async_all(dom):
                if s.state == "home":
                    return True
    except Exception:
        pass
    return False


def _configured_front_door() -> str:
    """The binary_sensor gating the arrival-briefing trigger, or "" if unset
    (Settings -> General -> Arrival). Unset means stay silent on arrival
    entirely — see the person-arrived branch below."""
    try:
        from . import nova_config
        return str(nova_config.get("arrival_front_door_entity", "") or "")
    except Exception:
        return ""


@callback
def _on_state_changed(event: Event) -> None:
    """Watch for person arrivals and security event accumulation."""
    if not _STATE.running:
        return

    entity_id = event.data.get("entity_id", "")
    new_state = event.data.get("new_state")
    old_state = event.data.get("old_state")
    if not new_state:
        return

    # ── Person arrived home (GPS/zone) ───────────────────────────────
    # This only marks a PENDING arrival (v7.101.9) — GPS/zone presence can
    # flip to "home" while someone's still in the driveway or car, well
    # before they've actually walked in. The welcome briefing itself fires
    # below, once the configured front door actually opens. No front door
    # configured means no way to know when they're actually inside, so stay
    # silent entirely rather than fall back to the old premature trigger.
    if entity_id.startswith("person."):
        old_val = old_state.state if old_state else "unknown"
        new_val = new_state.state
        if old_val != "home" and new_val == "home":
            door_entity = _configured_front_door()
            if not door_entity:
                return
            _STATE.pending_arrival_person = new_state.attributes.get(
                "friendly_name", entity_id.split(".")[-1].title()
            )
            _STATE.pending_arrival_ts = time.time()
            _LOGGER.info(
                "Proactive: %s is home (presence) — waiting for %s to open before announcing",
                _STATE.pending_arrival_person, door_entity,
            )
        return

    # ── Configured front door opened: consume a pending arrival ──────
    door_entity = _configured_front_door()
    if door_entity and entity_id == door_entity and new_state.state == "on":
        if _STATE.pending_arrival_person:
            person_name = _STATE.pending_arrival_person
            fresh = (time.time() - _STATE.pending_arrival_ts) <= ARRIVAL_DOOR_WINDOW_S
            _STATE.pending_arrival_person = ""
            if fresh:
                now = time.time()
                if (now - _STATE.last_arrival_briefing) > ARRIVAL_COOLDOWN * 60:
                    _STATE.last_arrival_briefing = now
                    _LOGGER.info(
                        "Proactive: %s opened the front door — triggering welcome briefing",
                        person_name,
                    )
                    _STATE.hass.async_create_task(
                        _trigger_briefing("arrival", person_name=person_name)
                    )
            # else: stale (>10 min since presence flipped home) — drop it
            # silently rather than announce a late/wrong-context arrival.
        # No pending arrival — an ordinary door open with someone already
        # home; fall through to the security-accumulation check below (it
        # already no-ops whenever anyone_home is true).

    # ── Security event accumulation ─────────────────────────────────
    # Watch for doors opening, locks unlocking, motion at unusual times
    domain = entity_id.split(".")[0]
    is_security = False

    if domain == "binary_sensor":
        dc = new_state.attributes.get("device_class", "")
        if dc in ("door", "window", "garage_door") and new_state.state == "on":
            is_security = True
    elif domain == "lock" and new_state.state == "unlocked":
        is_security = True

    if is_security:
        # Open windows / unlocked doors are NORMAL when a registered user is
        # home — only treat them as security-relevant when the house is empty.
        if _anyone_home(_STATE.hass):
            return
        now = time.time()
        _STATE.security_events.append(now)
        # Clean old events (> 30 min)
        _STATE.security_events = [t for t in _STATE.security_events if now - t < 1800]

        if (len(_STATE.security_events) >= SECURITY_THRESHOLD
                and (now - _STATE.last_briefing_time) > BRIEFING_COOLDOWN * 60):
            _LOGGER.info(
                "Proactive: %d security events in 30min (house empty) — triggering security briefing",
                len(_STATE.security_events),
            )
            _STATE.hass.async_create_task(_trigger_briefing("security"))
            _STATE.security_events.clear()


# ── Action-claim guard ───────────────────────────────────────────────────────
# _trigger_briefing's LLM call below is a single-shot text completion with no
# device-control tools at all — nothing stops it writing in-character flavor
# text that CLAIMS a device action happened ("I've nudged the thermostat up
# two degrees") when nothing was ever executed (the 2026-09-18 08:54
# incident). This is detected deterministically (no second LLM call) and,
# if found, the ENTIRE generated briefing is discarded for a safe fallback —
# never edited sentence-by-sentence, since a partial edit of model text can
# leave behind damaged or misleading wording. Kept generic across every
# controllable domain (lights, locks, covers, climate, media players,
# scenes, scripts, ...), not just climate.
#
# Device-action QUESTIONS/OFFERS ("Would you like me to raise the
# thermostat?") are rejected too, not just completed/promised claims. This
# branch has no mechanism to turn a "yes" reply into a real action — no
# device-control tools, and no pending-offer system — so an offer here
# would be a dead end for the user. Questions unrelated to device control
# (e.g. offering to read out the calendar) are unaffected.
import re as _re

_ACTION_VERBS = (
    r"turn(?:ed|ing)?|switch(?:ed|ing)?|set(?:ting)?|adjust(?:ed|ing)?|"
    r"nudg(?:ed|e|ing)|lock(?:ed|ing)?|unlock(?:ed|ing)?|open(?:ed|ing)?|"
    r"clos(?:ed|e|ing)|start(?:ed|ing)?|stopp?(?:ed|ing)?|"
    r"activat(?:ed|e|ing)|deactivat(?:ed|e|ing)|dimm?(?:ed|ing)?|"
    r"rais(?:ed|e|ing)|lower(?:ed|ing)?|arm(?:ed|ing)?|disarm(?:ed|ing)?|"
    r"paus(?:ed|e|ing)|resum(?:ed|e|ing)|play(?:ed|ing)?|runn?(?:ing)?|ran|"
    r"launch(?:ed|ing)?"
)
# Deliberately excludes observation verbs (checked, noticed, seen,
# confirmed, ...) — those describe what Nova perceived, not an action it
# took or is offering, and must never be treated as a control claim.
_APOSTROPHE = r"['’]"  # straight and curly ('ve vs 've)
_FIRST_PERSON_ACTION_CLAIM = _re.compile(
    rf"\bI(?:{_APOSTROPHE}ve|\s+have|\s+just|\s+already|{_APOSTROPHE}ll|"
    rf"\s+will|{_APOSTROPHE}m|\s+am(?:\s+going\s+to)?)?"
    rf"\s+(?:{_ACTION_VERBS})\b",
    _re.IGNORECASE,
)
# "Would you like me to raise..." / "Do you want me to lock..." — offer
# phrasing whose subject is "me"/"you", not "I", so it never matches
# _FIRST_PERSON_ACTION_CLAIM above. ("Shall I turn off the lights?" /
# "Should I lock the door?" / "Can I raise the thermostat?" DO contain a
# literal "I <verb>" and are already caught by _FIRST_PERSON_ACTION_CLAIM
# now that questions are no longer exempt from it — see below.)
_OFFER_ACTION_PATTERN = _re.compile(
    rf"\b(?:would you like (?:me )?to|do you want me to|want me to)"
    rf"\s+(?:{_ACTION_VERBS})\b",
    _re.IGNORECASE,
)
_DEVICE_NOUN = _re.compile(
    r"\b(light|lamp|lock|door|thermostat|climate|heat(?:ing)?|cover|blind|"
    r"curtain|shade|garage|media\s*player|speaker|tv|television|scene|"
    r"script|fan|switch|outlet|plug|alarm|siren|camera)s?\b",
    _re.IGNORECASE,
)


def _split_sentences(text: str) -> list[str]:
    """Sentence split good enough to scope matching per sentence — not meant
    to reconstruct polished text."""
    return _re.split(r"(?<=[.!?])\s+", text.strip())


def contains_unsupported_device_action_claim(text: str) -> bool:
    """True if `text` contains a first-person claim that Nova performed, is
    performing, or will perform a device action, OR a question/offer asking
    whether Nova should perform one. This briefing path has no way to turn
    either into a real action (no device-control tools, no pending-offer
    system), so both are rejected — regardless of phrasing as a statement
    or a question. Plain factual state reports ("the thermostat is at 18")
    and questions unrelated to device control (e.g. offering to read out
    the calendar) are never flagged."""
    for sentence in _split_sentences(text):
        stripped = sentence.strip()
        if not stripped:
            continue
        if not _DEVICE_NOUN.search(stripped):
            continue
        if _FIRST_PERSON_ACTION_CLAIM.search(stripped) or _OFFER_ACTION_PATTERN.search(stripped):
            return True
    return False


def _deterministic_fallback_briefing(
    *, honorific: str, greeting: str, reason: str,
    weather: str = "", open_things: list[str] | None = None,
    events: list[str] | None = None,
) -> str:
    """A safe briefing built only from context already verified elsewhere in
    _trigger_briefing — used when the generated text fails the action-claim
    guard. Never claims a device was changed and never mentions a device
    action at all, question or otherwise — this function only ever emits
    the greeting plus verified weather/security/event facts."""
    parts = []
    if honorific:
        parts.append(f"Welcome home, {honorific}." if reason == "arrival"
                      else f"{greeting}, {honorific}.")
    else:
        parts.append(f"{greeting}.")
    if weather:
        parts.append(f"{weather}.")
    if open_things:
        parts.append(f"Open/unlocked: {', '.join(open_things)}.")
    if events:
        parts.append(f"Recent events: {'; '.join(events[:3])}.")
    return " ".join(parts)


# ── Briefing trigger ────────────────────────────────────────────────────────

async def _trigger_briefing(
    reason: str,
    person_name: str = "",
) -> None:
    """Fire a proactive briefing through the existing briefing system."""
    hass = _STATE.hass
    config = _STATE.config
    now = time.time()

    if (now - _STATE.last_briefing_time) < BRIEFING_COOLDOWN * 60:
        _LOGGER.debug("Proactive: briefing cooldown active, skipping")
        return

    _STATE.last_briefing_time = now
    try:
        from . import honorific as honorific_mod
        honorific = honorific_mod.effective_honorific(hass)  # Phase C: presence-aware
    except Exception:
        honorific = config.get("honorific", "sir")

    # Gather camera snapshot summary
    snap_summary = get_snapshot_summary(hours=4)

    # Check if anyone is home
    anyone_home = any(
        s.state == "home" for s in hass.states.async_all("person")
    )

    # Build extra context based on reason
    extra_context = ""
    if reason == "arrival":
        if honorific:
            # honorific is non-empty only when exactly one person is home
            # (see honorific.py) — that's the person who just walked in, so
            # this briefing is spoken directly to them. Telling the model
            # their name AND instructing it to greet "sir"/"ma'am" produced
            # redundant lines like "Good afternoon, sir. Abi has just
            # arrived home." Drop the name restatement; the greeting alone
            # already addresses them.
            extra_context = (
                "This welcome-home briefing is addressed directly to the "
                "person who just walked in. Do not restate their name or "
                "the fact that they arrived — the greeting already covers that."
            )
        else:
            # Nobody specific is being addressed (others already home, or
            # this is a whole-house broadcast) — naming who arrived is the
            # useful part for everyone else.
            extra_context = f"{person_name} just arrived home. This is a welcome briefing."
    elif reason == "security":
        extra_context = (
            "Multiple security events detected in a short period. "
            "Summarize what happened and any concerns."
        )
    elif reason == "scheduled":
        extra_context = "This is a scheduled briefing."

    if snap_summary:
        extra_context += f"\n\n{snap_summary}"

    # Try to use the existing briefing service
    try:
        from .briefing import _gather_weather, _gather_open_things, _gather_overnight_events
        from .briefing import _gather_calendar, _gather_energy_anomalies, _time_greeting
        from .directive_helper import build_system_prompt
        from .tts_helper import resolve_tts_for_context, async_announce
        from .audio_routing import observer_speak_target
        from . import sleep_detection

        # Gather context
        context_lines = [f"It is {datetime.now().strftime('%A %B %-d, %-I:%M %p')}."]
        weather = _gather_weather(hass)
        if weather:
            context_lines.append(f"Weather: {weather}.")
        open_things = _gather_open_things(hass)
        if reason == "arrival":
            # The arrival trigger IS a door opening (see _configured_front_door)
            # — reporting that door as "open" on the very briefing it caused
            # is stating the obvious. Unlocked locks are still worth a
            # mention (that's not self-evident from having just walked in).
            open_things = [item for item in open_things if not item.endswith("is open")]
        if open_things:
            context_lines.append(f"Open/unlocked: {', '.join(open_things)}.")
        events = _gather_overnight_events(hass, 4)
        if events:
            context_lines.append(f"Recent events: {'; '.join(events[:5])}.")
        if extra_context:
            context_lines.append(extra_context)

        context = "\n".join(context_lines)
        greeting = _time_greeting()

        # honorific may be "" once nobody specific is home to address (see
        # honorific.py) — instruct the model accordingly instead of leaving a
        # blank subject or a dangling "Begin with 'Good morning, .'"
        if honorific:
            to_whom = f"to {honorific}"
            if reason == "arrival":
                begin_with = f"Begin with 'Welcome home, {honorific}.'"
            else:
                begin_with = f"Begin with '{greeting}, {honorific}.'"
        else:
            to_whom = "to the household"
            begin_with = f"Begin with '{greeting}.'"
        task = (
            f"You are delivering a proactive briefing ({reason}) {to_whom}. "
            f"{begin_with} "
            f"Cover only the important items. Under 100 words. Be direct. "
            f"This message has NO device-control tools — you cannot turn "
            f"anything on or off, lock or unlock anything, or adjust any "
            f"device right now, and nothing you say here can be followed up "
            f"on or acted on later. Do not claim, promise, or imply that you "
            f"changed or will change a device (never say things like "
            f"\"I've turned up the heat\" or \"I'll lock the door\"). Do not "
            f"ask whether the user wants you to change a device either "
            f"(never say things like \"Would you like me to raise the "
            f"thermostat?\" or \"Shall I turn off the lights?\") — if they "
            f"want a device changed, they can ask you directly by voice or "
            f"chat afterward. Report verified current state only (e.g. "
            f"\"the thermostat is at 18 degrees\"). Questions unrelated to "
            f"device control — for example offering to read out the "
            f"calendar — are fine."
        )
        system = build_system_prompt(hass, honorific, task)

        # Generate briefing via LLM
        from .llm_provider import create_provider, create_tier_provider
        try:
            provider = await hass.async_add_executor_job(
                create_tier_provider, config, "reasoning",
            )
        except Exception:
            provider = await hass.async_add_executor_job(
                create_provider,
                config.get("llm_provider", "groq"),
                config.get("api_key", ""),
                config.get("model", "openai/gpt-oss-120b"),
                config.get("llm_base_url"),
            )

        result = await hass.async_add_executor_job(
            provider.chat,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": context},
            ],
            None, 300, 0.6,
        )
        briefing_text = result.get("text", "").strip()
        if not briefing_text:
            return

        if contains_unsupported_device_action_claim(briefing_text):
            _LOGGER.warning(
                "Proactive briefing (%s) rejected — unsupported device-action "
                "claim in generated text; using deterministic fallback: %s",
                reason, briefing_text[:200],
            )
            briefing_text = _deterministic_fallback_briefing(
                honorific=honorific, greeting=greeting, reason=reason,
                weather=weather, open_things=open_things, events=events,
            )
            if not briefing_text:
                return

        _LOGGER.info("Proactive briefing (%s): %s", reason, briefing_text[:100])

        # Action Audit Log / Spoken History linkage (v3 correction): one
        # request_id for this whole briefing event — the spoken copy (if
        # any) and the phone push (if any) are two sides of the SAME
        # request, not independent actions.
        from . import action_log
        request_id = action_log.new_request_id()

        # Route: speak at home, push notification when away
        if anyone_home:
            # Check sleep
            bedroom_areas = config.get("bedroom_areas", []) or []
            sleeping, _ = sleep_detection.is_sleeping(
                hass,
                bedroom_area_ids=bedroom_areas,
                quiet_start=config.get("observer_quiet_start", "22:00"),
                quiet_end=config.get("observer_quiet_end", "07:00"),
            )

            if sleeping:
                # Push to phone instead of speaking
                await _push_to_phone(hass, config, briefing_text, reason, request_id=request_id)
            else:
                # Speak via announcement speakers
                tts_entity = resolve_tts_for_context(
                    hass, "briefing",
                    config.get("tts_engine", "auto"),
                    config.get("tts_premium_engine") or None,
                    config.get("tts_premium_contexts") or [],
                )
                broadcast_group = config.get("broadcast_group") or None
                targets, mode = observer_speak_target(
                    hass, urgency="medium", broadcast_group=broadcast_group,
                )
                if tts_entity and targets:
                    await async_announce(
                        hass, briefing_text, tts_entity, targets,
                        context="briefing", action_request_id=request_id,
                    )
                # Also push to phone for record
                await _push_to_phone(hass, config, briefing_text, reason, request_id=request_id)
        else:
            # Everyone away — push only
            await _push_to_phone(hass, config, briefing_text, reason, request_id=request_id)

    except Exception as exc:
        _LOGGER.warning("Proactive briefing failed: %s", exc)


async def _push_to_phone(
    hass: HomeAssistant,
    config: dict,
    message: str,
    reason: str,
    *,
    request_id: Optional[str] = None,
) -> None:
    """Push briefing to every configured normal notification target."""
    from .notify_targets import async_send_configured_notifications

    title = {
        "arrival": "Nova — Welcome Home",
        "security": "Nova — Security Alert",
        "scheduled": "Nova — Briefing",
        "camera": "Nova — Camera Alert",
    }.get(reason, "Nova — Briefing")
    sent = await async_send_configured_notifications(
        hass, config, {"message": message, "title": title},
        request_id=request_id, action="notify", source="proactive",
    )
    if sent:
        _LOGGER.info("Proactive: pushed to %d configured device(s)", len(sent))


# ── Start / Stop ────────────────────────────────────────────────────────────

async def start(hass: HomeAssistant, config: dict) -> None:
    """Start proactive briefing system."""
    if _STATE.running:
        await stop()

    _STATE.hass = hass
    _STATE.config = config
    _STATE.running = True
    _STATE.unsub_presence = hass.bus.async_listen("state_changed", _on_state_changed)
    _LOGGER.info("Nova Proactive Briefing system started")


async def stop() -> None:
    """Stop proactive briefing system."""
    if _STATE.unsub_presence:
        try:
            _STATE.unsub_presence()
        except Exception:
            pass
    _STATE.running = False
    _LOGGER.info("Nova Proactive Briefing system stopped")
