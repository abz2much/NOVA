"""Pure deterministic evaluators for proactive decisions.

Each evaluator takes an immutable EventSnapshot (plus, for the Local Mind,
the history grade, case prior and flap flag the coordinator collected) and
returns a typed Decision, or None when its rule does not apply. None of them
touches Home Assistant, a database, a provider, a service, the reasoning
cache, speech, the scheduler or module state; the words a person hears are
described as a Phrase and rendered later by presentation.

LOCAL_RULES is the explicit priority order of the local templates, exactly
the order v7.119.0 applied them in: critical hazards first, before
recent-announcement dedup and before any occupancy shortcut, so an active
smoke, gas, leak or carbon monoxide alarm is voiced whatever the sensor is
called and whoever is home.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from ..const import URGENCY_CEILINGS, URGENCY_CRITICAL
from .models import (
    ACTION_SILENT,
    Decision,
    EventSnapshot,
    Evidence,
    ORIGIN_CACHE,
    ORIGIN_FALLBACK,
    ORIGIN_LOCAL_MIND,
    PROVENANCE_CLASSIFIER,
    PROVENANCE_DEVICE_CLASS,
    PROVENANCE_NAME,
    Phrase,
    R_ALARM_TRIGGERED,
    R_APPLIANCE_DONE,
    R_ARRIVAL,
    R_BATTERY_LOW,
    R_CACHE_SILENT,
    R_CACHE_SPEAK,
    R_CLIMATE_EXTREME,
    R_CLIMATE_NORMAL,
    R_DEPARTURE_AUDIENCE,
    R_ENTRY_CLOSED,
    R_ENTRY_LOW,
    R_ENTRY_OPEN,
    R_ENTRY_OPEN_OCCUPIED,
    R_FALLBACK_QUIET,
    R_FALLBACK_URGENT,
    R_GARAGE_CLOSED,
    R_GARAGE_OPEN,
    R_HAZARD_ACTIVE,
    R_HAZARD_INACTIVE,
    R_LAST_DEPARTURE,
    R_LOCAL_MIND,
    R_LOCK_AWAY,
    R_LOCK_OCCUPIED,
    R_LOCK_SECURED,
    R_LOW_URGENCY,
    R_MOTION,
    R_MOTION_ROUTINE,
    R_POWER_SPIKE,
    R_RECENTLY_ANNOUNCED,
    R_SECURITY_OCCUPIED,
    SOURCE_ANNOUNCEMENTS,
    SOURCE_CASE_MEMORY,
    SOURCE_CLASSIFIER,
    SOURCE_HISTORY,
    TRUST_DERIVED,
    TRUST_HEURISTIC,
    silent,
    speak,
)

# A safety sensor is only a genuine emergency when it ENTERS an active state.
# Going unavailable/unknown or returning to normal (off/dry/clear) is not.
ACTIVE_TRIGGER_STATES = frozenset({"on", "detected", "wet", "triggered", "unsafe"})

# Device classes whose urgency ceiling is critical (smoke, gas, moisture,
# carbon monoxide). Derived from the canonical ceilings, so the two can't
# drift; high/medium classes (tamper, safety, door, …) are never promoted.
CRITICAL_HAZARD_CLASSES = frozenset(
    dc for dc, ceiling in URGENCY_CEILINGS.items() if ceiling == URGENCY_CRITICAL)
HAZARD_WORDS = {"moisture": "a water leak", "carbon_monoxide": "carbon monoxide"}
NAMED_HAZARD_WORDS = ("smoke", "carbon_monoxide", "co_alarm", "gas", "leak",
                      "moisture", "flood", "glass_break")

_URGENT = ("critical", "high")


# ── Summary text helpers (pure) ─────────────────────────────────────────────

def summary_new_state(evt: str):
    """Post-transition state from a 'changed from X to Y' event summary."""
    m = re.search(r"\bto\s+([a-z_]+)\b", evt)
    return m.group(1) if m else None


def parse_summary(evt: str):
    """Best-effort structured fields from 'FNAME (entity_id) ... from X to Y'."""
    friendly = ""
    m = re.match(r"(.+?)\s*\(", evt)
    if m:
        friendly = m.group(1).strip()
    entity_id = ""
    m = re.search(r"\(([\w.]+)\)", evt)
    if m:
        entity_id = m.group(1)
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    from_s = to_s = ""
    m = re.search(r"changed from (\S+) to (\S+)", evt)
    if m:
        from_s, to_s = m.group(1), m.group(2)
    return friendly, entity_id, domain, from_s, to_s


def _device_from_summary(evt: str, default: str) -> str:
    m = re.search(r"([\w\s]+?)\s*\(", evt)
    return m.group(1).strip().title() if m else default


# ── 1. Critical safety and hazards ──────────────────────────────────────────

def is_critical_hazard_activation(device_class: str, from_state: str, to_state: str) -> bool:
    """True when a critical hazard sensor ENTERS an active state from a state
    that was not active. Used before the observer's debounce, ignore rule and
    rate limit so none of them can drop a new smoke, gas, leak or carbon
    monoxide alarm."""
    dc = (device_class or "").strip().lower()
    if dc not in CRITICAL_HAZARD_CLASSES:
        return False
    new = str(to_state or "").strip().lower()
    old = str(from_state or "").strip().lower()
    return new in ACTIVE_TRIGGER_STATES and old not in ACTIVE_TRIGGER_STATES


def evaluate_structured_hazard(s: EventSnapshot) -> Optional[Decision]:
    """A critical hazard sensor decided from structured fields only — never
    from words in its name. None when device_class isn't a critical hazard
    class, or when there's no new state to judge."""
    dc = (s.device_class or "").strip().lower()
    if dc not in CRITICAL_HAZARD_CLASSES:
        return None
    new_st = (s.to_state.strip().lower() or summary_new_state(s.summary_lower))
    if not new_st:
        return None
    ev = (s.state_evidence("hazard", provenance=PROVENANCE_DEVICE_CLASS),)
    if new_st not in ACTIVE_TRIGGER_STATES:
        # Cleared, back to normal, unavailable or unknown: not an emergency.
        return silent(R_HAZARD_INACTIVE, f"{dc} sensor not in triggered state ({new_st})",
                      evaluator="critical_hazard", evidence=ev)
    hazard = HAZARD_WORDS.get(dc, dc.replace("_", " "))
    source = s.friendly_name or s.entity_id or "a sensor"
    return speak(R_HAZARD_ACTIVE, "critical",
                 Phrase.of("lead_in", sentence=f"{hazard} detected by {source} — "
                                              "immediate attention required."),
                 reason=f"active {dc} hazard", evaluator="critical_hazard", evidence=ev)


