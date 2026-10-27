"""Nova's local cognition: a free, on device stage that sees every state change.

It keeps a small model of each entity's normal rhythm and says which events
stand out enough to send to the cloud model. It only ever adds: the observer
ORs its verdict with the static pre-filter, so nothing that filter escalates
is lost. What it adds is the change the filter would miss, such as a device
that rarely changes, or changes at an hour it never does. A numeric reading
is never escalated on statistics alone, since a value spike is nothing Nova
should talk about. classifier_rate_limit still caps cloud use each hour.

The same model feeds anticipation: the normal state of openings and locks by
hour, daily routines ("usually happens by now"), people's departures and
arrivals, calendar leave times and learned per person routines.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import re
import time
from collections import deque, namedtuple
from typing import Optional

from .persistence import sqlite as _store

_LOGGER = logging.getLogger(__name__)

Decision = namedtuple("Decision", ["escalate", "salience", "reason"])

# ── salience ────────────────────────────────────────────────────────────────
MODEL_MAX_ENTITIES = 1500        # the stalest entity goes when full
TRANSITION_WINDOW = 50           # change times kept per entity
RARE_TRANSITIONS_PER_DAY = 3.0   # at most this many changes a day is rare
DEFAULT_THRESHOLD = 0.6          # salience that escalates
ANOMALY_COOLDOWN = 1800.0        # one statistical escalation per entity per 30 min

# Life safety device classes. "problem" is diagnostic (a stale backup, a
# failed update) and has its own lower tier; maintenance entities drop lower
# still, so housekeeping never reads as a security event.
SAFETY_CLASSES = {"smoke", "gas", "carbon_monoxide", "moisture", "safety", "tamper"}
_MAINTENANCE_HINTS = ("backup", "stale", "update", "snapshot", "certificat", "reboot",
                      "restart_required", "hacs", "supervisor", "firmware", "addon",
                      "config_entry")
SECURITY_DOMAINS = {"lock", "alarm_control_panel"}
OPENING_CLASSES = {"door", "window", "garage_door", "opening"}
# Only the active state is an emergency: "on" for a safety sensor, triggered
# or pending for an alarm. Unavailable, unknown or back to normal never is.
_ALARM_ACTIVE = {"triggered", "pending"}
_IMPORTANT_REASONS = ("safety/security trigger", "access-point", "device problem reported")

# ── anticipation ────────────────────────────────────────────────────────────
# The normal state of openings and locks by hour.
OCCUPANCY_DOMAINS = {"lock", "cover", "alarm_control_panel"}
OCCUPANCY_BINARY_CLASSES = {"door", "window", "garage_door", "opening", "garage", "lock"}
OCC_SAMPLE_INTERVAL = 900        # seconds between samples
OCC_MIN_SAMPLES_PER_HOUR = 20    # samples before an hour is trusted (about five days)
OCC_UNUSUAL_SHARE = 0.20         # a state seen less than this share of the hour is unusual
OCC_MIN_STILLNESS = 600          # held at least ten minutes, so not passing
PREDICT_COOLDOWN = 3600          # the same entity at most hourly
_DEAD_STATES = {"unknown", "unavailable", "none", "", None}
_OCC_DECAY_AT = 50000

# Daily events ("usually happened by now").
RECUR_DOMAINS = {"binary_sensor", "lock", "cover", "alarm_control_panel"}
RECUR_MIN_DAYS = 7               # a week of daily points at least
RECUR_MAX_STD = 2700             # within 45 minutes is a routine
RECUR_TOL_MIN = 1800             # grace past the usual time: 30 minutes at least
RECUR_TOL_MAX = 3600             # and an hour at most
RECUR_ALERT_KEEP_DAYS = 2        # the saved ledger keeps today and yesterday
RECUR_ALERT_MAX = 512            # and never more entries than this
_DAILY_KEEP = 45
_ROUTINE_MIN_SHARE = 0.6         # on at least this share of the days in its span

# People.
PRESENCE_DOMAINS = {"person", "device_tracker"}
DEPART_HISTORY = 240             # stable departures kept, about 60 days
DEFAULT_ROUTINE_DEPART_LEAD_MIN = 15
# A tracker state must hold this long to count. Router and Bluetooth trackers
# flap with the Wi-Fi; GPS ones report off the network and are trusted sooner.
PRESENCE_DEBOUNCE = 300
PRESENCE_DEBOUNCE_GPS = 60
APPROACH_OUTER_KM = 5.0          # closing in within this is heading home
APPROACH_MIN_CLOSE_KM = 0.05     # by at least 50 m since the last reading

# Once a day ledgers and per trip state. _RECUR_ALERTED (key to local day
# ordinal) is saved in patterns.db.
_PREDICT_COOLDOWNS: dict = {}
_RECUR_ALERTED: dict = {}
_LAST_DIST: dict = {}            # entity id to last distance from home, km
_APPROACH_ALERTED: dict = {}     # entity id to flagged on this trip
_LAST_ANOMALY: dict = {}         # entity id to last statistical escalation
# When continuous watching began (0: not watching). Cleared whenever the
# observer stops or cognition is off, so "none yet today" is only said about
# a stretch Nova really saw.
_OBSERVING_SINCE = 0.0
_EVENTS_SEEN = 0
_ANOMALIES_ESCALATED = 0


class _Entry:
    """What is kept about one entity. Small and bounded."""

    __slots__ = ("last_state", "last_changed", "first_seen", "transitions", "n", "mean", "m2",
                 "hours", "occ", "daily_first", "last_first_day", "depart_first",
                 "return_first", "pres_depart_day", "pres_return_day", "last_depart_ts",
                 "home_since_depart")

    def __init__(self, now: float):
        self.first_seen = self.last_changed = now
        self.last_state = ""
        self.transitions: deque = deque(maxlen=TRANSITION_WINDOW)
        self.hours = [0] * 24                  # changes per local hour
        self.occ: dict = {}                    # hour to {state: samples}
        # Time of day of the first change on each day, for daily routines.
        self.daily_first: deque = deque(maxlen=_DAILY_KEEP)
        self.last_first_day = 0
        # Every debounced departure (the name is older than that) and the
        # latest debounced arrival of each day.
        self.depart_first: deque = deque(maxlen=DEPART_HISTORY)
        self.return_first: deque = deque(maxlen=_DAILY_KEEP)
        self.pres_depart_day = self.pres_return_day = 0
        self.last_depart_ts = 0.0
        # Seen home since the last departure. A restart gives every state a
        # new last_changed, and without this would look like another departure.
        self.home_since_depart = True


_MODEL: dict[str, _Entry] = {}


def reset() -> None:
    """Forget everything learned (the observer restarted)."""
    global _EVENTS_SEEN, _ANOMALIES_ESCALATED, _OBSERVING_SINCE
    for table in (_MODEL, _PREDICT_COOLDOWNS, _RECUR_ALERTED, _LAST_DIST, _APPROACH_ALERTED,
                  _LAST_ANOMALY):
        table.clear()
    _EVENTS_SEEN = _ANOMALIES_ESCALATED = 0
    _OBSERVING_SINCE = 0.0


def mark_unobserved() -> None:
    """Note a gap in watching (observer stopped, cognition off)."""
    global _OBSERVING_SINCE
    _OBSERVING_SINCE = 0.0


def _local_day(now: float) -> int:
    """The local calendar day as an ordinal: one per day, and in order."""
    return datetime.datetime.fromtimestamp(now).toordinal()


def _secs_since_midnight(now: float) -> int:
    clock = time.localtime(now)
    return (clock.tm_hour * 60 + clock.tm_min) * 60 + clock.tm_sec


def _evict_if_full() -> None:
    if len(_MODEL) >= MODEL_MAX_ENTITIES:
        stalest = min(_MODEL, key=lambda eid: _MODEL[eid].last_changed)
        _MODEL.pop(stalest, None)


def _entry_for(entity_id: str, now: float, state=None) -> _Entry:
    """The entity's entry, made (with its state, when given) if new."""
    entry = _MODEL.get(entity_id)
    if entry is None:
        _evict_if_full()
        entry = _Entry(now)
        if state is not None:
            entry.last_state = state
        _MODEL[entity_id] = entry
    return entry


