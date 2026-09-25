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
from typing import Optional

from .models import DetectedPattern

_LOGGER = logging.getLogger(__name__)

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
                ev.append(f"Specifically when {person} is home")
        elif pattern_type == "sequence":
            headline = "One action reliably follows another"
            first = d.get("first") or d.get("trigger")
            then = d.get("then") or d.get("action")
            if first and then:
                ev.append(f"After {first}, {then} usually follows")
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
        elif pattern_type == "presence":
            headline = "A presence-linked pattern"
            ev.append(f"Correlated {count} times over 30 days")
        else:
            headline = "A learned pattern"
            ev.append(f"Observed {count} times in 30 days")
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


def _trigger_phrase(entity: str, state: str) -> str:
    """Readable lead-in for a sequence description given its trigger."""
    dom = entity.split(".")[0] if "." in entity else ""
    if dom in ("person", "device_tracker"):
        if state == "not_home":
            return f"When {entity} leaves home"
        if state == "home":
            return f"When {entity} arrives home"
    if dom == "event":
        return f"When {entity} is pressed ({state})"
    if dom == "scene":
        return f"When {entity} is activated"
    return f"When {entity} turns {state}"



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

    if p.pattern_type == "time_routine" and d.get("state") in ("on", "off"):
        auto = {
            "alias": f"Nova Learned: {p.entity_ids[0]} {d['state']} at {d['hour']:02d}:00",
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
                "note": f"Consider automating: {action.get('entity','?')} → "
                        f"{action.get('state','?')} after "
                        f"{trigger.get('entity','?')} "
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
        seq_action.append(svc)
        trig = _trigger_for(trigger["entity"], trigger["state"])
        extra = _trigger_extra_conditions(trigger["entity"], trigger["state"])
        if trig.get("platform") == "zone":
            verb = "leaves" if trig["event"] == "leave" else "arrives"
            alias = f"Nova Learned: {action['entity']} when {trigger['entity']} {verb} home"
        elif (trigger["entity"].split(".")[0] if "." in trigger["entity"]
                else "") == "event":
            alias = f"Nova Learned: {action['entity']} on {trigger['entity']} press"
        else:
            alias = f"Nova Learned: {action['entity']} after {trigger['entity']}"
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
        return json.dumps(auto, indent=2)

    if p.pattern_type == "numeric_trigger":
        action = d.get("action", {})
        svc = service_for(action.get("entity", ""), action.get("state", ""))
        if not svc:
            return json.dumps({
                "type": "manual_review",
                "note": (f"Consider: {action.get('entity','?')} when "
                         f"{d.get('trigger_sensor','?')} {d.get('op','?')} "
                         f"{d.get('threshold','?')}"),
            }, indent=2)
        trig = {"platform": "numeric_state",
                "entity_id": d["trigger_sensor"], d["op"]: d["threshold"]}
        return json.dumps({
            "alias": (f"Nova Learned: {action['entity']} when "
                      f"{d['trigger_sensor']} {d['op']} {d['threshold']:g}"),
            "trigger": trig,
            "action": [svc],
        }, indent=2)

    if p.pattern_type == "repeated_command":
        return json.dumps({
            "note": f"Consider automating: '{d.get('command', '')}' at {d.get('hour', 0):02d}:00",
            "type": "manual_review",
        }, indent=2)

    if p.pattern_type == "presence":
        svc = service_for(d.get("action_entity", ""), d.get("action_state", ""))
        if not svc:
            return json.dumps({
                "note": f"Consider automating: {d.get('action_entity','?')} → "
                        f"{d.get('action_state','?')} when "
                        f"{d.get('trigger_person','?')} "
                        f"{d.get('trigger_state','?')}",
                "type": "manual_review",
            }, indent=2)
        return json.dumps({
            "alias": f"Nova Learned: {d['action_entity']} when {d['trigger_person']} {d['trigger_state']}",
            "trigger": {
                "platform": "state",
                "entity_id": d["trigger_person"],
                "to": d["trigger_state"],
            },
            "action": svc,
        }, indent=2)

    return json.dumps({"note": p.description}, indent=2)


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

    def store(self, pattern: DetectedPattern, generate=None) -> bool:
        """Store a pattern as a suggestion in the DB. Returns True if new."""
        try:
            conn = sqlite3.connect(self._db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            # Check if similar suggestion already exists
            existing = conn.execute(
                "SELECT id FROM suggestions WHERE description = ?",
                (pattern.description,)
            ).fetchone()
            if existing:
                # Update occurrence count and confidence
                conn.execute(
                    "UPDATE suggestions SET confidence = ?, pattern_count = ? WHERE id = ?",
                    (pattern.confidence, pattern.occurrences, existing[0]),
                )
                conn.commit()
                conn.close()
                return False

            # Generate automation YAML suggestion
            auto_yaml = (generate or generate_automation)(pattern)

            _cur = conn.execute(
                "INSERT INTO suggestions (created, description, automation_yaml, "
                "confidence, pattern_count, pattern_type, entity_ids, details, "
                "status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (datetime.now().isoformat(), pattern.description,
                 auto_yaml, pattern.confidence, pattern.occurrences,
                 pattern.pattern_type,
                 json.dumps(pattern.entity_ids or []),
                 json.dumps(pattern.details or {})),
            )
            _new_sid = _cur.lastrowid
            conn.commit()
            conn.close()
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
                    ref="suggestion:%d" % _new_sid,
                )
            except Exception:
                pass
            return True
        except Exception as exc:
            _LOGGER.debug("Store suggestion error: %s", exc)
            return False

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