def evaluate_named_hazard(s: EventSnapshot) -> Optional[Decision]:
    """Name-based fallback for a hazard sensor without a device_class. Like
    the structured check it runs before recent-announcement dedup."""
    evt = s.summary_lower
    for kw in NAMED_HAZARD_WORDS:
        if kw in evt:
            ev = (s.state_evidence("hazard", provenance=PROVENANCE_NAME,
                                   trust=TRUST_HEURISTIC),)
            new_st = summary_new_state(evt)
            active = (new_st in ACTIVE_TRIGGER_STATES) if new_st else \
                any(w in evt for w in ("detected", "triggered", " wet"))
            if not active:
                # Unavailable/unknown or back to normal — a connectivity blip
                # or all-clear is NOT an emergency.
                return silent(R_HAZARD_INACTIVE,
                              f"{kw} sensor not in triggered state ({new_st or 'n/a'})",
                              evaluator="named_hazard", evidence=ev)
            dev = _device_from_summary(evt, "")
            src = f" from {dev}" if dev else ""
            return speak(R_HAZARD_ACTIVE, "critical",
                         Phrase.of("lead_in", sentence=f"a {kw.replace('_', ' ')} alert{src} — "
                                                      "immediate attention required."),
                         evaluator="named_hazard", evidence=ev)
    return None


# ── 2. Repetition (self-awareness) ──────────────────────────────────────────

def evaluate_recent_repeat(s: EventSnapshot) -> Optional[Decision]:
    """Don't repeat one of the last five announcements."""
    evt = s.summary_lower
    for ann in s.recent_announcements[-5:]:
        if ann.lower()[:40] in evt[:40]:
            return silent(R_RECENTLY_ANNOUNCED, "recently announced similar event",
                          evaluator="repetition",
                          evidence=(Evidence(SOURCE_ANNOUNCEMENTS, "repetition",
                                             entity_id=s.entity_id, trust=TRUST_DERIVED),))
    return None