def observe(entity_id: str, old_state, new_value, now: float) -> _Entry:
    """Learn from one state change. Every event comes here, telemetry the
    static filter drops included. Returns the updated entry."""
    entry = _entry_for(entity_id, now)
    if new_value != entry.last_state:
        entry.transitions.append(now)
        entry.hours[time.localtime(now).tm_hour] += 1
        entry.last_changed = now
        day = _local_day(now)
        if entry.last_first_day != day:
            entry.last_first_day = day
            entry.daily_first.append((day, _secs_since_midnight(now)))
    if new_value is not None:
        entry.last_state = new_value
    return entry


def _is_rare(entry: _Entry, now: float) -> bool:
    """Whether the entity averages no more than RARE_TRANSITIONS_PER_DAY
    changes a day over the time it has been known."""
    seen = len(entry.transitions)
    if seen < 2:
        return False
    days = max(now - entry.first_seen, 1.0) / 86400.0
    return seen / days <= RARE_TRANSITIONS_PER_DAY


def _is_unusual_hour(entry: _Entry, now: float) -> bool:
    """Whether this hour holds under 2% of the entity's changes, once it has
    twenty to judge by."""
    total = sum(entry.hours)
    if total < 20:
        return False
    return entry.hours[time.localtime(now).tm_hour] / total < 0.02


def _is_safety_trigger(domain, dclass, new_value) -> bool:
    """Whether a safety sensor or alarm has just entered its active state. A
    connection blip or a return to normal never counts."""
    value = "" if new_value is None else str(new_value).lower()
    if value in ("", "unavailable", "unknown", "none"):
        return False
    if dclass in SAFETY_CLASSES:
        return value == "on"
    return domain == "alarm_control_panel" and value in _ALARM_ACTIVE


def _base_salience(domain, dclass, new_value, entity_id: str) -> tuple:
    if _is_safety_trigger(domain, dclass, new_value):
        return 0.9, "safety/security trigger"
    if dclass == "problem" and str(new_value).lower() == "on":
        lowered = (entity_id or "").lower()
        if any(hint in lowered for hint in _MAINTENANCE_HINTS):
            return 0.15, "system maintenance — informational"
        return 0.4, "device problem reported"
    if domain == "cover" or dclass in OPENING_CLASSES or domain in SECURITY_DOMAINS:
        return 0.55, "access-point"
    return 0.0, ""


def _salience(domain, dclass, new_value, entry, now, entity_id="") -> tuple:
    """A salience in [0, 1] and a short reason, from local heuristics."""
    score, why = _base_salience(domain, dclass, new_value, entity_id)
    reasons = [why] if why else []
    for applies, extra, said in ((_is_rare, 0.15, "rare transition"),
                                 (_is_unusual_hour, 0.10, "unusual hour")):
        if applies(entry, now):
            score = min(1.0, score + extra)
            reasons.append(said)
    return score, ", ".join(reasons) or "routine"


def process(event, threshold: float = DEFAULT_THRESHOLD) -> Decision:
    """Learn from an event and say whether it stands out enough to escalate.
    Safe on every state change; escalate only ever adds to what the static
    filter sends."""
    global _EVENTS_SEEN, _ANOMALIES_ESCALATED, _OBSERVING_SINCE
    _EVENTS_SEEN += 1
    if not _OBSERVING_SINCE:
        _OBSERVING_SINCE = time.time()
    try:
        data = event.data
        entity_id = data.get("entity_id", "")
        new_state, old_state = data.get("new_state"), data.get("old_state")
        value = new_state.state if new_state else None
        dclass = new_state.attributes.get("device_class") if new_state else None
        domain = entity_id.split(".", 1)[0] if entity_id else ""
        now = time.time()
        # An opening or lock back from unknown or unavailable did not move:
        # never escalated, never learned as a change.
        from .cognitive.evaluators import is_entry_state_recovery
        before = old_state.state if old_state else "unknown"
        if is_entry_state_recovery(entity_id, dclass or "", before, value):
            if entity_id in _MODEL:
                _MODEL[entity_id].last_state = value
            return Decision(escalate=False, salience=0.0, reason="state recovery")
        entry = observe(entity_id, old_state, value, now)
        score, reason = _salience(domain, dclass, value, entry, now, entity_id)
        escalate = score >= threshold
        # A purely statistical escalation waits out a cooldown per entity, so
        # one noisy sensor cannot flood the pipeline. Safety, access and
        # device problems always pass.
        if escalate and not any(word in reason for word in _IMPORTANT_REASONS):
            escalate = now - _LAST_ANOMALY.get(entity_id, 0.0) >= ANOMALY_COOLDOWN
            if escalate:
                _LAST_ANOMALY[entity_id] = now
        _ANOMALIES_ESCALATED += int(escalate)
        return Decision(escalate, round(score, 3), reason)
    except Exception as exc:
        _LOGGER.debug("cognition.process error: %s", exc)
        return Decision(False, 0.0, "error")


def stats() -> dict:
    """Counts for the panel, learning progress included."""
    predictable = routines = presence_routines = 0
    for eid, entry in _MODEL.items():
        if any(sum(hour.values()) >= OCC_MIN_SAMPLES_PER_HOUR for hour in entry.occ.values()):
            predictable += 1
        if eid.split(".", 1)[0] not in PRESENCE_DOMAINS:
            routines += _routine_of(entry) is not None
            continue
        from .cognitive import presence_routines as pr
        today = _local_day(time.time())
        for kind in (pr.WEEKDAY, pr.WEEKEND):
            presence_routines += len(pr.departures(entry.depart_first, today, kind))
        if _routine_of_pts(list(entry.return_first)):
            presence_routines += 1
    return {"entities_tracked": len(_MODEL), "events_seen": _EVENTS_SEEN,
            "anomalies_escalated": _ANOMALIES_ESCALATED, "predictable": predictable,
            "routines": routines, "presence_routines": presence_routines}


# ── the normal state by hour ────────────────────────────────────────────────

_OPENING_WORDS = {"on": "open", "off": "closed"}
_OPENING_LIKE = {"door", "window", "garage_door", "opening", "garage"}


def _humanize(state, dclass) -> str:
    """A state as it is said: an opening's on and off become open and closed."""
    text = str(state)
    if dclass in _OPENING_LIKE:
        return _OPENING_WORDS.get(text, text)
    return text.replace("_", " ")


def _occupancy_candidates(hass):
    """Entities with a meaningful normal state by hour."""
    found = []
    for domain in OCCUPANCY_DOMAINS:
        try:
            found.extend(hass.states.async_all(domain))
        except Exception:
            pass
    try:
        found.extend(st for st in hass.states.async_all("binary_sensor")
                     if st.attributes.get("device_class") in OCCUPANCY_BINARY_CLASSES)
    except Exception:
        pass
    return found


def sample_occupancy(hass, now: float = None) -> int:
    """Count each candidate's current state against this hour. Run about every
    15 minutes, so over days it learns "the garage is closed at this hour 95%
    of the time". Returns how many were sampled."""
    now = now or time.time()
    hour = time.localtime(now).tm_hour
    count = 0
    try:
        for st in _occupancy_candidates(hass):
            value = st.state
            if value in _DEAD_STATES:
                continue
            tally = _entry_for(st.entity_id, now, value).occ.setdefault(hour, {})
            tally[value] = tally.get(value, 0) + 1
            if tally[value] > _OCC_DECAY_AT:
                for state in list(tally):
                    tally[state] //= 2
            count += 1
    except Exception as exc:
        _LOGGER.debug("sample_occupancy error: %s", exc)
    return count


