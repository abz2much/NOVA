"""Who is home, what the alarm says, and which sensors count, in one place.

Before 8.21.0 each part of Nova worked these out for itself, and the answers
disagreed. Intrusion counted every device tracker as a resident, so a smart
TV reading "home" stopped away detection even with the alarm armed away. A
person reading "unknown" counted as away. The alarm's custom bypass mode was
missing from the "residents are home" rule, which let a resident walking about
confirm an intrusion. This module gives one answer to each question.

* Residents: home, away or unknown. People count, plus only the device
  trackers linked to a person. A home with no person entities at all falls
  back to its device trackers, as before. Unknown is never away, and motion
  never counts: intrusion exists to judge motion, so motion cannot also say
  whether anyone is home.
* Posture: what the selected alarm's state says about the household. One
  table covers every state Home Assistant defines.
* Shared entity rules: what is a way into the house, and what is a person's
  motion.

Pure reads of the state machine. Nothing here calls a service.
"""
from __future__ import annotations

from dataclasses import dataclass

HOME = "home"
AWAY = "away"
UNKNOWN = "unknown"

_UNREADABLE = frozenset({"unknown", "unavailable", "none", ""})

# Every alarm state, and what it says about the household. Armed away and
# vacation mean nobody should be moving about. Armed home, night and custom
# bypass mean residents are expected to move about. Every other state says
# nothing either way. "triggered" is read on its own (Household.triggered).
ALARM_POSTURE = {
    "armed_away": AWAY,
    "armed_vacation": AWAY,
    "armed_home": HOME,
    "armed_night": HOME,
    "armed_custom_bypass": HOME,
    "disarmed": UNKNOWN,
    "arming": UNKNOWN,
    "pending": UNKNOWN,
    "disarming": UNKNOWN,
    "triggered": UNKNOWN,
    "unavailable": UNKNOWN,
    "unknown": UNKNOWN,
}


def _lower(state) -> str:
    return str(getattr(state, "state", "") or "").lower()


def posture_of(alarm_states) -> str:
    """The posture for a set of alarm states. Away beats home."""
    found = {ALARM_POSTURE.get(str(s).lower(), UNKNOWN) for s in alarm_states}
    if AWAY in found:
        return AWAY
    if HOME in found:
        return HOME
    return UNKNOWN


def _person_reading(hass, person) -> str:
    """Home if the person, or a device tracker linked to them, reads home.
    Unknown if the person's own state is unreadable. Otherwise away."""
    if _lower(person) == "home":
        return HOME
    for tracker_id in person.attributes.get("device_trackers") or ():
        tracker = hass.states.get(tracker_id)
        if tracker is not None and _lower(tracker) == "home":
            return HOME
    if _lower(person) in _UNREADABLE:
        return UNKNOWN
    return AWAY


def residents(hass) -> str:
    """HOME, AWAY or UNKNOWN for the household."""
    people = list(hass.states.async_all("person"))
    if people:
        readings = {_person_reading(hass, p) for p in people}
        if HOME in readings:
            return HOME
        if UNKNOWN in readings:
            return UNKNOWN
        return AWAY
    # No person entities at all: the device trackers are the only signal.
    trackers = {_lower(t) for t in hass.states.async_all("device_tracker")}
    if "home" in trackers:
        return HOME
    if trackers & {"not_home", "away"}:
        return AWAY
    return UNKNOWN


@dataclass(frozen=True)
class Household:
    """One reading of the household, taken once per tick."""
    residents: str
    alarm_states: frozenset

    @property
    def posture(self) -> str:
        return posture_of(self.alarm_states)

    @property
    def armed(self) -> bool:
        """The selected alarm is in any armed state."""
        return self.posture in (HOME, AWAY)

    @property
    def triggered(self) -> bool:
        """The alarm itself has gone off, not merely armed."""
        return "triggered" in self.alarm_states

    @property
    def anyone_home(self) -> bool:
        return self.residents == HOME

    @property
    def residents_away(self) -> bool:
        """Confidently away. A resident reading home always wins. Unknown
        residents count as away only when the alarm is armed away."""
        if self.residents == HOME:
            return False
        return self.residents == AWAY or self.posture == AWAY

    def residents_home_guard(self, sleeping: bool) -> bool:
        """Residents are home and the alarm is armed for people at home, or
        the household is asleep. Then a resident walking about looks just
        like an intruder, so motion alone must never confirm an intrusion.
        Armed away or vacation keeps the away rules. "triggered" keeps the
        guard on, so the alarm going off can still confirm."""
        if self.posture == AWAY:
            return False
        if not (sleeping or self.posture == HOME or self.triggered):
            return False
        return self.residents == HOME


def snapshot(hass, config: dict | None = None) -> Household:
    """Read the household now: residents, and the selected alarm's state."""
    from . import alarm_source
    states = frozenset(_lower(st) for st in alarm_source.states(hass, config))
    return Household(residents=residents(hass), alarm_states=states)


# ── shared entity rules ─────────────────────────────────────────────────────

def is_way_in(hass, state) -> bool:
    """An opening someone could come into the house through. An appliance or
    cabinet door, anything the user excluded from Nova, and openings outside
    the house (a driveway gate, a shed door) are not. The garage is part of
    the house, so anything garage named stays in. Does not look at the
    device class or whether it is open; callers do."""
    from . import outdoor
    from .entity_filter import is_appliance_opening, is_excluded
    eid = state.entity_id
    fname = state.attributes.get("friendly_name") or ""
    if is_appliance_opening(eid, fname) or is_excluded(hass, eid):
        return False
    if "garage" in (eid + " " + fname).lower():
        return True
    return not outdoor.is_outdoor(hass, eid, fname)


def is_person_motion(hass, state) -> bool:
    """A motion, occupancy or presence sensor that can show a person inside
    the house. A camera's car, animal or package sensor, anything the user
    excluded from Nova, and outdoor sensors are not. Does not look at the
    device class or the state; callers do."""
    from . import outdoor
    from .entity_filter import is_excluded, is_object_sensor
    eid = state.entity_id
    fname = state.attributes.get("friendly_name") or ""
    if is_object_sensor(eid, fname) or is_excluded(hass, eid):
        return False
    return not outdoor.is_outdoor(hass, eid, fname)