# ── 3. Security and entry ───────────────────────────────────────────────────

def evaluate_alarm(s: EventSnapshot) -> Optional[Decision]:
    """A real alarm-panel trigger is an emergency regardless of occupancy;
    open doors and unlocked locks are normal while a registered user is home."""
    if not (s.category == "security" and s.urgency == "critical"):
        return None
    ev = (Evidence(SOURCE_CLASSIFIER, "security", entity_id=s.entity_id,
                   transition=s.transition, provenance=PROVENANCE_CLASSIFIER),)
    if "alarm_control_panel." not in s.summary_lower and s.anyone_home:
        return silent(R_SECURITY_OCCUPIED,
                      "security event but a registered user is home — normal",
                      evaluator="alarm", evidence=ev)
    return speak(R_ALARM_TRIGGERED, "critical",
                 Phrase.of("lead_in", sentence="a security alert has been triggered. "
                                              "Immediate attention required."),
                 evaluator="alarm", evidence=ev)


def evaluate_entry_point(s: EventSnapshot) -> Optional[Decision]:
    """A door or window opening or closing."""
    if s.category != "doors_windows":
        return None
    evt = s.summary_lower
    new = summary_new_state(evt)
    ev = (s.state_evidence("entry"),)
    if new == "on" or "open" in evt or "off → on" in evt or "off→on" in evt:
        # Normal household activity when someone is home — only worth
        # surfacing when away (possible entry) or critical.
        if s.anyone_home and s.urgency != "critical":
            return silent(R_ENTRY_OPEN_OCCUPIED,
                          "door/window opened but a user is home — normal",
                          evaluator="entry", evidence=ev)
        dev_name = _device_from_summary(evt, "A door")
        if s.urgency in ("high", "critical", "medium"):
            return speak(R_ENTRY_OPEN, s.urgency,
                         Phrase.of("lm_compose", name=dev_name, device_class="door",
                                   to_state="open", away=not s.anyone_home,
                                   escalated=s.urgency in _URGENT),
                         evaluator="entry", evidence=ev)
        return silent(R_ENTRY_LOW, "low urgency door event — logged only",
                      evaluator="entry", evidence=ev)
    if new == "off" or "close" in evt or "on → off" in evt or "on→off" in evt:
        return silent(R_ENTRY_CLOSED, "door/window closed — normal operation",
                      evaluator="entry", evidence=ev)
    return None


def evaluate_lock(s: EventSnapshot) -> Optional[Decision]:
    if s.category != "security":
        return None
    evt = s.summary_lower
    ev = (s.state_evidence("entry"),)
    if "unlock" in evt:
        if s.anyone_home:
            return silent(R_LOCK_OCCUPIED, "lock unlocked but a user is home — normal",
                          evaluator="entry", evidence=ev)
        dev_name = _device_from_summary(evt, "A lock")
        return speak(R_LOCK_AWAY, "medium",
                     Phrase.of("lm_compose", name=dev_name, device_class="lock",
                               to_state="unlocked", away=not s.anyone_home, escalated=True),
                     evaluator="entry", evidence=ev)
    if "locked" in evt:
        return silent(R_LOCK_SECURED, "lock secured — normal operation",
                      evaluator="entry", evidence=ev)
    return None


def evaluate_garage(s: EventSnapshot) -> Optional[Decision]:
    evt = s.summary_lower
    if "garage" not in evt:
        return None
    ev = (s.state_evidence("entry", provenance=PROVENANCE_NAME, trust=TRUST_HEURISTIC),)
    if "open" in evt:
        return speak(R_GARAGE_OPEN, s.urgency if s.urgency != "low" else "medium",
                     Phrase.of("lead_in", sentence="the garage door has been opened."),
                     evaluator="entry", evidence=ev)
    if "close" in evt or "closing" in evt:
        return silent(R_GARAGE_CLOSED, "garage closing — normal operation",
                      evaluator="entry", evidence=ev)
    return None


# ── 4. Presence and occupancy ───────────────────────────────────────────────