# Covers that are real openings. Blinds, curtains, shades, shutters, awnings
# and dampers dress a window; an open blind is no security matter.
_SECURITY_COVER_CLASSES = {"door", "garage", "garage_door", "gate", "window"}
_SECURE_BINARY_CLASSES = {"door", "window", "garage_door", "opening", "garage"}
_LOCK_SIDES = {"locked": True, "unlocked": False}
_COVER_SIDES = {"closed": True, "closing": True, "open": False, "opening": False}


def _security_direction(domain: str, dclass, state) -> Optional[bool]:
    """True for the secure side of an entity's state, False for the less
    secure side, None when it cannot be told. Callers treat None like True:
    guessing "less secure" wrongly is the very mistake this prevents."""
    value = str(state).lower()
    if domain == "lock":
        return _LOCK_SIDES.get(value)
    if domain == "alarm_control_panel":
        if value.startswith("armed"):
            return True
        return False if value == "disarmed" else None
    if domain == "cover":
        return _COVER_SIDES.get(value) if dclass in _SECURITY_COVER_CLASSES else None
    if domain == "binary_sensor" and value in ("on", "off") and (
            dclass in _SECURE_BINARY_CLASSES or dclass == "lock"):
        # off is closed or locked
        return value == "off"
    return None


def _all_tracked_residents_away(hass) -> bool:
    """Every person positively away (household.py): none home, none unknown,
    no linked tracker home. No person entities, or only stray trackers (a
    car, a courier's phone), is not away."""
    try:
        from . import household
        return household.residents(hass, people_only=True) == household.AWAY
    except Exception:
        return False


def _conflicting_armed_alarm(hass) -> bool:
    """An alarm armed away or on vacation: nobody should be opening anything.
    Armed home or night is left out, since residents open windows then."""
    from . import household
    return household.snapshot(hass).posture == household.AWAY


def _deviation(hass, eid: str, entry: _Entry, hour: int, now: float):
    """The current state when it is unusual for the hour, settled, off
    cooldown and not the usual one; else None."""
    tally = entry.occ.get(hour)
    if not tally:
        return None
    total = sum(tally.values())
    if total < OCC_MIN_SAMPLES_PER_HOUR:
        return None
    st = hass.states.get(eid)
    if st is None or st.state in _DEAD_STATES:
        return None
    current = st.state
    if tally.get(current, 0) / total >= OCC_UNUSUAL_SHARE:
        return None
    if now - entry.last_changed < OCC_MIN_STILLNESS:
        return None
    if now - _PREDICT_COOLDOWNS.get(eid, 0.0) < PREDICT_COOLDOWN:
        return None
    if max(tally, key=tally.get) == current:
        return None
    return st


def predict(hass, now: float = None) -> list:
    """Openings and locks left in a less secure state that is unusual for the
    hour, settled, and backed by a separate reason to speak.

    A habit is not an expectation: an unusual open window is a choice, not
    something to interrupt anyone about. So nothing is flagged when the state
    is the secure one (closed, locked, armed) however unusual, nor when it is
    less secure with nothing to back it up; sample_occupancy keeps learning
    either way. The only backing that counts is everyone positively away, or
    an alarm armed away or on vacation. Missing presence tracking does not,
    nor "usually" alone, nor an alarm armed home or night.
    """
    now = now or time.time()
    hour = time.localtime(now).tm_hour
    flagged = []
    try:
        for eid, entry in list(_MODEL.items()):
            st = _deviation(hass, eid, entry, hour, now)
            if st is None:
                continue
            dclass = st.attributes.get("device_class")
            domain = eid.split(".", 1)[0]
            if _security_direction(domain, dclass, st.state) is not False:
                continue
            if _all_tracked_residents_away(hass):
                because = "nobody appears to be home"
            elif _conflicting_armed_alarm(hass):
                because = "the alarm is armed"
            else:
                continue
            _PREDICT_COOLDOWNS[eid] = now
            name = st.attributes.get("friendly_name", eid)
            flagged.append({
                "type": "anticipation",
                "urgency": "medium" if domain in ("lock", "alarm_control_panel", "cover") else "low",
                "message": f"{name} is {_humanize(st.state, dclass)} while {because}.",
                "pattern_key": f"anticipate:{eid}",
                # Lets habituation see a lock, cover or alarm and never quiet it.
                "entity_id": eid,
                "offer": False,
            })
    except Exception as exc:
        _LOGGER.debug("predict error: %s", exc)
    return flagged


# ── daily routines ──────────────────────────────────────────────────────────

def _adaptive_tol_scale() -> float:
    """adaptive_awareness's learned grace multiplier; 1.0 when off."""
    try:
        from . import adaptive_awareness
        return adaptive_awareness.tolerance_scale()
    except Exception:
        return 1.0


def _adaptive_extra_days() -> int:
    """adaptive_awareness's extra days before a routine is trusted; 0 when off."""
    try:
        from . import adaptive_awareness
        return adaptive_awareness.extra_min_days()
    except Exception:
        return 0


def _routine_of_pts(pts):
    """(mean, spread) in seconds of the day for (day, seconds) points that
    form a routine: enough days, a tight time, and most days in their span.
    None otherwise."""
    if len(pts) < RECUR_MIN_DAYS + _adaptive_extra_days():
        return None
    times = [secs for _day, secs in pts]
    mean = sum(times) / len(times)
    spread = (sum((t - mean) ** 2 for t in times) / len(times)) ** 0.5
    if spread > RECUR_MAX_STD:
        return None
    days = [day for day, _secs in pts]
    span = max(days) - min(days) + 1
    if span <= 0 or len(pts) / span < _ROUTINE_MIN_SHARE:
        return None
    return mean, spread


def _routine_of(entry: _Entry):
    """The entity's daily routine, if it has a steady, near daily one. Steady
    but rare events do not count, which is what keeps "usually by now" safe."""
    return _routine_of_pts(list(entry.daily_first))


def _hhmm(secs: float) -> str:
    hours, rest = divmod(secs, 3600)
    return f"{int(hours):02d}:{int(rest // 60):02d}"


def _grace(spread: float) -> float:
    """How long past the usual time before it is overdue."""
    return min(max(2 * spread, RECUR_TOL_MIN), RECUR_TOL_MAX) * _adaptive_tol_scale()


def _log_decision(kind, observation, interpretation, decision, reason, confidence=None,
                  ref=None):
    """Write a Decision Record entry and return its id (None if that fails, which
    never stops the decision), so the alert can carry it to be rated."""
    try:
        from . import decision_record
        return decision_record.record(kind, observation=observation,
                                      interpretation=interpretation, decision=decision,
                                      reason=reason, confidence=confidence, ref=ref)
    except Exception:
        return None


def _local_midnight(now: float) -> float:
    day_start = datetime.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0,
                                                              microsecond=0)
    return day_start.timestamp()


def predict_overdue(hass, now: float = None) -> list:
    """Daily events that have not happened by their usual time plus grace
    ("the front door usually has activity by 07:45, none yet today"), once per
    entity per day.

    "None yet today" is only said when Nova has watched since local midnight;
    after a start, a reload or a gap the morning is unknown, so nothing is.
    """
    now = now or time.time()
    flagged = []
    if not _OBSERVING_SINCE or _OBSERVING_SINCE > _local_midnight(now):
        return flagged
    today = _local_day(now)
    clock = _secs_since_midnight(now)
    try:
        for eid, entry in list(_MODEL.items()):
            routine = _routine_of(entry) if eid.split(".", 1)[0] in RECUR_DOMAINS else None
            if routine is None:
                continue
            usual_secs, spread = routine
            if clock <= usual_secs + _grace(spread):
                continue
            if entry.last_first_day == today or _RECUR_ALERTED.get(eid) == today:
                continue
            _RECUR_ALERTED[eid] = today
            st = hass.states.get(eid)
            name = st.attributes.get("friendly_name", eid) if st else eid
            usual = _hhmm(usual_secs)
            decision_id = _log_decision(
                "anticipation_overdue", {"entity": name, "entity_id": eid,
                                         "usual_active_by": usual},
                {"predicted": "no activity yet, past the usual time"},
                "flag overdue activity", "recurring daily activity pattern",
                ref=f"entity:{eid}")
            flagged.append({
                "type": "anticipation_overdue", "urgency": "low",
                "message": (f"{name} usually has activity by around {usual}, but there's "
                            "been none yet today — thought it worth flagging."),
                "pattern_key": f"overdue:{eid}", "offer": False, "decision_id": decision_id,
            })
    except Exception as exc:
        _LOGGER.debug("predict_overdue error: %s", exc)
    return flagged


