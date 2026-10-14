"""Turning detected patterns into automation suggestions.

Three jobs live here:

* wording: why a suggestion was made (``explain_suggestion``) and how its
  trigger reads;
* building: the Home Assistant automation a pattern would become
  (``generate_automation``), and the check that turns that stored payload
  into something installable (``normalize_suggestion_automation``);
* storing: the ``suggestions`` table in patterns.db (``SuggestionStore``).
  StateLogger owns that table's schema and migrations.

A payload marked ``manual_review`` is advice for a person, never installed.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, cast

from .models import (
    REVIVABLE_STATUSES,
    SUGGESTION_PENDING,
    SUGGESTION_REJECTED,
    SUGGESTION_RETIRED,
    SUGGESTION_SUPERSEDED,
    DetectedPattern,
    loads_json,
)

_LOGGER = logging.getLogger(__name__)

# The longest a presence release automation waits for presence to clear; a
# sensor stuck on (or unavailable) for longer leaves the device alone.
RELEASE_TIMEOUT = "04:00:00"

DB_PATH: Optional[str] = None  # set to override; None means paths.patterns_db()

MIN_DAYS = 7  # days of history needed before any analysis runs


def _db_path() -> str:
    from .. import paths  # root modules are only reached at call time
    return DB_PATH or paths.patterns_db()


def _clock(details: dict) -> str:
    """A time routine's usual time as HH:MM. Rows stored before the minute
    was learned have only an hour, which reads as on the hour."""
    return f"{int(details['hour']):02d}:{int(details.get('minute') or 0):02d}"


def _as_list(value) -> list:
    if isinstance(value, dict):
        return [value]
    return list(value)


def _learned_conditions(value) -> list:
    """The real condition dicts in a pattern's ``condition`` detail, which
    may be one dict, a list of them, or nothing."""
    items = value if isinstance(value, list) else [value]
    return [c for c in items if isinstance(c, dict) and c.get("condition")]


# ── installing a stored payload ─────────────────────────────────────────────

def normalize_suggestion_automation(stored_yaml: str) -> dict:
    """Read a suggestion's stored automation JSON and return either

        {"installable": True, "alias", "trigger": [...], "action": [...],
         optionally "condition": [...] and "mode"}

    or {"installable": False, "reason": ...}. Pure.

    Older payloads use ``platform`` and ``service``; Home Assistant now
    calls these ``trigger`` and ``action``, so both are rewritten. Advice
    (``manual_review``, or a bare note) is refused rather than guessed at."""
    def refused(reason: str) -> dict:
        return {"installable": False, "reason": reason}

    if not stored_yaml:
        return refused("no automation payload")
    try:
        data = json.loads(stored_yaml)
    except Exception:
        return refused("payload is not valid JSON")
    if not isinstance(data, dict):
        return refused("payload is not an object")
    advice_only = data.get("type") == "manual_review" or (
        "note" in data and "trigger" not in data)
    if advice_only:
        return {"installable": False,
                "reason": "advisory only — needs a human to design the automation"}

    alias, trigger, action = data.get("alias"), data.get("trigger"), data.get("action")
    if not (alias and trigger and action):
        return {"installable": False, "reason": "missing alias, trigger, or action"}

    def renamed(item, old: str, new: str):
        if isinstance(item, dict) and old in item and new not in item:
            item = dict(item)
            item[new] = item.pop(old)
        elif isinstance(item, dict):
            item = dict(item)
        return item

    result: dict[str, Any] = {
        "installable": True,
        "alias": alias,
        "trigger": [renamed(t, "platform", "trigger") for t in _as_list(trigger)],
        "action": [renamed(a, "service", "action") for a in _as_list(action)],
    }
    # A learned condition (a time window, say) must reach the installed copy.
    if data.get("condition"):
        result["condition"] = _as_list(data["condition"])
    if data.get("mode") in ("single", "restart", "queued", "parallel"):
        result["mode"] = data["mode"]
    return result


# The on/off domains whose services are simply turn_on and turn_off.
_ON_OFF_DOMAINS = frozenset({"light", "switch", "fan", "input_boolean",
                             "humidifier", "siren"})


def service_for(entity_id: str, state: str) -> Optional[dict]:
    """The service call that puts ``entity_id`` into ``state``, as
    {"service": "domain.service", "entity_id": ...}, or None when there is
    no clean mapping (the caller then offers advice instead).

    Only real services are produced: a lock is locked with lock.lock, never
    lock.turn_on. ``camera_event.*`` is a pattern learning entity that is
    never a real Home Assistant entity, so it can only ever be a trigger."""
    if not entity_id or "." not in entity_id:
        return None
    domain = entity_id.split(".")[0]
    if domain == "camera_event":
        return None
    wanted = str(state).lower().strip()
    if domain in _ON_OFF_DOMAINS and wanted in ("on", "off"):
        return {"service": f"{domain}.turn_{wanted}", "entity_id": entity_id}
    if domain == "lock" and wanted in ("locked", "unlocked"):
        verb = "lock" if wanted == "locked" else "unlock"
        return {"service": f"lock.{verb}", "entity_id": entity_id}
    if domain == "cover" and wanted in ("open", "closed", "opening", "closing"):
        # A moving cover is heading for its end state; aim for that.
        verb = "open" if wanted in ("open", "opening") else "close"
        return {"service": f"cover.{verb}_cover", "entity_id": entity_id}
    if domain == "scene":
        return {"service": "scene.turn_on", "entity_id": entity_id}
    # Climate, media players and the rest need values a bare state does not
    # give, so they get advice rather than a guess.
    return None


# ── explaining a suggestion ─────────────────────────────────────────────────

def _why_time_routine(d: dict, count: int, names: dict, ev: list) -> str:
    when = _clock(d) if d.get("hour") is not None else "a regular time"
    if d.get("state") is not None:
        ev.append(f"Observed turning {d['state']} near {when}")
    observed, opportunity = d.get("observed_days"), d.get("opportunity_days")
    if observed is not None and opportunity:
        # Days it happened out of days it could have: the misses show too.
        line = f"Happened on {int(observed)} of {int(opportunity)} days"
        missed = int(opportunity) - int(observed)
        if missed > 0:
            line += f" (missed {missed})"
        ev.append(line)
    else:
        ev.append(f"Happened {count} times in the last 30 days")
    share = d.get("coverage", d.get("consistency"))
    if share is not None:
        ev.append(f"Consistent on about {int(float(share) * 100)}% of days")
    if d.get("person"):
        cond = d.get("condition")
        owner = cond.get("entity_id") if isinstance(cond, dict) else None
        ev.append(f"Specifically when {(names.get(owner) if owner else None) or d['person']} is home")
    return f"A daily routine around {when}"


def _why_sequence(d: dict, count: int, names: dict, ev: list) -> str:
    def step(value) -> str:
        if not isinstance(value, dict):
            return str(value)
        entity = value.get("entity")
        label = _name_for(entity, names) if entity else "something"
        state = value.get("state")
        return label if state is None else f"{label} → {state}"

    first = d.get("first") or d.get("trigger")
    then = d.get("then") or d.get("action")
    if first and then:
        ev.append(f"After {step(first)}, {step(then)} usually follows")
    ev.append(f"Seen {count} times in 30 days")
    if d.get("window_seconds"):
        ev.append(f"Usually within {int(d['window_seconds'])}s")
    return "One action reliably follows another"


def _why_command(d: dict, count: int, names: dict, ev: list) -> str:
    said = d.get("command") or d.get("text")
    if said:
        ev.append(f"You've asked '{said}' {count} times")
    if d.get("hour") is not None:
        ev.append(f"Most often around {int(d['hour']):02d}:00")
    return "A command you give often"


def _why_temperature(d: dict, count: int, names: dict, ev: list) -> str:
    if d.get("target") is not None:
        ev.append(f"Set to {d['target']}° repeatedly")
    ev.append(f"Observed {count} times")
    return "A temperature preference"


def _why_threshold(d: dict, count: int, names: dict, ev: list) -> str:
    sensor = _name_for(str(d.get("trigger_sensor") or ""), names)
    act: dict = d["action"] if isinstance(d.get("action"), dict) else {}
    target = _name_for(str(act.get("entity") or ""), names)
    op, threshold = str(d.get("op") or ""), d.get("threshold")
    if threshold is not None and op in ("below", "above"):
        moves = "drops below" if op == "below" else "rises above"
        ev.append(f"When {sensor} {moves} {threshold:g}, {target} turns {act.get('state', '')}")
    followed, crossings = d.get("followed"), d.get("crossings")
    if followed is not None and crossings:
        minutes = int(d.get("window_seconds") or 600) // 60
        ev.append(f"Followed within {minutes} minutes on {int(followed)} "
                  f"of {int(crossings)} times it crossed")
    else:
        ev.append(f"Observed {count} times in 30 days")
    if d.get("action_count"):
        ev.append(f"{target} changed this way {int(d['action_count'])} times in 30 days")
    if d.get("distinct_days"):
        ev.append(f"On {int(d['distinct_days'])} different days")
    return "A threshold routine"


def _why_presence(d: dict, count: int, names: dict, ev: list) -> str:
    ev.append(f"Correlated {count} times over 30 days")
    return "A presence-linked pattern"


_WHY = {
    "time_routine": _why_time_routine,
    "sequence": _why_sequence,
    "repeated_command": _why_command,
    "temp_pref": _why_temperature,
    "numeric_trigger": _why_threshold,
    "presence": _why_presence,
}


def _spoken_duration(seconds: int) -> str:
    if seconds and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    return f"{seconds} seconds"


def explain_suggestion(pattern_type: str, details: dict, count: int) -> dict:
    """{"headline", "evidence": [...]}: the observations behind a suggestion,
    for the review screen, so approving one is an informed choice. Pure and
    never raises."""
    d = details or {}
    names: dict = d["names"] if isinstance(d.get("names"), dict) else {}
    ev: list[str] = []
    headline = ""
    try:
        why = _WHY.get(pattern_type)
        if why is None:
            headline = "A learned pattern"
            ev.append(f"Observed {count} times in 30 days")
        else:
            headline = why(d, count, names, ev)
        gate = d.get("presence_gate")
        if isinstance(gate, dict) and gate.get("area_name"):
            ev.append(f"Only when presence is detected in {gate['area_name']}")
        release = d.get("presence_release")
        if isinstance(release, dict) and release.get("area_name"):
            settle = max(0, int(release.get("settle_seconds", 0) or 0))
            ev.append(f"Turns off again once presence in {release['area_name']} "
                      f"has cleared for {_spoken_duration(settle)}")
    except Exception:
        headline = headline or "A learned pattern"
        if not ev:
            ev.append(f"Observed {count} times")
    return {"headline": headline, "evidence": ev}


# ── triggers and their wording ──────────────────────────────────────────────

def _domain(entity: str) -> str:
    return entity.split(".")[0] if "." in entity else ""


def _trigger_for(entity: str, state: str) -> dict:
    """The trigger for a learned sequence's first step.

    A person or tracker arriving or leaving becomes a zone trigger, Home
    Assistant's own way to fire on that. An ``event.*`` entity (a button or
    remote) changes on every press, so it is a bare state trigger and the
    press type is checked by ``_trigger_extra_conditions``. A scene's state
    is a timestamp, so any change is an activation. Anything else waits for
    the learned state."""
    domain = _domain(entity)
    if domain in ("person", "device_tracker") and state in ("home", "not_home"):
        return {"platform": "zone", "entity_id": entity, "zone": "zone.home",
                "event": "enter" if state == "home" else "leave"}
    if domain in ("event", "scene"):
        return {"platform": "state", "entity_id": entity}
    return {"platform": "state", "entity_id": entity, "to": state}


def _trigger_extra_conditions(entity: str, state: str) -> list:
    """Conditions a trigger needs besides the learned ones: for an event
    entity, a template that matches the learned press type."""
    if _domain(entity) != "event":
        return []
    return [{"condition": "template",
             "value_template": "{{ trigger.to_state.attributes.event_type == '%s' }}" % state}]


def _name_for(entity_id: str, names: Optional[dict] = None) -> str:
    """cognitive.naming.name_for, imported late for the package layering."""
    from ..cognitive.naming import name_for
    return name_for(entity_id, names)


def _trigger_phrase(entity: str, state: str, names: Optional[dict] = None) -> str:
    """How a sequence's description opens, naming the trigger entity."""
    domain = _domain(entity)
    label = _name_for(entity, names)
    if domain in ("person", "device_tracker") and state in ("home", "not_home"):
        return f"When {label} {'arrives' if state == 'home' else 'leaves'} home"
    if domain == "event":
        return f"When {label} is pressed ({state})"
    if domain == "scene":
        return f"When {label} is activated"
    return f"When {label} turns {state}"