def evaluate_presence(s: EventSnapshot) -> Optional[Decision]:
    """Arrival or departure, read from the entity's own from/to states (an
    exact comparison against the literal "home"), never from summary prose.
    A zone-to-zone move is neither."""
    if s.category != "presence":
        return None
    is_arrival = s.to_state == "home" and s.from_state != "home"
    is_departure = s.from_state == "home" and s.to_state != "home"
    if not (is_arrival or is_departure):
        return None
    name = "Someone"
    nm = re.search(r"person\.(\w+)", s.summary_lower)
    if nm:
        name = nm.group(1).replace("_", " ").title()
    ev = (s.state_evidence("presence"),)
    if is_arrival:
        return speak(R_ARRIVAL, "medium", Phrase.of("arrival", name=name),
                     evaluator="presence", evidence=ev)
    if not s.anyone_home:
        return silent(R_LAST_DEPARTURE, "last person departure",
                      evaluator="presence", evidence=ev)
    # Someone remains home, so this announcement has an audience.
    return speak(R_DEPARTURE_AUDIENCE, "low",
                 Phrase.of("lead_in", sentence=f"{name} has left the premises."),
                 evaluator="presence", evidence=ev)


# ── 5. Relevance ────────────────────────────────────────────────────────────

def evaluate_motion(s: EventSnapshot) -> Optional[Decision]:
    evt = s.summary_lower
    if "motion" not in evt and "occupancy" not in evt:
        return None
    ev = (s.state_evidence("occupancy"),)
    if s.urgency in ("medium", "high"):
        area_name = _device_from_summary(evt, "an area")
        return speak(R_MOTION, s.urgency,
                     Phrase.of("lm_compose", name=area_name, device_class="motion",
                               to_state="on", away=not s.anyone_home,
                               escalated=s.urgency == "high"),
                     evaluator="relevance", evidence=ev)
    return silent(R_MOTION_ROUTINE, "routine motion — logged only",
                  evaluator="relevance", evidence=ev)


def evaluate_battery(s: EventSnapshot) -> Optional[Decision]:
    evt = s.summary_lower
    if not (s.category == "other" and ("battery" in evt or "low_battery" in evt)):
        return None
    dev_name = _device_from_summary(evt, "A device")
    return speak(R_BATTERY_LOW, "low",
                 Phrase.of("lead_in", sentence=f"{dev_name}'s battery is running low."),
                 evaluator="relevance", evidence=(s.state_evidence("maintenance"),))


def evaluate_appliance(s: EventSnapshot) -> Optional[Decision]:
    evt = s.summary_lower
    for appliance in ("washer", "dryer", "dishwasher", "washing_machine"):
        if appliance in evt and ("idle" in evt or "off" in evt
                                 or "complete" in evt or "not_running" in evt):
            nice = appliance.replace("_", " ").title()
            return speak(R_APPLIANCE_DONE, "medium",
                         Phrase.of("lead_in",
                                   sentence=f"the {nice} cycle appears to be complete."),
                         evaluator="relevance",
                         evidence=(s.state_evidence("appliance", provenance=PROVENANCE_NAME,
                                                    trust=TRUST_HEURISTIC),))
    return None


def evaluate_climate(s: EventSnapshot) -> Optional[Decision]:
    if s.category != "climate":
        return None
    evt = s.summary_lower
    ev = (s.state_evidence("climate"),)
    temp_match = re.search(r"(\d+(?:\.\d+)?)", evt)
    if temp_match:
        try:
            temp = float(temp_match.group(1))
        except (ValueError, TypeError):
            temp = None
        if temp is not None and temp > 90:
            return speak(R_CLIMATE_EXTREME, "medium",
                         Phrase.of("lead_in", sentence=(
                             f"indoor temperature has reached {temp}°. "
                             "You may want to check the climate control.")),
                         evaluator="relevance", evidence=ev)
        if temp is not None and temp < 55:
            return speak(R_CLIMATE_EXTREME, "medium",
                         Phrase.of("lead_in", sentence=(
                             f"indoor temperature has dropped to {temp}°. "
                             "Heating may need attention.")),
                         evaluator="relevance", evidence=ev)
    return silent(R_CLIMATE_NORMAL, "climate change within normal range",
                  evaluator="relevance", evidence=ev)


def evaluate_energy(s: EventSnapshot) -> Optional[Decision]:
    if s.category == "energy" and "power" in s.summary_lower:
        return silent(R_POWER_SPIKE, "power spike noted but not urgent enough to announce",
                      evaluator="relevance", evidence=(s.state_evidence("energy"),))
    return None


def evaluate_low_urgency(s: EventSnapshot) -> Optional[Decision]:
    if s.urgency == "low":
        return silent(R_LOW_URGENCY, "low urgency — logged but not announced",
                      evaluator="relevance")
    return None