# ── people ──────────────────────────────────────────────────────────────────

def _is_home(state) -> bool:
    return state == "home"


def _is_away(state) -> bool:
    return state != "home" and state not in _DEAD_STATES


def _last_changed_ts(st):
    """When the state last changed, as a timestamp; None if unreadable."""
    try:
        changed = st.last_changed
        return changed.timestamp()
    except Exception:
        return None


def _source_type(st) -> str:
    return (st.attributes.get("source_type") or "").lower()


def _entity_coords(st):
    point = (st.attributes.get("latitude"), st.attributes.get("longitude"))
    if None in point:
        return None
    try:
        return float(point[0]), float(point[1])
    except (TypeError, ValueError):
        return None


def _is_gps(st) -> bool:
    """Location aware (works off the network): marked gps or carrying
    coordinates, which covers a person backed by a GPS tracker."""
    return _source_type(st) == "gps" or _entity_coords(st) is not None


def _debounce_for(st) -> int:
    return PRESENCE_DEBOUNCE_GPS if _is_gps(st) else PRESENCE_DEBOUNCE


def _home_coords(hass):
    zone = hass.states.get("zone.home")
    point = _entity_coords(zone) if zone is not None else None
    if point:
        return point
    try:
        return float(hass.config.latitude), float(hass.config.longitude)
    except Exception:
        return None


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    sin_dphi = math.sin(math.radians(lat2 - lat1) / 2)
    sin_dlam = math.sin(math.radians(lon2 - lon1) / 2)
    h = sin_dphi ** 2 + math.cos(phi1) * math.cos(phi2) * sin_dlam ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def distance_home_km(hass, st):
    """Distance in km from the entity's coordinates to home, or None."""
    home, here = _home_coords(hass), _entity_coords(st)
    if not (home and here):
        return None
    return _haversine_km(here[0], here[1], home[0], home[1])


def _routine_presence_domains(hass) -> set:
    """People's comings and goings are routines. Device trackers count only
    in a home with no person entities: a phone, router or NAS tracker is not
    someone leaving."""
    try:
        if hass.states.async_all("person"):
            return {"person"}
    except Exception:
        pass
    return {"device_tracker"}


def _routine_presence_entities(hass):
    found = []
    for domain in _routine_presence_domains(hass):
        try:
            found.extend(hass.states.async_all(domain))
        except Exception:
            pass
    return found


def _note_departure(entry: _Entry, ts: float, secs: int, today: int) -> None:
    """A stable departure not yet recorded, after being seen home."""
    if ts == entry.last_depart_ts or not entry.home_since_depart:
        return
    entry.last_depart_ts, entry.home_since_depart = ts, False
    point = (_local_day(ts), secs)
    if not entry.depart_first or tuple(entry.depart_first[-1]) != point:
        entry.depart_first.append(point)
    entry.pres_depart_day = today


def _note_arrival(entry: _Entry, secs: int, today: int) -> None:
    """Home again. On a day they left, keep the latest arrival."""
    entry.home_since_depart = True
    if entry.pres_depart_day != today:
        return
    arrivals = entry.return_first
    if entry.pres_return_day == today and arrivals and arrivals[-1][0] == today:
        arrivals[-1] = (today, secs)
    else:
        entry.pres_return_day = today
        arrivals.append((today, secs))


def sample_presence(hass, now: float = None) -> int:
    """Record people's debounced departures and arrivals (device trackers only
    when there are no people). A state counts once it has held the tracker's
    debounce, so flaps never reach a routine. Each stable departure is kept
    once as (day, time); the arrival is the day's latest stable return, on
    days they left. Run periodically; returns how many were settled."""
    now = now or time.time()
    today = _local_day(now)
    settled = 0
    try:
        for st in _routine_presence_entities(hass):
            state = st.state
            if state in _DEAD_STATES:
                continue
            entry = _entry_for(st.entity_id, now, state)
            since = _last_changed_ts(st)
            if since is None or now - since < _debounce_for(st):
                continue
            secs = _secs_since_midnight(since)
            settled += 1
            if _is_away(state):
                _note_departure(entry, since, secs, today)
            elif _is_home(state):
                _note_arrival(entry, secs, today)
    except Exception as exc:
        _LOGGER.debug("sample_presence error: %s", exc)
    return settled


# ── leave times for calendar events ─────────────────────────────────────────
DEFAULT_DEPART_LEAD_MIN = 30     # lead when no travel time can be had
DEPART_PREP_BUFFER_MIN = 5       # added to a travel time
DEPART_LOOKAHEAD_H = 3           # only events starting within this many hours
DEPART_ROUTE_TTL = 1800          # a drive time is reused this long (s)
DEPART_RETRY_AFTER = 300         # a failed lookup is retried after this (s)
DEPART_WALK_TTL = 6 * 3600       # walking barely changes (s)
DEPART_TRANSIT_TTL = 2400        # public transport keeps to a timetable (s)
DEPART_MERGE_MIN = 3             # leave times this close read as one
DEPART_TRANSIT_SAME_PCT = 0.05   # transit within 5% (or 2 min) of walking is walking
DEPART_MAX_WALK_MIN = 60         # beyond this it is no walk
DEPART_MODE_ORDER = ("walk", "transit", "drive")
DEPART_MODE_LABEL = {"walk": "walking", "transit": "taking public transport",
                     "drive": "driving", "any": "heading out"}
_TRAVEL_KEEP = 6 * 3600 + 60     # cached travel times older than this are dropped
_DEPART_TRAVEL: dict = {}        # lookup key to (minutes or None, reason, asked at)
_DEPART_STAGES: dict = {}        # event key to the later leave times still to come

# Not somewhere to travel to: a link, a call, "online", a placeholder. With
# the home's own names, an empty location and a bare phone number, these get
# no leave alert. Whole words, any case.
_NOT_A_PLACE_RE = re.compile(
    r"https?://|\bwww\.|\.(?:com|net|org|io|ie|co\.uk)/|\b(?:"
    r"zoom|teams|meet|webex|skype|facetime|whereby|jitsi|gotomeeting|"
    r"online|virtual|remote|video\s*call|phone\s*call|conference\s*call|"
    r"call|phone|dial\s*-?\s*in|tbc|tbd|tba|n/a|"
    r"to\s+be\s+(?:confirmed|arranged|decided))\b",
    re.IGNORECASE)
_HOME_WORDS = {"home", "at home", "my home", "my house", "house", "the house", "here"}


def _home_names(hass) -> tuple:
    """Home Assistant's location name and the home zone's friendly name."""
    names = []
    for read in (lambda: getattr(hass.config, "location_name", ""),
                 lambda: (hass.states.get("zone.home").attributes.get("friendly_name")
                          if hass.states.get("zone.home") is not None else "")):
        try:
            names.append(str(read() or ""))
        except Exception:
            pass
    return tuple(name for name in names if name.strip())