# What Nova can emit today, against Home Assistant's full lists (keep this up
# to date as coverage grows):
#   triggers   state, time, numeric_state, zone, and event.* presses (as a
#              state trigger plus a template on event_type). Not yet: sun,
#              time_pattern, template, calendar, device and the rest.
#   conditions time and sun windows, numeric_state, state (a person home),
#              template (press type). Not yet: zone, and/or/not, device.
# Each new one is its own piece of work: learn the discriminator from
# history, attach it only when it holds, and keep the dict in Home
# Assistant's own form so the payload passes through normalisation as is.


# ── building the automation ─────────────────────────────────────────────────

def _advice(note: str, *, note_first: bool) -> str:
    """A manual_review payload. The key order matches what each pattern type
    has always stored."""
    body = ({"note": note, "type": "manual_review"} if note_first
            else {"type": "manual_review", "note": note})
    return json.dumps(body, indent=2)


def _duration(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _time_routine_automation(p: DetectedPattern, d: dict, n) -> str:
    target = p.entity_ids[0] if p.entity_ids else None
    # Only something Nova can actually drive becomes an automation; a
    # sensor or tracker would need a service that does not exist.
    call = service_for(target, d["state"]) if target else None
    at = _clock(d)
    if not call:
        return _advice(f"Consider automating: {n(target) if target else '?'}"
                       f" → {d['state']} around {at}", note_first=False)
    auto: dict[str, Any] = {
        "alias": f"Nova Learned: {n(target)} {d['state']} at {at}",
        "trigger": {"platform": "time", "at": f"{at}:00"},
        "action": call,
    }
    conditions = _learned_conditions(d.get("condition"))
    if conditions:
        auto["condition"] = conditions
    return json.dumps(auto, indent=2)


def _release_steps(sensor_id: str, settle: int, call: dict, off_call: dict) -> list:
    """Turn on while presence is detected, then off once it has been clear
    for ``settle`` seconds (waiting at most RELEASE_TIMEOUT)."""
    hold = _duration(settle)
    present = {"condition": "state", "entity_id": sensor_id, "state": "on"}
    clear = {"condition": "state", "entity_id": sensor_id, "state": "off"}
    return [
        # The gate was checked at trigger time; check again after any delay
        # so an early clear cannot leave the waits below armed for nothing.
        present,
        call,
        # Presence cleared while the device was coming on: allow the settling
        # time for it to return before deciding anything.
        {"choose": [{
            "conditions": [clear],
            "sequence": [{
                "wait_for_trigger": [{"trigger": "state", "entity_id": sensor_id, "to": "on"}],
                "timeout": hold,
                "continue_on_timeout": True,
            }],
        }]},
        # Unless it has already been clear for the settling time, wait for
        # that from any state (an unavailable sensor coming back as off
        # counts), for no longer than RELEASE_TIMEOUT.
        {"choose": [{
            "conditions": [{"condition": "not",
                            "conditions": [dict(clear, **{"for": hold})]}],
            "sequence": [{
                "wait_for_trigger": [{"trigger": "state", "entity_id": sensor_id,
                                      "to": "off", "for": hold}],
                "timeout": RELEASE_TIMEOUT,
                "continue_on_timeout": True,
            }],
        }]},
        # Deliberately at the top level: inside a choose a failed condition
        # ends only that branch, so this is what keeps the device on while
        # presence is still, or again, detected.
        clear,
        off_call,
    ]


def _sequence_automation(d: dict, n) -> str:
    first, then = d.get("trigger", {}), d.get("action", {})
    call = service_for(then.get("entity", ""), then.get("state", ""))
    if not call:
        return _advice(f"Consider automating: {n(then.get('entity', '?'))} → "
                       f"{then.get('state', '?')} after {n(first.get('entity', '?'))} "
                       f"{first.get('state', '?')}", note_first=True)
    steps: list = []
    # The measured typical gap, rounded to 5 seconds. Under 15 seconds the
    # action follows straight away rather than after an odd little wait.
    lag = int(d.get("delay_seconds", 60) or 0)
    if lag >= 15:
        lag = int(round(lag / 5.0) * 5)
        steps.append({"delay": f"00:{lag // 60:02d}:{lag % 60:02d}"})

    gate = d.get("presence_gate") if isinstance(d.get("presence_gate"), dict) else None
    release = d.get("presence_release") if isinstance(d.get("presence_release"), dict) else None
    off_call = service_for(then.get("entity", ""), "off")
    releases = bool(release and gate and off_call
                    and release.get("entity_id") == gate.get("entity_id"))
    if releases:
        assert release is not None and off_call is not None
        settle = max(0, min(600, int(release.get("settle_seconds", 0) or 0)))
        steps.extend(_release_steps(str(release["entity_id"]), settle, call, off_call))
    else:
        steps.append(call)

    trigger = _trigger_for(first["entity"], first["state"])
    target, source = n(then["entity"]), n(first["entity"])
    if trigger.get("platform") == "zone":
        verb = "leaves" if trigger["event"] == "leave" else "arrives"
        alias = f"Nova Learned: {target} when {source} {verb} home"
    elif _domain(first["entity"]) == "event":
        alias = f"Nova Learned: {target} on {source} press"
    else:
        alias = f"Nova Learned: {target} after {source}"
    if releases:
        alias += ", off when presence clears"
    elif gate:
        alias += " when presence is detected"

    auto: dict[str, Any] = {"alias": alias, "trigger": trigger, "action": steps}
    conditions = (_learned_conditions(d.get("condition"))
                  + _trigger_extra_conditions(first["entity"], first["state"]))
    if conditions:
        auto["condition"] = conditions
    if releases:
        auto["mode"] = "restart"
    return json.dumps(auto, indent=2)


def _threshold_automation(d: dict, n) -> str:
    then = d.get("action", {})
    call = service_for(then.get("entity", ""), then.get("state", ""))
    if not call:
        return _advice(f"Consider: {n(then.get('entity', '?'))} when "
                       f"{n(d.get('trigger_sensor', '?'))} {d.get('op', '?')} "
                       f"{d.get('threshold', '?')}", note_first=False)
    auto: dict[str, Any] = {
        "alias": (f"Nova Learned: {n(then['entity'])} when "
                  f"{n(d['trigger_sensor'])} {d['op']} {d['threshold']:g}"),
        "trigger": {"platform": "numeric_state", "entity_id": d["trigger_sensor"],
                    d["op"]: d["threshold"]},
        "action": [call],
    }
    conditions = _learned_conditions(d.get("condition"))
    if conditions:
        auto["condition"] = conditions
    if isinstance(d.get("presence_gate"), dict):
        auto["alias"] += " when presence is detected"
    return json.dumps(auto, indent=2)


def _presence_automation(d: dict, n) -> str:
    call = service_for(d.get("action_entity", ""), d.get("action_state", ""))
    if not call:
        return _advice(f"Consider automating: {n(d.get('action_entity', '?'))} → "
                       f"{d.get('action_state', '?')} when "
                       f"{n(d.get('trigger_person', '?'))} {d.get('trigger_state', '?')}",
                       note_first=True)
    return json.dumps({
        "alias": (f"Nova Learned: {n(d['action_entity'])} when "
                  f"{n(d['trigger_person'])} {d['trigger_state']}"),
        "trigger": {"platform": "state", "entity_id": d["trigger_person"],
                    "to": d["trigger_state"]},
        "action": call,
    }, indent=2)


def generate_automation(pattern: DetectedPattern) -> str:
    """The automation (as JSON text) a detected pattern would become, or a
    manual_review note when it cannot be one."""
    d = pattern.details
    names: dict = d["names"] if isinstance(d.get("names"), dict) else {}

    def n(entity_id):
        return _name_for(entity_id, names)

    kind = pattern.pattern_type
    if kind == "time_routine" and d.get("state") is not None and d.get("hour") is not None:
        return _time_routine_automation(pattern, d, n)
    if kind == "sequence":
        return _sequence_automation(d, n)
    if kind == "numeric_trigger":
        return _threshold_automation(d, n)
    if kind == "repeated_command":
        return _advice(f"Consider automating: '{d.get('command', '')}' at "
                       f"{d.get('hour', 0):02d}:00", note_first=True)
    if kind == "presence":
        return _presence_automation(d, n)
    return json.dumps({"note": pattern.description}, indent=2)


# ── identity: when two detections are the same suggestion ──────────────────

def suggestion_identity(pattern_type: str, entity_ids, details) -> Optional[tuple]:
    """What makes a suggestion the same behaviour, or None when its type (or
    an old row with no details) has no identity.

    Only what triggers the behaviour and what it does count. Values that
    drift between analyses (counts, coverage, a sequence's delay, a learned
    window or threshold, the likely owner, a routine's exact minute) do not,
    so the same behaviour measured again is the same suggestion and a
    dismissed one stays dismissed. A different trigger, target, state or
    hour is a different behaviour."""
    d = details if isinstance(details, dict) else {}
    ents = [str(e) for e in (entity_ids or [])]
    try:
        if pattern_type == "time_routine":
            if ents and d.get("hour") is not None and d.get("state") is not None:
                return (pattern_type, ents[0], str(d["state"]), int(d["hour"]))
            return None
        if pattern_type == "repeated_command":
            if d.get("command") and d.get("hour") is not None:
                return (pattern_type, str(d["command"]), int(d["hour"]))
            return None
        if pattern_type == "sequence":
            first, then = d.get("trigger") or {}, d.get("action") or {}
            if not (first.get("entity") and then.get("entity")):
                return None
            key: tuple = (pattern_type, str(first["entity"]), str(first.get("state")),
                   str(then["entity"]), str(then.get("state")))
            if isinstance(d.get("presence_release"), dict):
                key += ("presence_release",)
            return key
        if pattern_type == "numeric_trigger":
            then = d.get("action") or {}
            if not (d.get("trigger_sensor") and then.get("entity")):
                return None
            return (pattern_type, str(d["trigger_sensor"]), str(d.get("op")),
                    str(then["entity"]), str(then.get("state")))
        if pattern_type == "presence":
            if not (d.get("trigger_person") and d.get("action_entity")):
                return None
            return (pattern_type, str(d["trigger_person"]), str(d.get("trigger_state")),
                    str(d["action_entity"]), str(d.get("action_state")))
    except (TypeError, ValueError):
        return None
    return None


def _variant_family(key: Optional[tuple]) -> Optional[tuple]:
    """An identity without its presence release marker. A gated sequence and
    the same one with "off when presence clears" are separate suggestions
    (dismissing one never hides the other) but one behaviour, so only one of
    them is ever pending."""
    if key and key[-1] == "presence_release":
        return key[:-1]
    return key


def _row_identity(pattern_type, entity_ids_json, details_json) -> Optional[tuple]:
    return suggestion_identity(pattern_type, loads_json(entity_ids_json, []),
                               loads_json(details_json, {}))


def _find_existing(conn: sqlite3.Connection, pattern: DetectedPattern):
    """(id, status) of the stored suggestion for this pattern, or None.

    A pending row is preferred, so a refresh lands where the reviewer looks;
    otherwise any decided row (it still means "not new"); a superseded or
    retired row only when nothing else matches."""
    key = suggestion_identity(pattern.pattern_type, pattern.entity_ids, pattern.details)
    texts = [pattern.description]
    if pattern.legacy_description and pattern.legacy_description != pattern.description:
        texts.append(pattern.legacy_description)   # rows worded with entity ids
    found: list[tuple[int, str]] = []
    marks = ", ".join("?" for _ in texts)
    for rid, status, ptype, ents, details in conn.execute(
            "SELECT id, status, pattern_type, entity_ids, details FROM suggestions "
            f"WHERE description IN ({marks})", tuple(texts)).fetchall():
        # Text is only a fallback for rows with no identity; it must never
        # join two variants whose wording happens to match.
        row_key = _row_identity(ptype, ents, details)
        if key is None or row_key is None or row_key == key:
            found.append((int(rid), str(status or "")))
    if key is not None:
        for rid, status, ents, details in conn.execute(
                "SELECT id, status, entity_ids, details FROM suggestions "
                "WHERE pattern_type = ? ORDER BY id", (pattern.pattern_type,)).fetchall():
            if _row_identity(pattern.pattern_type, ents, details) == key:
                found.append((int(rid), str(status or "")))
    if not found:
        return None
    pending = [f for f in found if f[1] == SUGGESTION_PENDING]
    if pending:
        return pending[0]
    live = [f for f in found if f[1] not in (SUGGESTION_SUPERSEDED, SUGGESTION_RETIRED)]
    return live[0] if live else found[0]


def _retire_other_variants(conn: sqlite3.Connection, pattern: DetectedPattern,
                           keep_id: int) -> None:
    """Mark the other pending variants of this behaviour superseded. Only
    pending rows move; every decided row stays exactly as it is."""
    key = suggestion_identity(pattern.pattern_type, pattern.entity_ids, pattern.details)
    family = _variant_family(key)
    if family is None:
        return
    for rid, ents, details in conn.execute(
            "SELECT id, entity_ids, details FROM suggestions "
            "WHERE pattern_type = ? AND status = ? AND id != ?",
            (pattern.pattern_type, SUGGESTION_PENDING, keep_id)).fetchall():
        other = _row_identity(pattern.pattern_type, ents, details)
        if other != key and _variant_family(other) == family:
            conn.execute("UPDATE suggestions SET status = ? WHERE id = ? AND status = ?",
                         (SUGGESTION_SUPERSEDED, rid, SUGGESTION_PENDING))


# ── the store ───────────────────────────────────────────────────────────────

class SuggestionStore:
    """SQL access to the suggestions table. Holds no state: every call opens
    and closes its own connection in the calling thread."""

    def __init__(self, db_path: Optional[str] = None):
        self._db = db_path or _db_path()

    def _connect(self) -> Optional[sqlite3.Connection]:
        try:
            if not Path(self._db).exists():
                return None
            from ..persistence import sqlite as _store  # package layering
            return _store.connect(self._db, mkdir=False)
        except Exception:
            return None

    def store(self, pattern: DetectedPattern, generate=None,
              status: str = SUGGESTION_PENDING) -> bool:
        """Store a pattern as a suggestion; True only when a new row was
        made. ``status`` applies to a new row and is pending, or rejected when
        the AI review turned it down. An existing row keeps its own rules."""
        build = generate or generate_automation
        try:
            from ..persistence import sqlite as _store  # package layering
            conn = _store.connect(self._db, row_factory=False, mkdir=False)
            # One behaviour is one row however its counts drift, so match on
            # identity, never on the description (which carries counts).
            existing = _find_existing(conn, pattern)
            if existing:
                self._refresh(conn, existing, pattern, build)
                conn.commit()
                conn.close()
                return False

            new_status = SUGGESTION_REJECTED if status == SUGGESTION_REJECTED else SUGGESTION_PENDING
            cursor = conn.execute(
                "INSERT INTO suggestions (created, description, automation_yaml, "
                "confidence, pattern_count, pattern_type, entity_ids, details, "
                "status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now().isoformat(), pattern.description, build(pattern),
                 pattern.confidence, pattern.occurrences, pattern.pattern_type,
                 json.dumps(pattern.entity_ids or []), json.dumps(pattern.details or {}),
                 new_status))
            new_id = cast(int, cursor.lastrowid)
            if new_status == SUGGESTION_PENDING:
                _retire_other_variants(conn, pattern, new_id)
            conn.commit()
            conn.close()
            if new_status == SUGGESTION_REJECTED:
                # Nobody was shown it, so it is no "suggestion" decision: the
                # adaptive threshold learns only from people's choices.
                return False
            _record_proposal(pattern, new_id)
            return True
        except Exception as exc:
            _LOGGER.debug("Store suggestion error: %s", exc)
            return False

    @staticmethod
    def _refresh(conn, existing: tuple, pattern: DetectedPattern, build) -> None:
        sid, status = existing
        if status not in REVIVABLE_STATUSES:
            # Decided (dismissed, installed, covered, approved): the decision
            # stands and only the counts move.
            conn.execute("UPDATE suggestions SET confidence = ?, pattern_count = ? WHERE id = ?",
                         (pattern.confidence, pattern.occurrences, sid))
            return
        # Still open, or retired only because another variant came later:
        # bring in the latest evidence and payload, keep any AI review it
        # already had, and make it the one pending variant.
        details = dict(pattern.details or {})
        if "review" not in details:
            row = conn.execute("SELECT details FROM suggestions WHERE id = ?", (sid,)).fetchone()
            before = loads_json(row[0], {}) if row else {}
            if isinstance(before, dict) and isinstance(before.get("review"), dict):
                details["review"] = before["review"]
        conn.execute(
            "UPDATE suggestions SET confidence = ?, pattern_count = ?, description = ?, "
            "details = ?, automation_yaml = ?, status = ? WHERE id = ?",
            (pattern.confidence, pattern.occurrences, pattern.description,
             json.dumps(details), build(pattern), SUGGESTION_PENDING, sid))
        _retire_other_variants(conn, pattern, sid)

    def lookup(self, pattern: DetectedPattern) -> Optional[dict]:
        """{"id", "status", "reviewed"} for this behaviour's stored row, or
        None when it was never stored."""
        conn = self._connect()
        if not conn:
            return None
        try:
            found = _find_existing(conn, pattern)
            if not found:
                return None
            sid, status = found
            row = conn.execute("SELECT details FROM suggestions WHERE id = ?", (sid,)).fetchone()
            details = loads_json(row[0], {}) if row else {}
            reviewed = isinstance(details, dict) and isinstance(details.get("review"), dict)
            return {"id": sid, "status": status, "reviewed": reviewed}
        except Exception:
            return None
        finally:
            conn.close()

    def _set_review(self, suggestion_id: int, allowed, new_status: str, review_of) -> bool:
        conn = self._connect()
        if not conn:
            return False
        try:
            row = conn.execute("SELECT details, status FROM suggestions WHERE id = ?",
                               (suggestion_id,)).fetchone()
            if not row or row["status"] not in allowed:
                return False
            details = loads_json(row["details"], {})
            details = details if isinstance(details, dict) else {}
            details["review"] = review_of(details)
            conn.execute("UPDATE suggestions SET status = ?, details = ? WHERE id = ?",
                         (new_status, json.dumps(details), suggestion_id))
            conn.commit()
        except Exception:
            return False
        finally:
            conn.close()
        return True

    def reject(self, suggestion_id: int, review: dict) -> bool:
        """Mark a still open suggestion rejected by the AI review, keeping the
        review (verdict, reason, model) in its details."""
        return self._set_review(suggestion_id, REVIVABLE_STATUSES, SUGGESTION_REJECTED,
                                lambda _details: dict(review or {}))

    def restore(self, suggestion_id: int) -> bool:
        """Put a suggestion the AI review rejected back in front of a person.
        It is not reviewed again."""
        def overridden(details: dict) -> dict:
            earlier = details.get("review")
            return dict(earlier if isinstance(earlier, dict) else {}, overridden=True)
        return self._set_review(suggestion_id, {SUGGESTION_REJECTED}, SUGGESTION_PENDING,
                                overridden)

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        conn = self._connect()
        if not conn:
            return []
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        except Exception:
            return []
        finally:
            conn.close()

    def rejected(self, limit: int = 20) -> list[dict]:
        """Suggestions the AI review turned down, newest first."""
        return self._rows("SELECT * FROM suggestions WHERE status = ? ORDER BY id DESC LIMIT ?",
                          (SUGGESTION_REJECTED, int(limit)))

    def retire_missing(self, pattern_type: str, detected: list) -> int:
        """Hide the pending suggestions of ``pattern_type`` this pass did not
        detect (they come back as pending when detected again). Returns how
        many were retired."""
        seen = {suggestion_identity(p.pattern_type, p.entity_ids, p.details)
                for p in detected if p.pattern_type == pattern_type}
        conn = self._connect()
        if not conn:
            return 0
        try:
            retired = 0
            for row in conn.execute(
                    "SELECT id, entity_ids, details FROM suggestions "
                    "WHERE pattern_type = ? AND status = ?",
                    (pattern_type, SUGGESTION_PENDING)).fetchall():
                key = _row_identity(pattern_type, row["entity_ids"], row["details"])
                if key is None or key in seen:
                    continue
                conn.execute("UPDATE suggestions SET status = ? WHERE id = ? AND status = ?",
                             (SUGGESTION_RETIRED, row["id"], SUGGESTION_PENDING))
                retired += 1
            conn.commit()
            return retired
        except Exception:
            return 0
        finally:
            conn.close()

    def pending(self) -> list[dict]:
        """The open suggestions, most confident first (at most 20)."""
        return self._rows("SELECT * FROM suggestions WHERE status = 'pending' "
                          "ORDER BY confidence DESC LIMIT 20")

    def get(self, suggestion_id: int) -> Optional[dict]:
        """One suggestion row by id, or None."""
        found = self._rows("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,))
        return found[0] if found else None

    def _update(self, sql: str, args: tuple, after=None) -> bool:
        conn = self._connect()
        if not conn:
            return False
        try:
            conn.execute(sql, args)
            if after is not None:
                after()
            conn.commit()
        except Exception:
            return False
        finally:
            conn.close()
        return True

    def mark_installed(self, suggestion_id: int, automation_id: str) -> None:
        """Record that an approved suggestion became a live automation."""
        def follow_up():
            # Installed means it was a useful proposal (the decision record).
            with _quietly():
                from .. import decision_record
                decision_record.set_outcome_by_ref("suggestion:%d" % suggestion_id,
                                                   "good", "installed")
            # Whether it then actually runs is a separate question, tracked by
            # an automation trial.
            with _quietly():
                from . import trials
                trials.create(suggestion_id, automation_id)
        self._update("UPDATE suggestions SET status = 'installed', approved_at = ? WHERE id = ?",
                     (datetime.now().isoformat(), suggestion_id), follow_up)

    def mark_covered(self, suggestion_id: int) -> None:
        """Retire a suggestion Home Assistant now has an equal automation for."""
        self._update("UPDATE suggestions SET status = 'already_automated' WHERE id = ?",
                     (suggestion_id,))

    def approve(self, suggestion_id: int) -> bool:
        """Mark a suggestion approved."""
        return self._update("UPDATE suggestions SET status = 'approved', approved_at = ? WHERE id = ?",
                            (datetime.now().isoformat(), suggestion_id))

    def dismiss(self, suggestion_id: int) -> bool:
        """Mark a suggestion dismissed, and record it as unnecessary."""
        def follow_up():
            with _quietly():
                from .. import decision_record
                decision_record.set_outcome_by_ref("suggestion:%d" % suggestion_id,
                                                   "unnecessary", "dismiss_suggestion")
        return self._update("UPDATE suggestions SET status = 'dismissed', dismissed_at = ? WHERE id = ?",
                            (datetime.now().isoformat(), suggestion_id), follow_up)

    def stats(self) -> dict:
        """Counts for the panel, and whether there is enough history yet."""
        conn = self._connect()
        if not conn:
            return {"available": False}
        try:
            def count(sql: str) -> int:
                return conn.execute(sql).fetchone()[0]
            stats: dict[str, Any] = {
                "available": True,
                "state_changes": count("SELECT COUNT(*) FROM state_changes"),
                "commands": count("SELECT COUNT(*) FROM commands"),
                "pending_suggestions": count("SELECT COUNT(*) FROM suggestions WHERE status='pending'"),
                "approved": count("SELECT COUNT(*) FROM suggestions WHERE status='approved'"),
                "dismissed": count("SELECT COUNT(*) FROM suggestions WHERE status='dismissed'"),
            }
            first = conn.execute("SELECT MIN(timestamp) FROM state_changes").fetchone()[0]
            days = (datetime.now() - datetime.fromisoformat(first)).days if first else 0
            stats["days_of_data"] = days
            stats["ready_for_analysis"] = bool(first) and days >= MIN_DAYS
            return stats
        except Exception:
            return {"available": False}
        finally:
            conn.close()


class _quietly:
    """``with _quietly():`` swallows any error from optional follow ups."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def _record_proposal(pattern: DetectedPattern, suggestion_id: int) -> None:
    with _quietly():
        from .. import decision_record
        seen = {"pattern_type": pattern.pattern_type,
                "entities": pattern.entity_ids or [],
                "occurrences": pattern.occurrences}
        decision_record.record(
            "suggestion", observation=seen,
            interpretation={"suggested": pattern.description},
            decision="propose automation", reason="recurring observed behavior",
            confidence=pattern.confidence, ref="suggestion:%d" % suggestion_id)
