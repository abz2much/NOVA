"""Briefings Nova starts on its own.

Three things start one:

* someone comes home: presence alone only arms a welcome, which is spoken
  once the configured front door opens (no door set, no welcome at all);
* several doors, windows or locks open while the house is empty;
* the scheduled briefing, which arrives here with reason "scheduled".

Camera analyses are kept in a small ring so a briefing can say what the
cameras saw without looking at any image again. The ring is saved to
camera_snapshots.json in Nova's data folder, so it survives a restart.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

from homeassistant.core import Event, HomeAssistant, callback

from .locale_format import format_date

_LOGGER = logging.getLogger(__name__)

BRIEFING_COOLDOWN = 30        # minutes between any two proactive briefings
ARRIVAL_COOLDOWN = 60         # minutes, so presence flapping never re-welcomes
SECURITY_THRESHOLD = 3        # openings within the window below
_SECURITY_WINDOW_S = 30 * 60
ARRIVAL_DOOR_WINDOW_S = 600   # how long an arrival waits for the door
# On arrival only the last half hour is news: a caller from this morning is not.
ARRIVAL_RECENT_HOURS = 0.5
_OTHER_RECENT_HOURS = 4

# A state either side of these is no evidence that anything moved.
_NO_READING = ("unavailable", "unknown")
_OPENING_CLASSES = ("door", "window", "garage_door")


# ── what the cameras saw ────────────────────────────────────────────────────

@dataclass
class CameraSnapshot:
    """One analysed camera detection, kept for later briefings."""
    timestamp: float
    camera_name: str
    camera_entity: str
    analysis: str          # what the model said about the frame
    detection_type: str    # person, vehicle, motion or doorbell
    urgency: str = "low"   # low, medium or high


MAX_SNAPSHOTS = 20
SNAPSHOTS_FILE = "camera_snapshots.json"
_SNAPSHOTS: deque[CameraSnapshot] = deque(maxlen=MAX_SNAPSHOTS)

# Writes run in the executor and may finish out of order: a write older
# than the last one saved is skipped.
_WRITE_LOCK = threading.Lock()
_WRITES = {"queued": 0, "saved": 0}


def _snapshots_path() -> str:
    from . import paths
    return paths.nova_path(SNAPSHOTS_FILE)


def _write_snapshots(seq: int, data: list[dict]) -> None:
    from .persistence.files import write_json_atomic
    with _WRITE_LOCK:
        if seq < _WRITES["saved"]:
            return
        write_json_atomic(_snapshots_path(), data, indent=2)
        _WRITES["saved"] = seq


def _save_snapshots() -> None:
    """Save the ring in the executor. Does nothing when Nova's entities
    are not set up yet."""
    from . import nova_state
    hass = nova_state._HASS["hass"]
    if hass is None:
        return
    _WRITES["queued"] += 1
    data = [asdict(snap) for snap in _SNAPSHOTS]
    try:
        task = hass.async_add_executor_job(_write_snapshots, _WRITES["queued"], data)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Proactive: could not save camera snapshots: %s", exc)
        return

    def _done(fut) -> None:
        if not fut.cancelled() and fut.exception() is not None:
            _LOGGER.warning("Proactive: could not save camera snapshots: %s",
                            fut.exception())

    try:
        task.add_done_callback(_done)
    except Exception:  # noqa: BLE001
        pass


def _snapshot_from(item) -> Optional[CameraSnapshot]:
    """One saved entry, or None when a field is missing or the wrong type."""
    if not isinstance(item, dict):
        return None
    stamp = item.get("timestamp")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    texts = ("camera_name", "camera_entity", "analysis", "detection_type", "urgency")
    if not all(isinstance(item.get(key), str) for key in texts):
        return None
    return CameraSnapshot(float(stamp), *(item[key] for key in texts))


def load_snapshots() -> None:
    """Load the saved camera snapshots, merged with any recorded since
    start, newest MAX_SNAPSHOTS kept. A missing or broken file is ignored.
    Blocking: run in the executor."""
    try:
        from .persistence.files import OK, read_json
        read = read_json(_snapshots_path())
        if read.status != OK or not isinstance(read.value, list):
            return
        saved = [snap for snap in map(_snapshot_from, read.value) if snap]
        if not saved:
            return
        seen = {(s.timestamp, s.camera_entity, s.analysis) for s in _SNAPSHOTS}
        merged = [s for s in saved
                  if (s.timestamp, s.camera_entity, s.analysis) not in seen]
        merged += list(_SNAPSHOTS)
        merged.sort(key=lambda s: s.timestamp)
        _SNAPSHOTS.clear()
        _SNAPSHOTS.extend(merged[-MAX_SNAPSHOTS:])
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Proactive: saved camera snapshots not loaded: %s", exc)
        return
    from . import nova_state
    nova_state.notify()


def record_snapshot(camera_name: str, camera_entity: str, analysis: str,
                    detection_type: str = "motion", urgency: str = "low") -> None:
    """Keep one analysis from camera.py for the next briefing, and save it."""
    snap = CameraSnapshot(time.time(), camera_name, camera_entity, analysis,
                          detection_type, urgency)
    _SNAPSHOTS.append(snap)
    _LOGGER.debug("Proactive: recorded snapshot from %s (%s, %s)",
                  camera_name, detection_type, urgency)
    _save_snapshots()
    from . import nova_state
    nova_state.notify()


def get_recent_snapshots(hours: float = 12) -> list[CameraSnapshot]:
    """The kept analyses newer than ``hours`` ago, oldest first."""
    since = time.time() - hours * 3600
    return [snap for snap in _SNAPSHOTS if snap.timestamp > since]


def get_snapshot_summary(hours: float = 12) -> str:
    """One line per camera: how many detections of each kind, and the newest
    analysis with the time it happened. Empty when nothing was seen."""
    per_camera: dict[str, list[CameraSnapshot]] = {}
    for snap in get_recent_snapshots(hours):
        per_camera.setdefault(snap.camera_name, []).append(snap)
    if not per_camera:
        return ""
    rows = []
    for camera, snaps in per_camera.items():
        kinds: dict[str, int] = {}
        for snap in snaps:
            kinds[snap.detection_type] = kinds.get(snap.detection_type, 0) + 1
        mix = ", ".join(f"{n} {kind}" for kind, n in kinds.items())
        newest = snaps[-1]
        # Stamped with its time, so the briefing reports it as past.
        clock = datetime.fromtimestamp(newest.timestamp).strftime("%H:%M")
        rows.append(f"  {camera}: {len(snaps)} detection(s) ({mix}). "
                    f"Latest, at {clock}: {newest.analysis[:120]}")
    return "Camera detections:\n" + "\n".join(rows)


# ── listener state ──────────────────────────────────────────────────────────

@dataclass
class _ProactiveState:
    running: bool = False
    hass: Optional[HomeAssistant] = None
    config: dict = field(default_factory=dict)
    last_briefing_time: float = 0.0
    last_arrival_briefing: float = 0.0
    security_events: list = field(default_factory=list)   # opening times
    unsub_presence: Optional[object] = None
    unsub_timer: Optional[object] = None
    # Who presence says has come home, waiting for the front door to open.
    pending_arrival_person: str = ""
    pending_arrival_entity: str = ""
    pending_arrival_ts: float = 0.0


_STATE = _ProactiveState()


def _house_maybe_occupied(hass) -> bool:
    """Only a confident "everyone is away" from household.py says no."""
    try:
        from . import household
        return household.residents(hass) != household.AWAY
    except Exception:
        return True


def _configured_front_door() -> str:
    """The door sensor that releases a welcome, or "" when none is set
    (Settings, General, Arrival)."""
    try:
        from . import nova_config
        return str(nova_config.get("arrival_front_door_entity", "") or "")
    except Exception:
        return ""


def _arm_arrival(entity_id: str, new_state, door: str) -> None:
    name = new_state.attributes.get("friendly_name",
                                    entity_id.split(".")[-1].title())
    _STATE.pending_arrival_person = name
    _STATE.pending_arrival_entity = entity_id
    _STATE.pending_arrival_ts = time.time()
    _LOGGER.info("Proactive: %s is home (presence), waiting for %s to open "
                 "before announcing", name, door)


def _release_arrival() -> None:
    """The front door opened while a welcome was waiting. A welcome armed
    over ten minutes ago is dropped without a word: too late to be one."""
    person = _STATE.pending_arrival_person
    entity = _STATE.pending_arrival_entity
    waited = time.time() - _STATE.pending_arrival_ts
    _STATE.pending_arrival_person = ""
    _STATE.pending_arrival_entity = ""
    if waited > ARRIVAL_DOOR_WINDOW_S:
        return
    now = time.time()
    if now - _STATE.last_arrival_briefing <= ARRIVAL_COOLDOWN * 60:
        return
    _STATE.last_arrival_briefing = now
    _LOGGER.info("Proactive: %s opened the front door, triggering welcome "
                 "briefing", person)
    _STATE.hass.async_create_task(
        _trigger_briefing("arrival", person_name=person, person_entity=entity))


def _is_opening(entity_id: str, old_state, new_state) -> bool:
    """A door, window or garage door opening, or a lock unlocking, seen as a
    real change. An update that keeps the same state (attributes only), or a
    sensor coming back from unavailable or a restart, is not one."""
    if old_state is None or old_state.state in _NO_READING:
        return False
    if old_state.state == new_state.state:
        return False
    domain = entity_id.split(".", 1)[0]
    if domain == "lock":
        return new_state.state == "unlocked"
    if domain == "binary_sensor":
        return (new_state.state == "on"
                and new_state.attributes.get("device_class", "") in _OPENING_CLASSES)
    return False


def _count_opening() -> None:
    """Note an opening in an empty house; enough of them close together
    start a security briefing."""
    now = time.time()
    _STATE.security_events = [t for t in _STATE.security_events
                              if now - t < _SECURITY_WINDOW_S] + [now]
    if len(_STATE.security_events) < SECURITY_THRESHOLD:
        return
    if now - _STATE.last_briefing_time <= BRIEFING_COOLDOWN * 60:
        return
    _LOGGER.info("Proactive: %d security events in 30min (house empty), "
                 "triggering security briefing", len(_STATE.security_events))
    _STATE.hass.async_create_task(_trigger_briefing("security"))
    _STATE.security_events.clear()


@callback
def _on_state_changed(event: Event) -> None:
    if not _STATE.running:
        return
    entity_id = event.data.get("entity_id", "")
    new_state = event.data.get("new_state")
    if not new_state:
        return
    old_state = event.data.get("old_state")

    if entity_id.startswith("person."):
        was_home = old_state is not None and old_state.state == "home"
        if new_state.state == "home" and not was_home:
            # Presence turns home in the driveway; the welcome waits for the
            # door, and with no door set there is no welcome.
            door = _configured_front_door()
            if door:
                _arm_arrival(entity_id, new_state, door)
        return

    door = _configured_front_door()
    if (door and entity_id == door and new_state.state == "on"
            and _STATE.pending_arrival_person):
        _release_arrival()

    # Openings matter only in an empty house; at home they are daily life.
    if _is_opening(entity_id, old_state, new_state) \
            and not _house_maybe_occupied(_STATE.hass):
        _count_opening()


# ── device action guard ─────────────────────────────────────────────────────
# The briefing model has no tools, yet it can still write "I've nudged the
# thermostat up" or "Shall I lock the door?". Neither can be true or followed
# up here, so any such sentence about a device throws the whole reply away
# for a plain one built from checked facts. Nothing is edited sentence by
# sentence: half an edited model reply can still mislead. What Nova only saw
# (checked, noticed, confirmed) is never treated as an action.

_ACTION_VERBS = "|".join((
    r"turn(?:ed|ing)?", r"switch(?:ed|ing)?", r"set(?:ting)?",
    r"adjust(?:ed|ing)?", r"nudg(?:ed|e|ing)", r"lock(?:ed|ing)?",
    r"unlock(?:ed|ing)?", r"open(?:ed|ing)?", r"clos(?:ed|e|ing)",
    r"start(?:ed|ing)?", r"stopp?(?:ed|ing)?", r"activat(?:ed|e|ing)",
    r"deactivat(?:ed|e|ing)", r"dimm?(?:ed|ing)?", r"rais(?:ed|e|ing)",
    r"lower(?:ed|ing)?", r"arm(?:ed|ing)?", r"disarm(?:ed|ing)?",
    r"paus(?:ed|e|ing)", r"resum(?:ed|e|ing)", r"play(?:ed|ing)?",
    r"runn?(?:ing)?", r"ran", r"launch(?:ed|ing)?",
))
_APOSTROPHE = r"['’]"
# "I've turned", "I will lock", "I'm going to raise", and also questions such
# as "Shall I turn off", which contain a bare "I turn".
_FIRST_PERSON_ACTION_CLAIM = re.compile(
    rf"\bI(?:{_APOSTROPHE}ve|\s+have|\s+just|\s+already|{_APOSTROPHE}ll"
    rf"|\s+will|{_APOSTROPHE}m|\s+am(?:\s+going\s+to)?)?\s+(?:{_ACTION_VERBS})\b",
    re.IGNORECASE)
# "Would you like me to raise", "Do you want me to lock".
_OFFER_ACTION_PATTERN = re.compile(
    rf"\b(?:would you like (?:me )?to|do you want me to|want me to)"
    rf"\s+(?:{_ACTION_VERBS})\b",
    re.IGNORECASE)
_DEVICE_NOUN = re.compile(
    r"\b(light|lamp|lock|door|thermostat|climate|heat(?:ing)?|cover|blind|"
    r"curtain|shade|garage|media\s*player|speaker|tv|television|scene|"
    r"script|fan|switch|outlet|plug|alarm|siren|camera)s?\b",
    re.IGNORECASE)


def _split_sentences(text: str) -> list[str]:
    """Rough sentence split, only to keep each match inside one sentence."""
    return re.split(r"(?<=[.!?])\s+", text.strip())


def contains_unsupported_device_action_claim(text: str) -> bool:
    """True when a sentence about a device says Nova did, is doing or will
    do something to it, or offers to. Plain state ("the thermostat is at
    18") and offers that are not about devices ("shall I read the
    calendar?") pass."""
    for sentence in _split_sentences(text):
        if not sentence.strip() or not _DEVICE_NOUN.search(sentence):
            continue
        if (_FIRST_PERSON_ACTION_CLAIM.search(sentence)
                or _OFFER_ACTION_PATTERN.search(sentence)):
            return True
    return False


def _deterministic_fallback_briefing(
    *, honorific: str, greeting: str, reason: str, weather: str = "",
    open_things: list[str] | None = None, events: list[str] | None = None,
) -> str:
    """The briefing said with no model at all: the greeting, then the
    weather, open things and recent events already gathered. It names no
    device action of any kind."""
    if reason == "arrival":
        opening = f"Welcome home, {honorific}." if honorific else "Welcome home."
    else:
        opening = f"{greeting}, {honorific}." if honorific else f"{greeting}."
    said = [opening]
    if weather:
        said.append(f"{weather}.")
    if open_things:
        said.append(f"Open/unlocked: {', '.join(open_things)}.")
    if events:
        said.append(f"Recent events: {'; '.join(events[:3])}.")
    return " ".join(said)


# ── building and delivering one briefing ────────────────────────────────────

_NO_ACTIONS_RULE = (
    "This message has NO device-control tools — you cannot turn anything on "
    "or off, lock or unlock anything, or adjust any device right now, and "
    "nothing you say here can be followed up on or acted on later. Do not "
    "claim, promise, or imply that you changed or will change a device "
    "(never say things like \"I've turned up the heat\" or \"I'll lock the "
    "door\"). Do not ask whether the user wants you to change a device "
    "either (never say things like \"Would you like me to raise the "
    "thermostat?\" or \"Shall I turn off the lights?\") — if they want a "
    "device changed, they can ask you directly by voice or chat afterward. "
    "Questions unrelated to device control — for example offering to read "
    "out the calendar — are fine. "
)

_PUSH_TITLES = {
    "arrival": "Nova — Welcome Home",
    "security": "Nova — Security Alert",
    "scheduled": "Nova — Briefing",
    "camera": "Nova — Camera Alert",
}


def _push_title(reason: str) -> str:
    return _PUSH_TITLES.get(reason, "Nova — Briefing")


def _honorific_for(hass, reason: str, person_entity: str, config: dict) -> str:
    try:
        from . import honorific as honorific_mod
        if reason == "arrival" and person_entity:
            # Said to whoever walked in, even with others at home.
            return honorific_mod.arrival_honorific(hass, person_entity)
        return honorific_mod.effective_honorific(hass)
    except Exception:
        return config.get("honorific", "sir")


def _reason_note(reason: str, honorific: str, person_name: str) -> str:
    if reason == "arrival":
        if not honorific:
            # Nobody in particular is addressed, so who came in is the news.
            return f"{person_name} just arrived home. This is a welcome briefing."
        # An honorific means one person is home: the one who just came in.
        # Naming them as well gave "Good afternoon, sir. Alex has just
        # arrived home."
        return ("This welcome-home briefing is addressed directly to the "
                "person who just walked in. Do not restate their name or the "
                "fact that they arrived — the greeting already covers that.")
    if reason == "security":
        return ("Multiple security events detected in a short period. "
                "Summarize what happened and any concerns.")
    if reason == "scheduled":
        return "This is a scheduled briefing."
    return ""


def _task_text(reason: str, honorific: str, greeting: str) -> str:
    from .const import BRIEFING_GROUNDING_RULE
    if reason == "arrival":
        first = f"Welcome home, {honorific}." if honorific else "Welcome home."
    else:
        first = f"{greeting}, {honorific}." if honorific else f"{greeting}."
    audience = f"to {honorific}" if honorific else "to the household"
    return (f"You are delivering a proactive briefing ({reason}) {audience}. "
            f"Begin with '{first}' Cover only the important items. Under 100 "
            "words. Be direct. Anything listed with a time already happened: "
            "say so in the past tense with its time, never as if it is "
            f"happening now. {_NO_ACTIONS_RULE}{BRIEFING_GROUNDING_RULE}")


async def _generate(hass, config: dict, system: str, context: str) -> str:
    """One tool free completion on the reasoning tier, or on the primary
    provider when that tier cannot be built. The entry's ProviderManager
    owns the client, or a short lived one closes it."""
    from contextlib import AsyncExitStack
    from . import llm_provider
    from .providers.activity import execute_chat
    from .providers.manager import provider_scope
    from .providers.routing import ProviderSpec, tier_spec

    async with AsyncExitStack() as stack:
        providers = await stack.enter_async_context(provider_scope(hass))
        try:
            lease = providers.lease(
                tier_spec(config, "reasoning"),
                factory=lambda: llm_provider.create_tier_provider(config, "reasoning"))
            provider = await stack.enter_async_context(lease)
        except Exception:
            name = config.get("llm_provider", "groq")
            spec = ProviderSpec(
                provider=name,
                model=config.get("model", "openai/gpt-oss-120b"),
                api_key=llm_provider.resolve_provider_credential(config, name),
                base_url=llm_provider.resolve_provider_endpoint(config, name),
            )
            provider = await stack.enter_async_context(providers.lease(
                spec, factory=lambda: llm_provider.create_provider(
                    spec.provider, spec.api_key, spec.model, spec.base_url)))
        reply = await execute_chat(
            hass, provider,
            [{"role": "system", "content": system},
             {"role": "user", "content": context}],
            role="briefing", data_category="text",
            tools=None, max_tokens=300, temperature=0.2)
    return (reply.text or "").strip()


async def _trigger_briefing(reason: str, person_name: str = "",
                            person_entity: str = "") -> None:
    """Write one briefing and hand it to delivery."""
    hass, config = _STATE.hass, _STATE.config
    now = time.time()
    if now - _STATE.last_briefing_time < BRIEFING_COOLDOWN * 60:
        _LOGGER.debug("Proactive: briefing cooldown active, skipping")
        return
    _STATE.last_briefing_time = now

    honorific = _honorific_for(hass, reason, person_entity, config)
    hours = ARRIVAL_RECENT_HOURS if reason == "arrival" else _OTHER_RECENT_HOURS
    note = _reason_note(reason, honorific, person_name)
    cameras = get_snapshot_summary(hours=hours)

    try:
        from .prompt_fence import fence_home_data
        from .briefing import (
            _gather_calendar, _gather_energy_anomalies, _gather_open_things,
            _gather_overnight_events, _gather_weather, _time_greeting,
            ungrounded_briefing_fact,
        )
        from .directive_helper import build_system_prompt
        from .tts_helper import resolve_tts_for_context
        from . import output_language

        local_now = datetime.now()
        facts = [f"It is {format_date(local_now, hass)}, "
                 f"{local_now.strftime('%-I:%M %p')}."]
        weather = _gather_weather(hass)
        if weather:
            facts.append(f"Weather: {weather}.")
        open_things = _gather_open_things(hass)
        if reason == "arrival":
            # The door they came through is the one that started this; open
            # locks are still worth saying.
            open_things = [t for t in open_things if not t.endswith("is open")]
        if open_things:
            facts.append(f"Open/unlocked: {', '.join(open_things)}.")
        events = _gather_overnight_events(hass, hours)
        if events:
            facts.append(f"Recent events: {'; '.join(events[:5])}.")
        if cameras:
            facts.append(cameras)
        # The facts hold device names and camera text, so they are fenced.
        # The reason note is Nova's own instruction and stays outside.
        plain = "\n".join(facts + ([note] if note else []))
        context = fence_home_data("\n".join(facts))
        if note:
            context = f"{note}\n\n{context}"
        greeting = _time_greeting()

        task = _task_text(reason, honorific, greeting)
        system = build_system_prompt(hass, honorific, task)
        # Spoken and pushed, so in Nova's output language.
        system = output_language.with_language(hass, system)

        text = await _generate(hass, config, system, context)
        if not text:
            return

        if contains_unsupported_device_action_claim(text):
            _LOGGER.warning(
                "Proactive briefing (%s) rejected — unsupported device-action "
                "claim in generated text; using deterministic fallback: %s",
                reason, text[:200])
            problem = True
        else:
            bad = ungrounded_briefing_fact(text, plain)
            if bad:
                _LOGGER.warning(
                    "Proactive briefing (%s) rejected — %s is not in the "
                    "gathered facts; using deterministic fallback: %s",
                    reason, bad, text[:200])
            problem = bool(bad)
        if problem:
            text = _deterministic_fallback_briefing(
                honorific=honorific, greeting=greeting, reason=reason,
                weather=weather, open_things=open_things, events=events)
            if not text:
                return

        _LOGGER.info("Proactive briefing (%s): %s", reason, text[:100])

        # Delivery applies the household's rules. Every briefing goes to the
        # phones as a record and is spoken when someone is home and the rules
        # allow it. The scheduled one was asked for at its time, so shush,
        # the Announcements switch and quiet hours let it through (sleep
        # still holds it); arrival and security briefings follow every rule.
        from . import delivery
        tts_entity = resolve_tts_for_context(
            hass, "briefing", config.get("tts_engine", "auto"),
            config.get("tts_premium_engine") or None,
            config.get("tts_premium_contexts") or [])
        scheduled = reason == "scheduled"
        await delivery.async_deliver(hass, delivery.DeliveryRequest(
            source="briefing", message=text, category=f"briefing_{reason}",
            requested_by_user=scheduled, quiet_ok=scheduled,
            push=delivery.PUSH_ALWAYS, title=_push_title(reason),
            tts_context="briefing", tts_entity=tts_entity or "", config=config))
    except Exception as exc:
        _LOGGER.warning("Proactive briefing failed: %s", exc)


# ── lifecycle ───────────────────────────────────────────────────────────────

async def start(hass: HomeAssistant, config: dict) -> None:
    """Begin listening; a second start replaces the first listener."""
    if _STATE.running:
        await stop()
    _STATE.hass, _STATE.config, _STATE.running = hass, config, True
    _STATE.unsub_presence = hass.bus.async_listen("state_changed", _on_state_changed)
    _LOGGER.info("Nova Proactive Briefing system started")


async def stop() -> None:
    """Stop listening."""
    unsub = _STATE.unsub_presence
    if unsub:
        try:
            unsub()
        except Exception:
            pass
    _STATE.running = False
    _LOGGER.info("Nova Proactive Briefing system stopped")