def is_real_place(location, home_names=()) -> bool:
    """Whether an event's location is somewhere to travel to: an address or a
    place name. Not empty, a link, mostly a phone number, a call ("Zoom",
    "Teams", "online", "dial in"), a placeholder ("TBC") or the home. Pure
    and never raises."""
    if not isinstance(location, str) or not location.strip():
        return False
    text = location.strip()
    squeezed = " ".join(text.lower().split())
    own = {str(name).strip().lower() for name in home_names}
    if squeezed in _HOME_WORDS or squeezed in own:
        return False
    if _NOT_A_PLACE_RE.search(text):
        return False
    digits = sum(ch.isdigit() for ch in text)
    letters = sum(ch.isalpha() for ch in text)
    # Mostly digits is a phone number.
    return not (digits >= 6 and letters <= 2)


def _people_home(hass) -> set:
    """Who is home now: people, or trackers in a home with no people."""
    try:
        return {st.entity_id for st in _routine_presence_entities(hass) if _is_home(st.state)}
    except Exception:
        return set()


def _current_origin(hass):
    """Where the journey starts. A chosen origin entity always wins. Then home
    while anyone is in (a leave alert is about leaving the house, not about
    wherever the first person listed happens to be), then the first person
    with coordinates, then home."""
    try:
        from . import nova_config
        chosen = str(nova_config.get("departure_origin_entity", "") or "").strip()
        st = hass.states.get(chosen) if chosen else None
        point = _entity_coords(st) if st is not None else None
        if point:
            return point
        if _people_home(hass):
            home = _home_coords(hass)
            if home:
                return home
        for person in hass.states.async_all("person"):
            point = _entity_coords(person)
            if point:
                return point
    except Exception:
        pass
    return _home_coords(hass)


def _excluded_calendars(nova_config) -> set:
    """Calendars switched off for leave alerts (a JSON list from the panel)."""
    raw = nova_config.get("departure_excluded_calendars", []) or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            raw = []
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(calendar) for calendar in raw}


def _retry_after(reason, ttl) -> int:
    """How long a failed lookup rests. "No route" is an answer (there may be no
    public transport) and stands as long as a success; a Routes API that is
    off is retried slowly; anything else soon."""
    reason = reason or ""
    if reason.startswith("Google found no"):
        return ttl
    return 1800 if "not enabled" in reason else DEPART_RETRY_AFTER


async def _cached_lookup(key, now, ttl, fetch, what, warn=True):
    """Ask a service for a travel time once, then reuse it for ttl seconds (a
    failure for _retry_after). fetch() gives (minutes, reason). The check runs
    every tick, so this keeps each event from being looked up every 30
    seconds. Returns (minutes or None, why it is None)."""
    for stale in [k for k, held in _DEPART_TRAVEL.items() if now - held[2] > _TRAVEL_KEEP]:
        _DEPART_TRAVEL.pop(stale, None)
    held = _DEPART_TRAVEL.get(key)
    if held:
        minutes, reason, asked = held
        fresh_for = ttl if minutes is not None else _retry_after(reason, ttl)
        if now - asked < fresh_for:
            return minutes, reason
    minutes, reason = await fetch()
    if minutes is None:
        reason = reason or "no travel time came back"
        # Only a first failure is logged; a missing walk or transit route is
        # common and not worth a warning, the drive time is.
        if held is None:
            log = _LOGGER.warning if warn else _LOGGER.debug
            log("Departure alert: could not work out the travel time (%s): %s", what, reason)
    _DEPART_TRAVEL[key] = (minutes, reason, now)
    return minutes, reason


def _near(origin) -> tuple:
    return round(origin[0], 2), round(origin[1], 2)


async def _osrm_drive(hass, event_key, origin, loc, osrm_url, now):
    """A drive time from the open source router, the fallback with no key."""
    from . import travel

    async def ask():
        got = await travel.travel_minutes(hass, origin, loc, osrm_url)
        if got is not None:
            return got, None
        return None, travel.failure_reason(loc) or "no travel time came back"
    key = ("osrm", *event_key, *_near(origin))
    return await _cached_lookup(key, now, DEPART_ROUTE_TTL, ask,
                                "driving to %r, open-source router" % loc)


_GOOGLE_TTL = {"walk": DEPART_WALK_TTL, "drive": DEPART_ROUTE_TTL, "transit": DEPART_TRANSIT_TTL}


async def _google_minutes(hass, mode, event_key, origin, loc, start_dt, now):
    """One mode's minutes from Google Maps Travel Time, cached. Walking and
    driving are timed from now; public transport arrives by the event, so its
    answer belongs to that one event."""
    from . import google_travel
    transit = mode == "transit"
    key = ("google", mode, loc.strip().lower(), _near(origin))
    if transit:
        key += (event_key[1],)

    async def ask():
        return await google_travel.route_minutes(hass, mode, origin, loc, now=now,
                                                 arrive_by=start_dt if transit else None)
    return await _cached_lookup(key, now, _GOOGLE_TTL[mode], ask,
                                "%s to %r, Google" % (DEPART_MODE_LABEL[mode], loc),
                                warn=mode == "drive")


def _modes_enabled(nova_config) -> list:
    """The travel modes the owner wants in leave alerts, in order."""
    return [mode for mode in DEPART_MODE_ORDER
            if bool(nova_config.get(f"departure_mode_{mode}", True))]


async def _drive_minutes(hass, event_key, origin, loc, start_dt, osrm_url, sensor_min,
                         google_on, now):
    """(minutes or None, source, failure) for driving: the travel sensor, then
    Google, then the open source router."""
    if sensor_min is not None:
        return sensor_min, "sensor", None
    if not loc:
        return None, None, None
    if not origin:
        return None, None, "Nova does not know where you are"
    failure = None
    if google_on:
        got, failure = await _google_minutes(hass, "drive", event_key, origin, loc,
                                             start_dt, now)
        if got is not None:
            return got, "google", failure
    got, why = await _osrm_drive(hass, event_key, origin, loc, osrm_url, now)
    if got is not None:
        return got, "route", None
    return None, None, why or failure


async def _departure_plan(hass, nova_config, ev, event_key, origin, osrm_url,
                          sensor_min, lead_default, now):
    """({mode: minutes}, drive source, drive failure) for one event, leaving
    out modes with no answer; {"any": None} when there are none (the default
    lead then applies). Walking and public transport need Google Maps Travel
    Time and a place."""
    from . import google_travel
    modes = _modes_enabled(nova_config)
    loc, start_dt = ev.get("location"), ev["start"]
    google_on = (bool(nova_config.get("departure_use_google", True))
                 and google_travel.available(hass))
    minutes: dict = {}
    source = failure = None
    if "drive" in modes:
        got, source, failure = await _drive_minutes(hass, event_key, origin, loc, start_dt,
                                                    osrm_url, sensor_min, google_on, now)
        if got is not None:
            minutes["drive"] = got
    if loc and origin and google_on:
        for mode in ("walk", "transit"):
            if mode not in modes:
                continue
            got, _why = await _google_minutes(hass, mode, event_key, origin, loc, start_dt, now)
            if got is not None:
                minutes[mode] = got
        walk = minutes.get("walk")
        if walk is not None and walk > DEPART_MAX_WALK_MIN:
            minutes.pop("walk")
            walk = None
        transit = minutes.get("transit")
        # Transit about as long as the walk means Google found no real
        # service and answered with a walk.
        if None not in (walk, transit) and abs(transit - walk) <= max(
                2.0, DEPART_TRANSIT_SAME_PCT * walk):
            minutes.pop("transit")
    return (minutes or {"any": None}), source, failure


def _leave_groups(minutes: dict, start_dt, lead_default):
    """The leave time for each mode, earliest first, with modes leaving within
    DEPART_MERGE_MIN minutes of each other read as one. Each group is
    {"modes", "leave_at", "lead"}."""
    timed = []
    for mode in DEPART_MODE_ORDER + ("any",):
        if mode in minutes:
            got = minutes[mode]
            lead = lead_default if got is None else got + DEPART_PREP_BUFFER_MIN
            timed.append((start_dt - datetime.timedelta(minutes=lead), mode, lead))
    timed.sort(key=lambda row: row[0])
    merge = datetime.timedelta(minutes=DEPART_MERGE_MIN)
    groups: list = []
    for leave_at, mode, lead in timed:
        if groups and leave_at - groups[-1]["leave_at"] <= merge:
            groups[-1]["modes"].append(mode)
        else:
            groups.append({"modes": [mode], "leave_at": leave_at, "lead": lead})
    return groups


