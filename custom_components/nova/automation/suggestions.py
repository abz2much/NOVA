"""Nova automation suggestions: generation, explanation and storage.

A detected pattern becomes a stored suggestion (the ``suggestions`` table in
patterns.db, whose schema and migrations StateLogger owns), an explanation
for the review UI, and an installable Home Assistant automation payload when
the pattern maps onto a concrete service call. Advisory payloads
(``manual_review``) are never installable.
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

# Longest a presence-release automation waits for presence to clear. A sensor
# stuck on or unavailable past this leaves the device as it is.
RELEASE_TIMEOUT = "04:00:00"

DB_PATH = "/config/nova/patterns.db"
MIN_DAYS = 7           # Don't analyze until we have this much data


def normalize_suggestion_automation(stored_yaml: str) -> dict:
    """
    Pure: turn a suggestion's stored automation JSON into structured args for
    installation.create_automation, or explain why it can't (v6.52.0).

    Closes the pattern-engine loop: the analyzer generates these blobs, the
    user approves, and this converts the blob into an installable automation.
    Handles the legacy trigger/action shape the generator emits — HA modernized
    'platform'→'trigger' and 'service'→'action', so we translate both — and
    refuses the non-actionable 'manual_review' markers honestly instead of
    fabricating an automation from a vague note.

    Returns either:
        {"installable": True, "alias", "trigger": [...], "action": [...]}
        {"installable": False, "reason": "..."}
    """
    if not stored_yaml:
        return {"installable": False, "reason": "no automation payload"}
    try:
        data = json.loads(stored_yaml)
    except Exception:
        return {"installable": False, "reason": "payload is not valid JSON"}

    if not isinstance(data, dict):
        return {"installable": False, "reason": "payload is not an object"}
    if data.get("type") == "manual_review" or "note" in data and "trigger" not in data:
        return {"installable": False,
                "reason": "advisory only — needs a human to design the automation"}

    alias = data.get("alias")
    trigger = data.get("trigger")
    action = data.get("action")
    if not alias or not trigger or not action:
        return {"installable": False, "reason": "missing alias, trigger, or action"}

    def _modernize_trigger(t: dict) -> dict:
        t = dict(t)
        if "platform" in t and "trigger" not in t:
            t["trigger"] = t.pop("platform")
        return t

    def _modernize_action(a: dict) -> dict:
        a = dict(a)
        if "service" in a and "action" not in a:
            a["action"] = a.pop("service")
        return a

    triggers = [trigger] if isinstance(trigger, dict) else list(trigger)
    actions = [action] if isinstance(action, dict) else list(action)
    triggers = [_modernize_trigger(t) if isinstance(t, dict) else t for t in triggers]
    # action items can be delays or service calls; only modernize the dicts
    norm_actions = []
    for a in actions:
        if isinstance(a, dict):
            norm_actions.append(_modernize_action(a))
        else:
            norm_actions.append(a)

    out = {"installable": True, "alias": alias,
           "trigger": triggers, "action": norm_actions}
    # Preserve a learned "And if" condition (e.g. a time window) so it reaches
    # the installed automation.
    cond = data.get("condition")
    if cond:
        out["condition"] = [cond] if isinstance(cond, dict) else list(cond)
    mode = data.get("mode")
    if mode in ("single", "restart", "queued", "parallel"):
        out["mode"] = mode
    return out


def service_for(entity_id: str, state: str) -> Optional[dict]:
    """
    Map an entity + desired state to the correct HA service call (v6.52.1).
    The pattern generator used to build every action as `{domain}.turn_{state}`,
    which is only valid for on/off domains — it would emit `lock.turn_on` for a
    learned door-lock routine (the module's own flagship example) and write a
    broken automation. Now each domain gets its real service; anything without a
    clean mapping returns None so the caller can mark it advisory instead of
    installing garbage.

    Returns {"service": "domain.service", "entity_id": ...} or None.
    """
    if not entity_id or "." not in entity_id:
        return None
    domain = entity_id.split(".")[0]
    # Phase 4 (v7.109.0): camera_event.* is a synthetic, non-actuating
    # pattern-learning entity (camera_semantic.py) that was never registered
    # as a real Home Assistant entity — it can only ever be a TRIGGER, never
    # an action target. It was never in the mapped-domain list below either,
    # so this is a defensive, explicit statement of that boundary rather
    # than a behaviour change.
    if domain == "camera_event":
        return None
    s = str(state).lower().strip()

    onoff = {"light", "switch", "fan", "input_boolean", "humidifier", "siren"}
    if domain in onoff and s in ("on", "off"):
        return {"service": f"{domain}.turn_{s}", "entity_id": entity_id}

    if domain == "lock" and s in ("locked", "unlocked"):
        return {"service": f"lock.{'lock' if s == 'locked' else 'unlock'}",
                "entity_id": entity_id}

    if domain == "cover" and s in ("open", "closed", "opening", "closing"):
        # settle transient states to the intended end state
        want_open = s in ("open", "opening")
        return {"service": f"cover.{'open' if want_open else 'close'}_cover",
                "entity_id": entity_id}

    if domain in ("switch", "input_boolean") and s in ("on", "off"):
        return {"service": f"{domain}.turn_{s}", "entity_id": entity_id}

    # a scene target is always activated via scene.turn_on
    if domain == "scene":
        return {"service": "scene.turn_on", "entity_id": entity_id}

    # climate, media_player, and everything else need parameters we don't infer
    # from a bare state — better to advise than to guess.
    return None


def explain_suggestion(pattern_type: str, details: dict, count: int) -> dict:
    """Turn a suggestion's evidence into a human 'why' for the review UI
    (v6.80.0). Returns {headline, evidence:[...]} — the observations that led to
    the proposal, so approving is an informed choice rather than a leap. Pure,
    never raises."""
    d = details or {}
    ev: list[str] = []
    headline = ""
    raw_names = d.get("names")
    names: dict = raw_names if isinstance(raw_names, dict) else {}
    try:
        if pattern_type == "time_routine":
            hour = d.get("hour")
            state = d.get("state")
            consistency = d.get("coverage", d.get("consistency"))
            observed = d.get("observed_days")
            opportunity = d.get("opportunity_days")
            person = d.get("person")
            when = f"{int(hour):02d}:00" if hour is not None else "a regular time"
            headline = f"A daily routine around {when}"
            if state is not None:
                ev.append(f"Observed turning {state} near {when}")
            # Prefer the honest coverage framing — how many days it happened out
            # of how many it could have, so the negative evidence is visible too.
            if observed is not None and opportunity:
                missed = max(0, int(opportunity) - int(observed))
                line = f"Happened on {int(observed)} of {int(opportunity)} days"
                if missed:
                    line += f" (missed {missed})"
                ev.append(line)
            else:
                ev.append(f"Happened {count} times in the last 30 days")
            if consistency is not None:
                ev.append(f"Consistent on about {int(float(consistency) * 100)}% of days")
            if person:
                cond = d.get("condition")
                ent = cond.get("entity_id") if isinstance(cond, dict) else None
                who = names.get(ent) if ent else None
                ev.append(f"Specifically when {who or person} is home")
        elif pattern_type == "sequence":
            headline = "One action reliably follows another"
            first = d.get("first") or d.get("trigger")
            then = d.get("then") or d.get("action")
            if first and then:
                def _step(value):
                    if not isinstance(value, dict):
                        return str(value)
                    entity = value.get("entity")
                    name = _name_for(entity, names) if entity else "something"
                    state = value.get("state")
                    return f"{name} → {state}" if state is not None else name
                ev.append(f"After {_step(first)}, {_step(then)} usually follows")
            ev.append(f"Seen {count} times in 30 days")
            if d.get("window_seconds"):
                ev.append(f"Usually within {int(d['window_seconds'])}s")
        elif pattern_type == "repeated_command":
            headline = "A command you give often"
            cmd = d.get("command") or d.get("text")
            if cmd:
                ev.append(f"You've asked '{cmd}' {count} times")
            if d.get("hour") is not None:
                ev.append(f"Most often around {int(d['hour']):02d}:00")
        elif pattern_type == "temp_pref":
            headline = "A temperature preference"
            if d.get("target") is not None:
                ev.append(f"Set to {d['target']}° repeatedly")
            ev.append(f"Observed {count} times")
        elif pattern_type == "numeric_trigger":
            sensor = _name_for(str(d.get("trigger_sensor") or ""), names)
            raw_act = d.get("action")
            act: dict = raw_act if isinstance(raw_act, dict) else {}
            target = _name_for(str(act.get("entity") or ""), names)
            op = str(d.get("op") or "")
            threshold = d.get("threshold")
            headline = "A threshold routine"
            if threshold is not None and op in ("below", "above"):
                word = "drops below" if op == "below" else "rises above"
                ev.append(f"When {sensor} {word} {threshold:g}, "
                          f"{target} turns {act.get('state', '')}")
            followed, crossings = d.get("followed"), d.get("crossings")
            if followed is not None and crossings:
                window = int(d.get("window_seconds") or 600) // 60
                ev.append(f"Followed within {window} minutes on {int(followed)} "
                          f"of {int(crossings)} times it crossed")
            else:
                ev.append(f"Observed {count} times in 30 days")
            if d.get("action_count"):
                ev.append(f"{target} changed this way {int(d['action_count'])} "
                          f"times in 30 days")
            if d.get("distinct_days"):
                ev.append(f"On {int(d['distinct_days'])} different days")
        elif pattern_type == "presence":
            headline = "A presence-linked pattern"
            ev.append(f"Correlated {count} times over 30 days")
        else:
            headline = "A learned pattern"
            ev.append(f"Observed {count} times in 30 days")
        gate = d.get("presence_gate")
        if isinstance(gate, dict) and gate.get("area_name"):
            ev.append(f"Only when presence is detected in {gate['area_name']}")
        release = d.get("presence_release")
        if isinstance(release, dict) and release.get("area_name"):
            seconds = max(0, int(release.get("settle_seconds", 0) or 0))
            if seconds and seconds % 60 == 0:
                amount = seconds // 60
                duration = f"{amount} minute" + ("s" if amount != 1 else "")
            else:
                duration = f"{seconds} seconds"
            ev.append(
                f"Turns off again once presence in {release['area_name']} "
                f"has cleared for {duration}")
    except Exception:
        headline = headline or "A learned pattern"
        if not ev:
            ev.append(f"Observed {count} times")
    return {"headline": headline, "evidence": ev}


def _trigger_for(entity: str, state: str) -> dict:
    """HA trigger for a learned sequence's trigger entity. A person or
    device_tracker crossing home/away is emitted as a semantic *zone* trigger
    (HA's recommended way to fire on arrival/departure); an event.* entity
    (button/remote) fires on every event, so it becomes a state trigger on the
    entity with the specific press matched by a companion template condition (see
    ``_trigger_extra_conditions``); everything else stays a state trigger."""
    dom = entity.split(".")[0] if "." in entity else ""
    if dom in ("person", "device_tracker"):
        if state == "not_home":
            return {"platform": "zone", "entity_id": entity,
                    "zone": "zone.home", "event": "leave"}
        if state == "home":
            return {"platform": "zone", "entity_id": entity,
                    "zone": "zone.home", "event": "enter"}
    if dom == "event":
        return {"platform": "state", "entity_id": entity}
    if dom == "scene":
        # scene .state is a timestamp; any change to it is an activation
        return {"platform": "state", "entity_id": entity}
    return {"platform": "state", "entity_id": entity, "to": state}


def _trigger_extra_conditions(entity: str, state: str) -> list:
    """Companion conditions a trigger requires beyond the learned ones. An
    event.* entity fires on every press, so the specific press type is matched
    by a template condition on ``event_type`` — the reliable, integration-
    agnostic HA form for stateless event entities."""
    dom = entity.split(".")[0] if "." in entity else ""
    if dom == "event":
        return [{"condition": "template",
                 "value_template":
                     "{{ trigger.to_state.attributes.event_type == '%s' }}" % state}]
    return []


def _name_for(entity_id: str, names: Optional[dict] = None) -> str:
    """cognitive.naming.name_for, imported lazily (package layering)."""
    from ..cognitive.naming import name_for
    return name_for(entity_id, names)


def _trigger_phrase(entity: str, state: str, names: Optional[dict] = None) -> str:
    """Readable lead-in for a sequence description given its trigger, naming
    the entity with `names` (friendly name, else a readable id)."""
    dom = entity.split(".")[0] if "." in entity else ""
    name = _name_for(entity, names)
    if dom in ("person", "device_tracker"):
        if state == "not_home":
            return f"When {name} leaves home"
        if state == "home":
            return f"When {name} arrives home"
    if dom == "event":
        return f"When {name} is pressed ({state})"
    if dom == "scene":
        return f"When {name} is activated"
    return f"When {name} turns {state}"



# ── Home Assistant trigger / condition taxonomy (roadmap reference) ──────
# The long-term goal is for Nova to learn and emit the FULL range of HA
# triggers and conditions, not just the handful below. Keep this list current
# as coverage grows so future work knows the target.
#
# HA TRIGGER platforms:
#   state ✓(sequence, presence) · time ✓(time_routine) ·
#   numeric_state ✓(numeric_trigger) · zone ✓(sequence departure/arrival) ·
#   event ✓(button/remote presses via event.* entities) ·
#   time_pattern · sun · geo_location · template · homeassistant ·
#   mqtt · webhook · device · calendar · tag · conversation ·
#   persistent_notification
#   (device: the device_id/type/subtype form is integration-specific and not
#    emitted; modern buttons/remotes surface as event.* entities, which is the
#    general path used here — a state trigger + a template on event_type.)
# HA CONDITION types:
#   time ✓(sequence) · sun ✓(sequence) · numeric_state ✓(sequence) ·
#   state ✓(time_routine) · template ✓(event-press match) ·
#   zone · trigger · device · and · or · not
#
# Emitted today: TRIGGERS {state, time, numeric_state, zone, event};
#   CONDITIONS {time, sun, numeric_state, state, template} (ANDed as a list).
# Backlog (no longer the active list — revisit as desired):
#   • calendar / time_pattern — schedule-driven routines.
#   • state (presence) condition on sequences — the "away" direction only.
#   • numeric_state condition on time routines ("at 7pm, only if below 65").
# Each is its own focused build: mine the discriminator from history, attach
# only when it consistently holds, keep the HA dict self-describing so
# _generate_automation and normalize pass it through unchanged.
def generate_automation(pattern: DetectedPattern) -> str:
    """Generate HA automation YAML from a detected pattern."""
    p = pattern
    d = p.details
    raw_names = d.get("names")
    names: dict = raw_names if isinstance(raw_names, dict) else {}

    def n(entity_id):
        return _name_for(entity_id, names)

    if p.pattern_type == "time_routine" and d.get("state") in ("on", "off"):
        auto: dict[str, Any] = {
            "alias": f"Nova Learned: {n(p.entity_ids[0])} {d['state']} at {d['hour']:02d}:00",
            "trigger": {"platform": "time", "at": f"{d['hour']:02d}:00:00"},
            "action": {
                "service": f"{p.entity_ids[0].split('.')[0]}.turn_{d['state']}",
                "entity_id": p.entity_ids[0],
            },
        }
        cond = d.get("condition")
        conds = [c for c in (cond if isinstance(cond, list) else [cond])
                 if isinstance(c, dict) and c.get("condition")]
        if conds:
            auto["condition"] = conds
        return json.dumps(auto, indent=2)

    if p.pattern_type == "sequence":
        trigger = d.get("trigger", {})
        action = d.get("action", {})
        svc = service_for(action.get("entity", ""), action.get("state", ""))
        if not svc:
            return json.dumps({
                "note": f"Consider automating: {n(action.get('entity', '?'))} → "
                        f"{action.get('state','?')} after "
                        f"{n(trigger.get('entity', '?'))} "
                        f"{trigger.get('state','?')}",
                "type": "manual_review",
            }, indent=2)
        # Use the measured typical lag (rounded to 5s); omit a delay under 15s
        # so near-immediate reactions don't get an awkward tiny wait.
        lag = int(d.get("delay_seconds", 60) or 0)
        seq_action: list = []
        if lag >= 15:
            lag = int(round(lag / 5.0) * 5)
            seq_action.append({"delay": f"00:{lag // 60:02d}:{lag % 60:02d}"})
        gate = d.get("presence_gate") if isinstance(d.get("presence_gate"), dict) else None
        release = (d.get("presence_release")
                   if isinstance(d.get("presence_release"), dict) else None)
        off_svc = service_for(action.get("entity", ""), "off")
        has_release = bool(
            release and gate and off_svc
            and release.get("entity_id") == gate.get("entity_id"))
        mode = "single"
        if has_release:
            assert release is not None and off_svc is not None
            sensor_id = str(release["entity_id"])
            settle = max(0, min(600, int(release.get("settle_seconds", 0) or 0)))
            duration = (
                f"{settle // 3600:02d}:"
                f"{(settle % 3600) // 60:02d}:{settle % 60:02d}")
            # The top-level gate is evaluated at trigger time. Recheck after a
            # learned action delay so an early clear cannot leave the following
            # transition wait armed forever.
            present = {"condition": "state", "entity_id": sensor_id, "state": "on"}
            cleared = {"condition": "state", "entity_id": sensor_id, "state": "off"}
            seq_action.extend([
                present,
                svc,
                # Presence cleared while the device was turning on: give it
                # the settling time to come back before deciding.
                {
                    "choose": [{
                        "conditions": [cleared],
                        "sequence": [{
                            "wait_for_trigger": [{
                                "trigger": "state", "entity_id": sensor_id,
                                "to": "on",
                            }],
                            "timeout": duration,
                            "continue_on_timeout": True,
                        }],
                    }],
                },
                # Unless presence has already been clear for the settling
                # time, wait for it to clear from any state (an unavailable
                # sensor returning as off counts), for at most RELEASE_TIMEOUT.
                {
                    "choose": [{
                        "conditions": [{
                            "condition": "not",
                            "conditions": [dict(cleared, **{"for": duration})],
                        }],
                        "sequence": [{
                            "wait_for_trigger": [{
                                "trigger": "state", "entity_id": sensor_id,
                                "to": "off", "for": duration,
                            }],
                            "timeout": RELEASE_TIMEOUT,
                            "continue_on_timeout": True,
                        }],
                    }],
                },
                # Top level on purpose: a failed condition inside a choose
                # only ends that branch, so this is what keeps the device on
                # while presence is still (or again) detected.
                cleared,
                off_svc,
            ])
            mode = "restart"
        else:
            seq_action.append(svc)
        trig = _trigger_for(trigger["entity"], trigger["state"])
        extra = _trigger_extra_conditions(trigger["entity"], trigger["state"])
        if trig.get("platform") == "zone":
            verb = "leaves" if trig["event"] == "leave" else "arrives"
            alias = (f"Nova Learned: {n(action['entity'])} when "
                     f"{n(trigger['entity'])} {verb} home")
        elif (trigger["entity"].split(".")[0] if "." in trigger["entity"]
                else "") == "event":
            alias = f"Nova Learned: {n(action['entity'])} on {n(trigger['entity'])} press"
        else:
            alias = f"Nova Learned: {n(action['entity'])} after {n(trigger['entity'])}"
        if has_release:
            alias += ", off when presence clears"
        elif gate:
            alias += " when presence is detected"
        auto = {
            "alias": alias,
            "trigger": trig,
            "action": seq_action,
        }
        cond = d.get("condition")
        conds = [c for c in (cond if isinstance(cond, list) else [cond])
                 if isinstance(c, dict) and c.get("condition")] + extra
        if conds:
            auto["condition"] = conds
        if mode == "restart":
            auto["mode"] = mode
        return json.dumps(auto, indent=2)

    if p.pattern_type == "numeric_trigger":
        action = d.get("action", {})
        svc = service_for(action.get("entity", ""), action.get("state", ""))
        if not svc:
            return json.dumps({
                "type": "manual_review",
                "note": (f"Consider: {n(action.get('entity', '?'))} when "
                         f"{n(d.get('trigger_sensor', '?'))} {d.get('op','?')} "
                         f"{d.get('threshold','?')}"),
            }, indent=2)
        trig = {"platform": "numeric_state",
                "entity_id": d["trigger_sensor"], d["op"]: d["threshold"]}
        auto = {
            "alias": (f"Nova Learned: {n(action['entity'])} when "
                      f"{n(d['trigger_sensor'])} {d['op']} {d['threshold']:g}"),
            "trigger": trig,
            "action": [svc],
        }
        cond = d.get("condition")
        conds = [c for c in (cond if isinstance(cond, list) else [cond])
                 if isinstance(c, dict) and c.get("condition")]
        if conds:
            auto["condition"] = conds
        if isinstance(d.get("presence_gate"), dict):
            auto["alias"] += " when presence is detected"
        return json.dumps(auto, indent=2)

    if p.pattern_type == "repeated_command":
        return json.dumps({
            "note": f"Consider automating: '{d.get('command', '')}' at {d.get('hour', 0):02d}:00",
            "type": "manual_review",
        }, indent=2)

    if p.pattern_type == "presence":
        svc = service_for(d.get("action_entity", ""), d.get("action_state", ""))
        if not svc:
            return json.dumps({
                "note": f"Consider automating: {n(d.get('action_entity', '?'))} → "
                        f"{d.get('action_state','?')} when "
                        f"{n(d.get('trigger_person', '?'))} "
                        f"{d.get('trigger_state','?')}",
                "type": "manual_review",
            }, indent=2)
        return json.dumps({
            "alias": (f"Nova Learned: {n(d['action_entity'])} when "
                      f"{n(d['trigger_person'])} {d['trigger_state']}"),
            "trigger": {
                "platform": "state",
                "entity_id": d["trigger_person"],
                "to": d["trigger_state"],
            },
            "action": svc,
        }, indent=2)

    return json.dumps({"note": p.description}, indent=2)


def suggestion_identity(pattern_type: str, entity_ids, details) -> Optional[tuple]:
    """The stable identity of a suggestion's behaviour, or None when the
    pattern type (or a legacy row without details) has none.

    It names what triggers the behaviour and what the behaviour does. Measured
    values that drift between analyses (counts, coverage, a sequence's mean
    delay, a learned time window or sun condition, a numeric threshold, the
    probable owner) are deliberately not part of it: the same behaviour
    measured again is the same suggestion, and a dismissed one stays
    dismissed. A different trigger, target, state or hour is a different
    behaviour and so a different suggestion."""
    d = details if isinstance(details, dict) else {}
    ents = [str(e) for e in (entity_ids or [])]
    try:
        if pattern_type == "time_routine":
            if not ents or d.get("hour") is None or d.get("state") is None:
                return None
            return (pattern_type, ents[0], str(d["state"]), int(d["hour"]))
        if pattern_type == "repeated_command":
            if not d.get("command") or d.get("hour") is None:
                return None
            return (pattern_type, str(d["command"]), int(d["hour"]))
        if pattern_type == "sequence":
            trig, act = d.get("trigger") or {}, d.get("action") or {}
            if not trig.get("entity") or not act.get("entity"):
                return None
            identity = (pattern_type, str(trig["entity"]), str(trig.get("state")),
                        str(act["entity"]), str(act.get("state")))
            if isinstance(d.get("presence_release"), dict):
                return identity + ("presence_release",)
            return identity
        if pattern_type == "numeric_trigger":
            act = d.get("action") or {}
            if not d.get("trigger_sensor") or not act.get("entity"):
                return None
            return (pattern_type, str(d["trigger_sensor"]), str(d.get("op")),
                    str(act["entity"]), str(act.get("state")))
        if pattern_type == "presence":
            if not d.get("trigger_person") or not d.get("action_entity"):
                return None
            return (pattern_type, str(d["trigger_person"]),
                    str(d.get("trigger_state")), str(d["action_entity"]),
                    str(d.get("action_state")))
    except (TypeError, ValueError):
        return None
    return None


def _variant_family(key: Optional[tuple]) -> Optional[tuple]:
    """Identity with the presence-release marker removed.

    A gated sequence and the same sequence with "off when presence clears"
    are separate suggestions (a dismissed one never hides the other) but one
    behaviour, so only one of them is ever pending."""
    if key and key[-1] == "presence_release":
        return key[:-1]
    return key


def _find_existing(conn: sqlite3.Connection, pattern: DetectedPattern):
    """(id, status) of the stored suggestion for this pattern, or None.

    A pending row wins over decided ones so a refresh lands where the
    reviewer will see it; any decided row still means "not new". A row
    retired as superseded is only returned when nothing else matches."""
    key = suggestion_identity(pattern.pattern_type, pattern.entity_ids,
                              pattern.details)
    texts = [pattern.description]
    if pattern.legacy_description and pattern.legacy_description != pattern.description:
        # Rows stored before v7.125 were worded with entity ids.
        texts.append(pattern.legacy_description)
    same_text = conn.execute(
        "SELECT id, status, pattern_type, entity_ids, details FROM suggestions "
        f"WHERE description IN ({', '.join('?' for _ in texts)})",
        tuple(texts)).fetchall()
    matches = []
    for rid, status, ptype, ents, details in same_text:
        # The description is only a fallback for rows without an identity;
        # it must not join two variants whose text happens to coincide.
        row_key = suggestion_identity(ptype, loads_json(ents, []),
                                      loads_json(details, {}))
        if key is None or row_key is None or row_key == key:
            matches.append((int(rid), str(status or "")))
    if key is not None:
        rows = conn.execute(
            "SELECT id, status, entity_ids, details FROM suggestions "
            "WHERE pattern_type = ? ORDER BY id", (pattern.pattern_type,)).fetchall()
        for rid, status, ents, details in rows:
            if suggestion_identity(pattern.pattern_type,
                                   loads_json(ents, []),
                                   loads_json(details, {})) == key:
                matches.append((int(rid), str(status or "")))
    if not matches:
        return None
    for match in matches:
        if match[1] == SUGGESTION_PENDING:
            return match
    for match in matches:
        if match[1] not in (SUGGESTION_SUPERSEDED, SUGGESTION_RETIRED):
            return match
    return matches[0]


def _retire_other_variants(conn: sqlite3.Connection, pattern: DetectedPattern,
                           keep_id: int) -> None:
    """Mark other pending variants of this behaviour superseded.

    Only pending rows move; dismissed, approved, installed and covered rows
    are decisions and stay exactly as they are."""
    key = suggestion_identity(pattern.pattern_type, pattern.entity_ids,
                              pattern.details)
    family = _variant_family(key)
    if family is None:
        return
    rows = conn.execute(
        "SELECT id, entity_ids, details FROM suggestions "
        "WHERE pattern_type = ? AND status = ? AND id != ?",
        (pattern.pattern_type, SUGGESTION_PENDING, keep_id)).fetchall()
    for rid, ents, details in rows:
        row_key = suggestion_identity(pattern.pattern_type, loads_json(ents, []),
                                      loads_json(details, {}))
        if row_key != key and _variant_family(row_key) == family:
            conn.execute(
                "UPDATE suggestions SET status = ? WHERE id = ? AND status = ?",
                (SUGGESTION_SUPERSEDED, rid, SUGGESTION_PENDING))


class SuggestionStore:
    """SQL access to the suggestions table. Stateless: every call opens and
    closes its own connection in the calling thread."""

    def __init__(self, db_path: str = DB_PATH):
        self._db = db_path

    def _connect(self) -> Optional[sqlite3.Connection]:
        try:
            if not Path(self._db).exists():
                return None
            conn = sqlite3.connect(self._db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.row_factory = sqlite3.Row
            return conn
        except Exception:
            return None

    def store(self, pattern: DetectedPattern, generate=None,
              status: str = SUGGESTION_PENDING) -> bool:
        """Store a pattern as a suggestion in the DB. Returns True if new.

        ``status`` is the status a NEW row starts with: pending, or
        rejected when the AI suggestion review turned it down (v7.126.0).
        An existing row keeps its own status rules."""
        try:
            conn = sqlite3.connect(self._db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            # The same behaviour is ONE suggestion however its counts drift.
            # Match on the pattern's stable identity (what triggers it and
            # what it does), never on the description, which embeds counts.
            existing = _find_existing(conn, pattern)
            if existing:
                sid, status = existing
                if status in REVIVABLE_STATUSES:
                    # Still under review (or retired only because another
                    # variant was detected later): refresh the evidence and
                    # payload so the reviewer sees (and installs) the latest
                    # measurement, and make this the one pending variant.
                    # An AI review already recorded on the row is kept.
                    details = dict(pattern.details or {})
                    if "review" not in details:
                        prior = conn.execute(
                            "SELECT details FROM suggestions WHERE id = ?",
                            (sid,)).fetchone()
                        prior_d = loads_json(prior[0], {}) if prior else {}
                        if isinstance(prior_d, dict) and isinstance(
                                prior_d.get("review"), dict):
                            details["review"] = prior_d["review"]
                    conn.execute(
                        "UPDATE suggestions SET confidence = ?, pattern_count = ?, "
                        "description = ?, details = ?, automation_yaml = ?, "
                        "status = ? WHERE id = ?",
                        (pattern.confidence, pattern.occurrences,
                         pattern.description, json.dumps(details),
                         (generate or generate_automation)(pattern),
                         SUGGESTION_PENDING, sid),
                    )
                    _retire_other_variants(conn, pattern, sid)
                else:
                    # Decided (dismissed, installed, covered, approved): keep
                    # the decision; only the counts move, as they always have.
                    conn.execute(
                        "UPDATE suggestions SET confidence = ?, pattern_count = ? "
                        "WHERE id = ?",
                        (pattern.confidence, pattern.occurrences, sid),
                    )
                conn.commit()
                conn.close()
                return False

            # Generate automation YAML suggestion
            auto_yaml = (generate or generate_automation)(pattern)

            status = (SUGGESTION_REJECTED if status == SUGGESTION_REJECTED
                      else SUGGESTION_PENDING)
            _cur = conn.execute(
                "INSERT INTO suggestions (created, description, automation_yaml, "
                "confidence, pattern_count, pattern_type, entity_ids, details, "
                "status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now().isoformat(), pattern.description,
                 auto_yaml, pattern.confidence, pattern.occurrences,
                 pattern.pattern_type,
                 json.dumps(pattern.entity_ids or []),
                 json.dumps(pattern.details or {}), status),
            )
            _new_sid = _cur.lastrowid
            if status == SUGGESTION_PENDING:
                _retire_other_variants(conn, pattern, cast(int, _new_sid))
            conn.commit()
            conn.close()
            if status == SUGGESTION_REJECTED:
                # Not a proposal anyone saw: no "suggestion" record, so the
                # adaptive threshold only ever learns from people's choices.
                return False
            try:
                from .. import decision_record
                decision_record.record(
                    "suggestion",
                    observation={"pattern_type": pattern.pattern_type,
                                 "entities": pattern.entity_ids or [],
                                 "occurrences": pattern.occurrences},
                    interpretation={"suggested": pattern.description},
                    decision="propose automation",
                    reason="recurring observed behavior",
                    confidence=pattern.confidence,
                    ref="suggestion:%d" % cast(int, _new_sid),
                )
            except Exception:
                pass
            return True
        except Exception as exc:
            _LOGGER.debug("Store suggestion error: %s", exc)
            return False

    def lookup(self, pattern: DetectedPattern) -> Optional[dict]:
        """The stored row for this pattern's behaviour as {"id", "status",
        "reviewed"}, or None when it has never been stored."""
        conn = self._connect()
        if not conn:
            return None
        try:
            found = _find_existing(conn, pattern)
            if not found:
                return None
            sid, status = found
            row = conn.execute("SELECT details FROM suggestions WHERE id = ?",
                               (sid,)).fetchone()
            details = loads_json(row[0], {}) if row else {}
            reviewed = isinstance(details, dict) and isinstance(
                details.get("review"), dict)
            return {"id": sid, "status": status, "reviewed": reviewed}
        except Exception:
            return None
        finally:
            conn.close()

    def reject(self, suggestion_id: int, review: dict) -> bool:
        """Mark a pending suggestion rejected by the AI review, keeping the
        review (verdict, reason, model) in its details."""
        conn = self._connect()
        if not conn:
            return False
        try:
            row = conn.execute(
                "SELECT details, status FROM suggestions WHERE id = ?",
                (suggestion_id,)).fetchone()
            if not row or row["status"] not in REVIVABLE_STATUSES:
                return False
            details = loads_json(row["details"], {})
            details = details if isinstance(details, dict) else {}
            details["review"] = dict(review or {})
            conn.execute(
                "UPDATE suggestions SET status = ?, details = ? WHERE id = ?",
                (SUGGESTION_REJECTED, json.dumps(details), suggestion_id))
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def restore(self, suggestion_id: int) -> bool:
        """Bring a suggestion the AI review rejected back for a person to
        decide. It is not reviewed again."""
        conn = self._connect()
        if not conn:
            return False
        try:
            row = conn.execute(
                "SELECT details, status FROM suggestions WHERE id = ?",
                (suggestion_id,)).fetchone()
            if not row or row["status"] != SUGGESTION_REJECTED:
                return False
            details = loads_json(row["details"], {})
            details = details if isinstance(details, dict) else {}
            raw_review = details.get("review")
            review: dict = raw_review if isinstance(raw_review, dict) else {}
            details["review"] = dict(review, overridden=True)
            conn.execute(
                "UPDATE suggestions SET status = ?, details = ? WHERE id = ?",
                (SUGGESTION_PENDING, json.dumps(details), suggestion_id))
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def rejected(self, limit: int = 20) -> list[dict]:
        """Suggestions the AI review turned down, newest first."""
        conn = self._connect()
        if not conn:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM suggestions WHERE status = ? "
                "ORDER BY id DESC LIMIT ?", (SUGGESTION_REJECTED, int(limit))
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def retire_missing(self, pattern_type: str, detected: list) -> int:
        """Retire pending suggestions of `pattern_type` whose behaviour was
        not among the `detected` patterns of this pass. Retired rows are
        hidden and come back as pending if detected again. Returns how many
        were retired."""
        keys = {suggestion_identity(p.pattern_type, p.entity_ids, p.details)
                for p in detected if p.pattern_type == pattern_type}
        conn = self._connect()
        if not conn:
            return 0
        try:
            rows = conn.execute(
                "SELECT id, entity_ids, details FROM suggestions "
                "WHERE pattern_type = ? AND status = ?",
                (pattern_type, SUGGESTION_PENDING)).fetchall()
            retired = 0
            for row in rows:
                key = suggestion_identity(pattern_type,
                                          loads_json(row["entity_ids"], []),
                                          loads_json(row["details"], {}))
                if key is None or key in keys:
                    continue
                conn.execute(
                    "UPDATE suggestions SET status = ? WHERE id = ? AND status = ?",
                    (SUGGESTION_RETIRED, row["id"], SUGGESTION_PENDING))
                retired += 1
            conn.commit()
            return retired
        except Exception:
            return 0
        finally:
            conn.close()

    def pending(self) -> list[dict]:
        """Get all pending suggestions for the user to review."""
        conn = self._connect()
        if not conn:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM suggestions WHERE status = 'pending' "
                "ORDER BY confidence DESC LIMIT 20"
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def get(self, suggestion_id: int) -> Optional[dict]:
        """One suggestion row by id, or None."""
        conn = self._connect()
        if not conn:
            return None
        try:
            row = conn.execute(
                "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None
        finally:
            conn.close()

    def mark_installed(self, suggestion_id: int, automation_id: str) -> None:
        """Record that an approved suggestion became a live automation."""
        conn = self._connect()
        if not conn:
            return
        try:
            # widen status vocabulary without a migration: 'installed' is just
            # another string the UI can render distinctly from 'approved'.
            conn.execute(
                "UPDATE suggestions SET status = 'installed', "
                "approved_at = ? WHERE id = ?",
                (datetime.now().isoformat(), suggestion_id),
            )
            try:  # Decision Record outcome (v7.40.0): an installed suggestion was useful
                from .. import decision_record
                decision_record.set_outcome_by_ref(
                    "suggestion:%d" % suggestion_id, "good", "installed")
            except Exception:
                pass
            try:  # Automation Trial (Phase 3): tracks whether it RUNS — kept
                # deliberately separate from the acceptance outcome above.
                # Installing it only proves the suggestion was accepted, not
                # that the automation works.
                from . import trials as automation_trials
                automation_trials.create(suggestion_id, automation_id)
            except Exception:
                pass
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

    def mark_covered(self, suggestion_id: int) -> None:
        """Retire a stale suggestion when HA now has an equivalent automation."""
        conn = self._connect()
        if not conn:
            return
        try:
            conn.execute(
                "UPDATE suggestions SET status = 'already_automated' WHERE id = ?",
                (suggestion_id,),
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

    def approve(self, suggestion_id: int) -> bool:
        """Mark a suggestion as approved."""
        conn = self._connect()
        if not conn:
            return False
        try:
            conn.execute(
                "UPDATE suggestions SET status = 'approved', "
                "approved_at = ? WHERE id = ?",
                (datetime.now().isoformat(), suggestion_id),
            )
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def dismiss(self, suggestion_id: int) -> bool:
        """Mark a suggestion as dismissed."""
        conn = self._connect()
        if not conn:
            return False
        try:
            conn.execute(
                "UPDATE suggestions SET status = 'dismissed', "
                "dismissed_at = ? WHERE id = ?",
                (datetime.now().isoformat(), suggestion_id),
            )
            conn.commit()
            try:  # Decision Record outcome (v7.40.0): a dismissed suggestion was unnecessary
                from .. import decision_record
                decision_record.set_outcome_by_ref(
                    "suggestion:%d" % suggestion_id, "unnecessary", "dismiss_suggestion")
            except Exception:
                pass
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def stats(self) -> dict:
        """Return analysis statistics."""
        conn = self._connect()
        if not conn:
            return {"available": False}
        try:
            stats = {
                "available": True,
                "state_changes": conn.execute(
                    "SELECT COUNT(*) FROM state_changes").fetchone()[0],
                "commands": conn.execute(
                    "SELECT COUNT(*) FROM commands").fetchone()[0],
                "pending_suggestions": conn.execute(
                    "SELECT COUNT(*) FROM suggestions WHERE status='pending'"
                ).fetchone()[0],
                "approved": conn.execute(
                    "SELECT COUNT(*) FROM suggestions WHERE status='approved'"
                ).fetchone()[0],
                "dismissed": conn.execute(
                    "SELECT COUNT(*) FROM suggestions WHERE status='dismissed'"
                ).fetchone()[0],
            }
            oldest = conn.execute(
                "SELECT MIN(timestamp) FROM state_changes"
            ).fetchone()[0]
            if oldest:
                stats["days_of_data"] = (
                    datetime.now() - datetime.fromisoformat(oldest)
                ).days
                stats["ready_for_analysis"] = stats["days_of_data"] >= MIN_DAYS
            else:
                stats["days_of_data"] = 0
                stats["ready_for_analysis"] = False
            return stats
        except Exception:
            return {"available": False}
        finally:
            conn.close()