# Explicit priority order of the local templates. The first that applies
# decides; None from every rule means the event is genuinely ambiguous and
# goes to the learned cache, then the provider.
LOCAL_RULES: tuple[tuple[str, Callable[[EventSnapshot], Optional[Decision]]], ...] = (
    ("critical_hazard", evaluate_structured_hazard),
    ("named_hazard", evaluate_named_hazard),
    ("repetition", evaluate_recent_repeat),
    ("alarm", evaluate_alarm),
    ("presence", evaluate_presence),
    ("entry_point", evaluate_entry_point),
    ("lock", evaluate_lock),
    ("motion", evaluate_motion),
    ("garage", evaluate_garage),
    ("battery", evaluate_battery),
    ("appliance", evaluate_appliance),
    ("climate", evaluate_climate),
    ("energy", evaluate_energy),
    ("low_urgency", evaluate_low_urgency),
)


def evaluate_local_rules(s: EventSnapshot) -> list[Decision]:
    """Every local rule that applies, in priority order (for arbitration and
    diagnostics). Pure."""
    out = []
    for _name, rule in LOCAL_RULES:
        d = rule(s)
        if d is not None:
            out.append(d)
    return out


# ── 6. Local Mind (the offline decision procedure) ──────────────────────────

SECURITY_CLASSES = frozenset({"door", "window", "garage_door", "lock", "opening", "motion"})
SECURITY_DOMAINS = frozenset({"lock", "cover"})
OPENING_STATES = frozenset({"on", "open", "opening", "unlocked", "detected", "true"})


def is_duplicate(friendly_name: str, entity_id: str, recent_announcements) -> bool:
    """Have we already told the user about this device very recently?"""
    needles = set()
    if friendly_name:
        needles.add(friendly_name.lower())
    if entity_id and "." in entity_id:
        needles.add(entity_id.split(".", 1)[1].replace("_", " ").lower())
    if not needles:
        return False
    for ann in list(recent_announcements or [])[-6:]:
        a = str(ann).lower()
        if any(n in a for n in needles):
            return True
    return False


def security_relevant(domain: str, device_class: str, to_state: str, entity_id: str) -> bool:
    s = str(to_state or "").lower()
    if s not in OPENING_STATES:
        return False
    if (device_class or "").lower() in SECURITY_CLASSES:
        return True
    if (domain or "").lower() in SECURITY_DOMAINS:
        return True
    eid = (entity_id or "").lower()
    return any(k in eid for k in ("door", "window", "garage", "lock", "gate"))