def _label(modes) -> str:
    return " or ".join(map(DEPART_MODE_LABEL.get, modes))


def _minutes_until(when, now_dt) -> int:
    return int(round((when - now_dt).total_seconds() / 60.0))


def _departure_message(title, loc_str, mins_to, groups, now_dt, lead_default, failed):
    """The heads up. A single drive (or unknown mode) leave time keeps the
    original wording; several are listed with how long until each."""
    opening = "Heads up — %s%s begins in about %d minutes" % (title, loc_str, mins_to)
    if len(groups) == 1 and groups[0]["modes"] in (["drive"], ["any"]):
        said = f"{opening}; you'll want to head out."
    else:
        clauses = []
        for group in groups:
            wait, how = _minutes_until(group["leave_at"], now_dt), _label(group["modes"])
            if wait <= 1:
                clauses.append(f"head out now if you're {how}")
            else:
                clauses.append(f"leave in {wait} minutes if you're {how}")
        listed = clauses[0] if len(clauses) == 1 else (
            ", ".join(clauses[:-1]) + ", or " + clauses[-1])
        said = f"{opening}. {listed[0].upper()}{listed[1:]}."
    if failed:
        said += (" I couldn't work out the travel time, so I used the usual %d minutes."
                 % lead_default)
    return said


def _still_home(snapshot, home_now: set) -> bool:
    """Everyone home at the first alert is still home. With no snapshot (the
    first alert came before a restart), anyone home will do."""
    if snapshot is None:
        return bool(home_now)
    return bool(snapshot) and snapshot <= home_now


def _departure_followups(hass, now, now_dt, today):
    """The later leave times of an event already announced: each is reminded
    about only if everyone home at the first alert is still home, and is
    marked done either way so it never comes twice. At most one action."""
    for key, stage in list(_DEPART_STAGES.items()):
        if stage["start"] <= now_dt:
            _DEPART_STAGES.pop(key, None)
            continue
        for idx, group in enumerate(stage["groups"]):
            if not idx or idx in stage["done"] or group["leave_at"] > now_dt:
                continue
            stage["done"].add(idx)
            ledger = f"{key}:{idx}"
            # The key names the event and its start, so any entry means done:
            # checking the day would repeat it once the date turns at midnight.
            if ledger in _RECUR_ALERTED:
                continue
            _RECUR_ALERTED[ledger] = today
            if not _still_home(stage["home"], _people_home(hass)):
                continue
            mins_to = max(0, int((stage["start"] - now_dt).total_seconds() // 60))
            decision_id = _log_decision(
                "anticipation_departure",
                {"event": stage["title"], "minutes_until": mins_to, "modes": group["modes"],
                 "everyone_still_home": True},
                {"predicted": "still at home when it is time to leave"},
                "remind about the later leave time",
                "everyone home at the first alert is still home")
            text = (f"{stage['title']} begins in about {mins_to} minutes and everyone is "
                    f"still home. If you're {_label(group['modes'])}, it's time to leave.")
            return [{"type": "anticipation_departure", "urgency": "low", "message": text,
                     "pattern_key": ledger, "offer": False, "decision_id": decision_id}]
    return []


def _travel_sensor_minutes(hass, nova_config):
    """The optional travel time sensor's minutes (driving), or None."""
    entity = str(nova_config.get("departure_travel_sensor", "") or "").strip()
    if not entity or hass is None:
        return None
    st = hass.states.get(entity)
    if st is None or st.state in _DEAD_STATES:
        return None
    try:
        return float(st.state)
    except Exception:
        return None


_LEAD_REASONS = {"sensor": "travel sensor reading", "route": "estimated drive time",
                 "google": "Google travel times", "modes": "travel times by mode",
                 "default": "default lead time"}


async def predict_departure(hass, now: float = None) -> list:
    """Leave time anticipation: once, when it is time to head out for the
    nearest timed calendar event.

    The heads up covers each enabled travel mode Nova can time (walking,
    public transport, driving), with how long until each must leave; modes
    leaving within a few minutes read as one. Walking and public transport
    come from Google Maps Travel Time (google_travel). Driving comes from a
    travel sensor, else Google, else open source routing (travel), else the
    default lead, which the alert admits when a lookup failed. Each later
    leave time is reminded about only if everyone home at the first alert is
    still home. Calendars in departure_excluded_calendars are skipped. One
    alert per event per day; every lookup is cached. Never raises.
    """
    now = now or time.time()
    found = []
    try:
        from . import nova_config
        if not bool(nova_config.get("departure_alerts_enabled", False)):
            return found
        try:
            lead_default = int(nova_config.get("departure_lead_minutes", DEFAULT_DEPART_LEAD_MIN)
                               or DEFAULT_DEPART_LEAD_MIN)
        except Exception:
            lead_default = DEFAULT_DEPART_LEAD_MIN
        now_dt = datetime.datetime.fromtimestamp(now)
        today = _local_day(now)
        later = _departure_followups(hass, now, now_dt, today)
        if later:
            return later
        sensor_min = _travel_sensor_minutes(hass, nova_config)
        osrm_url = str(nova_config.get("departure_osrm_url", "") or "").strip() or None
        origin = _current_origin(hass)
        skipped = _excluded_calendars(nova_config)
        home_names = _home_names(hass)
        from . import comms
        upcoming = sorted((ev for ev in comms.gather_events(hass)
                           if ev.get("start") and not ev.get("all_day")
                           and ev.get("calendar") not in skipped
                           and is_real_place(ev.get("location"), home_names)),
                          key=lambda ev: ev["start"])
        horizon = datetime.timedelta(hours=DEPART_LOOKAHEAD_H)
        for ev in upcoming:
            start_dt = ev["start"]
            if start_dt <= now_dt:
                continue
            if start_dt - now_dt > horizon:
                break
            title = ev.get("title") or "an event"
            key = "depart:%s:%s" % (title, start_dt.strftime("%Y%m%d%H%M"))
            # One per event, not one per day: a 00:10 event alerted at 23:50
            # must not alert again after midnight.
            if key in _RECUR_ALERTED:
                continue
            loc = ev.get("location")
            minutes, source, failure = await _departure_plan(
                hass, nova_config, ev, (title, key), origin, osrm_url, sensor_min,
                lead_default, now)
            groups = _leave_groups(minutes, start_dt, lead_default)
            if now_dt < groups[0]["leave_at"]:
                continue
            _RECUR_ALERTED[key] = today
            mins_to = max(0, int((start_dt - now_dt).total_seconds() // 60))
            if "any" in minutes:
                lead_from = "default"
            else:
                lead_from = (source if set(minutes) == {"drive"} else "modes") or "default"
            home = _people_home(hass)
            # A later leave time the message already called due is not repeated
            # half a minute on.
            said_now = {i for i, group in enumerate(groups)
                        if i and _minutes_until(group["leave_at"], now_dt) <= 1}
            _DEPART_STAGES[key] = {"title": title, "start": start_dt, "groups": groups,
                                   "home": home, "done": said_now}
            observed = {"event": title, "location": loc or None, "minutes_until": mins_to,
                        "lead_minutes": round(groups[0]["lead"]), "lead_from": lead_from,
                        "travel_lookup_failed": failure,
                        "minutes_by_mode": {m: v for m, v in minutes.items() if v is not None},
                        "people_home": len(home)}
            decision_id = _log_decision(
                "anticipation_departure", observed,
                {"predicted": "departure imminent — should leave soon"},
                "announce departure heads-up",
                "upcoming calendar event + %s" % _LEAD_REASONS.get(lead_from, "travel time"))
            message = _departure_message(title, f" at {loc}" if loc else "", mins_to, groups,
                                         now_dt, lead_default,
                                         failure if "any" in minutes else None)
            found.append({"type": "anticipation_departure", "urgency": "low",
                          "message": message, "pattern_key": key, "offer": False,
                          "decision_id": decision_id})
            break
    except Exception as exc:
        _LOGGER.debug("predict_departure error: %s", exc)
    return found


# ── learned per person routines ─────────────────────────────────────────────
ROUTINE_START_CONF_MIN = 0.65    # only fairly confident routines
ROUTINE_START_TOL_MIN = 30       # up to this many minutes past the usual time


def _person_name(hass, person: str) -> str:
    """A normalised person id as a name: the matching person entity's friendly
    name, else the id in title case."""
    try:
        from . import identity
        for st in hass.states.async_all("person"):
            friendly = st.attributes.get("friendly_name") or ""
            ids = (identity.normalize(friendly), identity.normalize(st.entity_id.split(".", 1)[-1]))
            if person in ids:
                return str(friendly or person).strip()
    except Exception:
        pass
    return str(person or "").replace("_", " ").title()


def _people_in(hass) -> set:
    try:
        from . import identity
        return {identity.normalize(name) for name in identity._home_people(hass)}
    except Exception:
        return set()


def _routine_details(row: dict):
    """(hour, state, entity) from a stored routine row."""
    try:
        details = json.loads(row.get("data") or "{}")
    except Exception:
        details = {}
    entity = str(details.get("entity_id") or "")
    if not entity:
        # Rows from before entity_id was kept name it in the description.
        entity = str(row.get("description") or "").split(" ", 1)[0]
    return details, details.get("hour"), str(details.get("state") or ""), entity


def predict_routine_start(hass, now: float = None) -> list:
    """"You usually turn the Kitchen Light on around now." From the per person
    routine store: a confident time routine that a person does
    (cognitive.routines), as its usual time arrives and while that person is
    home, said once a day. Built from the routine's entity, state and current
    friendly name, and naming the person when someone else is home too. Voice
    command routines stay in the Memory panel unannounced. Off with
    routine_alerts_enabled. Never raises.
    """
    now = now or time.time()
    found = []
    try:
        from . import nova_config
        if not bool(nova_config.get("routine_alerts_enabled", True)):
            return found
        from . import person_patterns
        from .cognitive import naming, routines as rt
        rows = person_patterns.read()
        if not rows:
            return found
        home = _people_in(hass)
        today = _local_day(now)
        now_dt = datetime.datetime.fromtimestamp(now)
        minute_of_day = now_dt.hour * 60 + now_dt.minute
        for row in rows:
            person = row.get("person")
            if not person or person not in home or row.get("pattern_type") != rt.TIME_ROUTINE:
                continue
            if float(row.get("confidence") or 0.0) < ROUTINE_START_CONF_MIN:
                continue
            details, hour, state, entity = _routine_details(row)
            if hour is None or not rt.is_person_routine(entity, state):
                continue
            if not 0 <= minute_of_day - int(hour) * 60 <= ROUTINE_START_TOL_MIN:
                continue
            routine = rt.routine_key(rt.TIME_ROUTINE, entity, details)
            key = f"routine:{person}:{routine}"
            if _RECUR_ALERTED.get(key) == today:
                continue
            _RECUR_ALERTED[key] = today
            st = hass.states.get(entity)
            names = naming.names_from_states([] if st is None else [st])
            addressed = _person_name(hass, person) if home - {person} else ""
            message = rt.routine_sentence(entity, state, names, person_name=addressed)
            decision_id = _log_decision(
                "anticipation_routine", {"person": person, "routine": routine},
                {"predicted": "routine usually starts around now"}, "prompt routine start",
                "learned per-person routine", ref=f"entity:{entity}")
            found.append({
                "type": "anticipation_routine", "urgency": "low", "message": message,
                "pattern_key": key, "offer": False,
                # One habit per person and device, whatever the hour.
                "habit_key": f"routine:{person}:{entity}:{state}",
                "entity_id": entity, "decision_id": decision_id,
            })
    except Exception as exc:
        _LOGGER.debug("predict_routine_start error: %s", exc)
    return found


# ── departures, arrivals and heading home ───────────────────────────────────

def _departure_lead_secs() -> float:
    try:
        from . import nova_config
        lead = float(nova_config.get("routine_departure_lead_minutes",
                                     DEFAULT_ROUTINE_DEPART_LEAD_MIN))
    except Exception:
        lead = DEFAULT_ROUTINE_DEPART_LEAD_MIN
    return max(0.0, min(lead, 240.0)) * 60


def _departure_actions(rt, eid, name, entry, today, clock, lead, who) -> list:
    """Reminders and still home checks for one person who is home."""
    actions = []
    left_today = [secs for day, secs in entry.depart_first if day == today]
    for routine in rt.departures(entry.depart_first, today):
        if rt.left_for(routine, left_today):
            continue
        if rt.reminder_due(routine, clock, lead):
            key, remind = f"dep_soon:{eid}:{routine.key}", True
            message = rt.reminder_text(routine, clock, who)
        elif rt.late_due(routine, clock):
            key, remind = f"dep:{eid}:{routine.key}", False
            message = rt.late_text(routine, who)
        else:
            continue
        if _RECUR_ALERTED.get(key) == today:
            continue
        _RECUR_ALERTED[key] = today
        decision_id = _log_decision(
            "anticipation_presence",
            {"person": name, "entity_id": eid, "usual_out_by": rt.hhmm(routine.mean),
             "day_type": routine.day_type},
            {"predicted": "usual departure coming up" if remind
             else "still home past the usual departure time"},
            "remind departure" if remind else "flag still-home",
            "recurring departure pattern", ref=f"entity:{eid}")
        actions.append({"type": "anticipation_presence", "urgency": "low", "message": message,
                        "pattern_key": f"presence_depart:{eid}:{routine.key}", "offer": False,
                        "decision_id": decision_id})
    return actions


def _arrival_action(eid, name, entry, state, today, clock):
    """'Usually home by now' for someone out who left today and is not back."""
    usual = _routine_of_pts(list(entry.return_first))
    if not usual or not _is_away(state):
        return None
    if entry.pres_depart_day != today or entry.pres_return_day == today:
        return None
    mean, spread = usual
    key = f"arr:{eid}"
    if clock <= mean + _grace(spread) or _RECUR_ALERTED.get(key) == today:
        return None
    _RECUR_ALERTED[key] = today
    decision_id = _log_decision(
        "anticipation_presence", {"person": name, "entity_id": eid, "usual_home_by": _hhmm(mean)},
        {"predicted": "not home yet, past the usual arrival time"}, "flag not-back",
        "recurring arrival pattern", ref=f"entity:{eid}")
    return {"type": "anticipation_presence", "urgency": "low",
            "message": f"{name} is usually home by around {_hhmm(mean)}, but isn't back yet.",
            "pattern_key": f"presence_arrive:{eid}", "offer": False, "decision_id": decision_id}


def predict_presence(hass, now: float = None) -> list:
    """People's routines. Departures (cognitive.presence_routines): a reminder
    routine_departure_lead_minutes before the usual time for today's kind of
    day, then a "still home" check, each once a day while that person is home
    and has not left for it, said to them ("You usually leave around 12:00.")
    and named when someone else is home. Arrivals: "usually home by HH:MM but
    not back yet", once a day. Device trackers only in a home with no people.
    In memory and cheap, so it runs every tick.
    """
    from .cognitive import presence_routines as rt
    now = now or time.time()
    found = []
    today = _local_day(now)
    clock = _secs_since_midnight(now)
    lead = _departure_lead_secs()
    try:
        domains = _routine_presence_domains(hass)
        home_now = {st.entity_id for st in _routine_presence_entities(hass)
                    if _is_home(st.state)}
        for eid, entry in list(_MODEL.items()):
            if eid.split(".", 1)[0] not in domains:
                continue
            st = hass.states.get(eid)
            if st is None or st.state in _DEAD_STATES:
                continue
            name = st.attributes.get("friendly_name", eid)
            if _is_home(st.state):
                who = name if home_now - {eid} else ""
                found.extend(_departure_actions(rt, eid, name, entry, today, clock, lead, who))
            arrival = _arrival_action(eid, name, entry, st.state, today, clock)
            if arrival:
                found.append(arrival)
    except Exception as exc:
        _LOGGER.debug("predict_presence error: %s", exc)
    return found


def _tracked_people(hass) -> list:
    for domain in ("person", "device_tracker"):
        try:
            people = list(hass.states.async_all(domain))
        except Exception:
            people = []
        if people:
            return people
    return []


def presence_status(hass) -> list:
    """Where each tracked person is: zone, tracker kind, and distance from
    home for location aware trackers. Person entities first (they combine
    trackers), else device trackers."""
    rows = []
    try:
        for st in _tracked_people(hass):
            if st.state in _DEAD_STATES:
                continue
            km = distance_home_km(hass, st)
            if _is_home(st.state):
                zone = "Home"
            else:
                zone = "Away" if st.state == "not_home" else st.state
            rows.append({"entity_id": st.entity_id,
                         "name": st.attributes.get("friendly_name", st.entity_id),
                         "zone": zone, "gps": _is_gps(st),
                         "distance_km": None if km is None else round(km, 1)})
    except Exception as exc:
        _LOGGER.debug("presence_status error: %s", exc)
    return rows


def predict_proximity(hass, now: float = None) -> list:
    """A location aware person heading home: flagged once a trip as they close
    inside APPROACH_OUTER_KM. Needs no home network, and resets once they are
    home. People only (a person and their phone are one arrival), device
    trackers in a home with no people."""
    found = []
    try:
        for st in _routine_presence_entities(hass):
            eid = st.entity_id
            if _is_home(st.state):
                _LAST_DIST.pop(eid, None)
                _APPROACH_ALERTED.pop(eid, None)
                continue
            if _entity_coords(st) is None:
                continue
            km = distance_home_km(hass, st)
            if km is None:
                continue
            before, _LAST_DIST[eid] = _LAST_DIST.get(eid), km
            if before is None:
                continue
            closing = km < before - APPROACH_MIN_CLOSE_KM
            if not closing or km > APPROACH_OUTER_KM or _APPROACH_ALERTED.get(eid):
                continue
            _APPROACH_ALERTED[eid] = True
            name = st.attributes.get("friendly_name", eid)
            found.append({"type": "anticipation_arriving", "urgency": "low",
                          "message": f"{name} is heading home — about {km:.1f} km out.",
                          "pattern_key": f"arriving:{eid}", "offer": False})
    except Exception as exc:
        _LOGGER.debug("predict_proximity error: %s", exc)
    return found


# ── saving to patterns.db ───────────────────────────────────────────────────

def _entry_to_dict(e: _Entry) -> dict:
    return {
        "last_state": e.last_state, "last_changed": e.last_changed, "first_seen": e.first_seen,
        "transitions": list(e.transitions), "hours": e.hours,
        "occ": {str(hour): tally for hour, tally in e.occ.items()},
        "daily_first": list(e.daily_first), "last_first_day": e.last_first_day,
        "depart_first": list(e.depart_first), "return_first": list(e.return_first),
        "pres_depart_day": e.pres_depart_day, "pres_return_day": e.pres_return_day,
        "last_depart_ts": e.last_depart_ts, "home_since_depart": e.home_since_depart,
    }


def _points(saved, keep: int) -> deque:
    return deque((tuple(point) for point in saved or []), maxlen=keep)


def _entry_from_dict(d: dict) -> _Entry:
    e = _Entry(float(d.get("first_seen", time.time())))
    e.last_state = d.get("last_state", "")
    e.last_changed = float(d.get("last_changed", e.first_seen))
    e.transitions = deque(d.get("transitions", []), maxlen=TRANSITION_WINDOW)
    hours = d.get("hours") or [0] * 24
    e.hours = hours if len(hours) == 24 else [0] * 24
    e.occ = {int(hour): {state: int(n) for state, n in tally.items()}
             for hour, tally in (d.get("occ") or {}).items()}
    e.daily_first = _points(d.get("daily_first"), _DAILY_KEEP)
    e.depart_first = _points(d.get("depart_first"), DEPART_HISTORY)
    e.return_first = _points(d.get("return_first"), _DAILY_KEEP)
    e.last_first_day = int(d.get("last_first_day", 0))
    e.pres_depart_day = int(d.get("pres_depart_day", 0))
    e.pres_return_day = int(d.get("pres_return_day", 0))
    e.last_depart_ts = float(d.get("last_depart_ts", 0.0) or 0.0)
    e.home_since_depart = bool(d.get("home_since_depart", True))
    return e


def _prune_alerted(today: int) -> list:
    """Keep ledger entries from the last RECUR_ALERT_KEEP_DAYS days, at most
    RECUR_ALERT_MAX of them (newest first), and return those (key, day)
    pairs."""
    oldest = today - RECUR_ALERT_KEEP_DAYS + 1

    def drop(key, day):
        if _RECUR_ALERTED.get(key) == day:
            _RECUR_ALERTED.pop(key, None)
    kept = []
    for key, day in list(_RECUR_ALERTED.items()):
        if isinstance(key, str) and isinstance(day, int) and oldest <= day <= today:
            kept.append((key, day))
        else:
            drop(key, day)
    kept.sort(key=lambda pair: pair[1], reverse=True)
    for key, day in kept[RECUR_ALERT_MAX:]:
        drop(key, day)
    return kept[:RECUR_ALERT_MAX]


def _open(db_path: str):
    return _store.connect(db_path, timeout=10, row_factory=False, mkdir=False,
                          busy_timeout_ms=None)


def save_to_db(db_path: str, now: float = None) -> int:
    """Save the model and the once a day ledger to patterns.db. Blocking: run
    it in the executor."""
    try:
        ledger = _prune_alerted(_local_day(now or time.time()))
        with _open(db_path) as conn:
            _store.ensure(conn, "cognition")
            rows = [(eid, json.dumps(_entry_to_dict(entry)), time.time())
                    for eid, entry in list(_MODEL.items())]
            conn.executemany("INSERT OR REPLACE INTO cognition_model (entity_id, data, "
                             "updated) VALUES (?, ?, ?)", rows)
            conn.execute("DELETE FROM cognition_alerted")
            conn.executemany("INSERT INTO cognition_alerted (key, day) VALUES (?, ?)", ledger)
            conn.commit()
        return len(rows)
    except Exception as exc:
        _LOGGER.debug("cognition save_to_db failed: %s", exc)
        return 0


def load_from_db(db_path: str, now: float = None) -> int:
    """Load the model and the ledger from patterns.db. Blocking: run it in the
    executor. A missing or damaged ledger restores nothing."""
    loaded = 0
    try:
        with _open(db_path) as conn:
            _store.ensure(conn, "cognition")
            for eid, data in conn.execute(
                    "SELECT entity_id, data FROM cognition_model").fetchall():
                try:
                    _MODEL[eid] = _entry_from_dict(json.loads(data))
                except Exception:
                    continue
                loaded += 1
        _LOGGER.info("cognition: loaded %d entity models from patterns.db", loaded)
    except Exception as exc:
        _LOGGER.debug("cognition load_from_db failed: %s", exc)
    try:
        with _open(db_path) as conn:
            ledger = conn.execute("SELECT key, day FROM cognition_alerted").fetchall()
        for key, day in ledger:
            if isinstance(key, str) and isinstance(day, int) and day > _RECUR_ALERTED.get(key, 0):
                _RECUR_ALERTED[key] = day
        _prune_alerted(_local_day(now or time.time()))
    except Exception as exc:
        _LOGGER.debug("cognition alert ledger load failed: %s", exc)
    return loaded