def evaluate_local_mind(s: EventSnapshot, *, history: dict, prior: tuple[int, int],
                        flapping: bool, hour: int, flap_count: int = 3,
                        flap_window_s: float = 300.0) -> Decision:
    """The Local Mind's decision procedure, given the history grade, the case
    prior and the flap flag the coordinator collected. Pure."""
    urgency = (s.urgency or "medium").lower()
    grade = history.get("grade", "unknown")
    away = not s.anyone_home
    duplicate = is_duplicate(s.friendly_name, s.entity_id, s.recent_announcements)
    security = security_relevant(s.domain, s.device_class, s.to_state, s.entity_id)
    speak_n, silent_n = prior
    ev = [s.state_evidence("event")]
    if history.get("grade") not in (None, "unknown"):
        ev.append(Evidence(SOURCE_HISTORY, "history", entity_id=s.entity_id,
                           trust=TRUST_DERIVED))
    if speak_n or silent_n:
        ev.append(Evidence(SOURCE_CASE_MEMORY, "case_memory", entity_id=s.entity_id,
                           trust=TRUST_DERIVED))

    def decision(do_speak: bool, out_urgency: str, why: str, escalated: bool = False):
        if not do_speak:
            return silent(R_LOCAL_MIND, f"local mind: {why}", urgency=out_urgency,
                          evaluator="local_mind", evidence=tuple(ev), origin=ORIGIN_LOCAL_MIND)
        return speak(R_LOCAL_MIND, out_urgency,
                     Phrase.of("lm_full", name=s.friendly_name, entity_id=s.entity_id,
                               to_state=s.to_state, hour=int(hour), novelty=grade,
                               away=away, escalated=escalated, device_class=s.device_class),
                     reason=f"local mind: {why}", evaluator="local_mind",
                     evidence=tuple(ev), origin=ORIGIN_LOCAL_MIND)

    # Critical always surfaces — even repetition is worth hearing at critical.
    if urgency == "critical":
        return decision(True, "critical", "critical urgency — always voiced", escalated=True)
    if duplicate:
        return decision(False, urgency, "already announced this device recently")
    if flapping:
        return decision(False, urgency,
                        f"{s.friendly_name or s.entity_id} is flapping "
                        f"(≥{flap_count} events in {int(flap_window_s / 60)}m) — suppressed")
    # An entry point opening while the house is empty outranks 'medium'.
    if security and away and urgency in ("medium", "high"):
        return decision(True, "high", f"entry point active while away ({grade}) — escalated",
                        escalated=True)
    if urgency == "high":
        return decision(True, "high", f"high urgency ({grade})", escalated=True)
    if urgency == "medium":
        # Case-based memory first — actual past provider judgments outrank heuristics.
        if silent_n >= 2 and speak_n == 0:
            return decision(False, "medium",
                            f"{silent_n} similar past events judged routine (case memory)")
        if speak_n >= 2 and silent_n == 0:
            return decision(True, "medium",
                            f"{speak_n} similar past events voiced (case memory)")
        if grade == "novel":
            return decision(True, "medium", "novel event — never observed before")
        if grade == "unusual_hour":
            return decision(True, "medium",
                            f"out of hourly pattern (seen {history.get('total', 0)}× "
                            f"overall, never near this hour)")
        if grade in ("routine", "common"):
            return decision(False, "medium",
                            f"{grade} at this hour "
                            f"(~{history.get('at_hour', 0)}× in {history.get('days', 0)}d)")
        # occasional / unknown: quiet at home, surfaced when away (routes to push).
        if away:
            return decision(True, "medium", f"{grade} while away — surfaced")
        return decision(False, "medium", f"{grade} while home — not worth voicing")
    return decision(False, "low", "low urgency — silent")


# ── 7. Learned-cache replay and the last-ditch fallback ────────────────────

def decision_from_cached(s: EventSnapshot, cached: dict, *, grade: str, hour: int) -> Decision:
    """Replay a learned provider verdict. The verdict comes from the cache;
    the words come from the Local Mind's composer."""
    if not cached.get("speak"):
        return silent(R_CACHE_SILENT, "learned: routine pattern (local cache)",
                      evaluator="cache", origin=ORIGIN_CACHE)
    urgency = cached.get("urgency", "medium")
    return speak(R_CACHE_SPEAK, urgency,
                 Phrase.of("lm_cached", name=s.friendly_name, entity_id=s.entity_id,
                           to_state=s.to_state, device_class=s.device_class, hour=int(hour),
                           novelty=grade, away=not s.anyone_home,
                           escalated=urgency in _URGENT, urgency=urgency),
                 reason="learned (local cache)", evaluator="cache", origin=ORIGIN_CACHE)


def evaluate_basic_fallback(s: EventSnapshot) -> Decision:
    """Deterministic decision when neither the provider nor the Local Mind
    can decide: urgent events speak, routine ones stay quiet."""
    if s.urgency in _URGENT:
        return speak(R_FALLBACK_URGENT, s.urgency,
                     Phrase.of("fallback", name=s.friendly_name, to_state=s.to_state),
                     reason="local fallback (cloud unavailable)", evaluator="fallback",
                     origin=ORIGIN_FALLBACK)
    return silent(R_FALLBACK_QUIET, "local fallback: non-urgent, cloud unavailable",
                  evaluator="fallback", origin=ORIGIN_FALLBACK)


# ── 8. Delivery rules (after a decision, before the output gate) ────────────

def cap_reasoned_urgency(reasoned: str, classifier_urgency: str) -> str:
    """Reasoning over-classifies as critical. Critical stands only when the
    classifier (which checks device_class against the canonical ceilings)
    also said critical; otherwise it becomes high, so a door or motion event
    can never take the critical bypass."""
    if reasoned == "critical" and classifier_urgency != "critical":
        return "high"
    return reasoned


def held_for_sleep(urgency: str, sleeping: bool) -> bool:
    """While someone is asleep only critical may be delivered."""
    return bool(sleeping) and urgency != "critical"


def is_silent(d: Decision) -> bool:
    return d.action == ACTION_SILENT
