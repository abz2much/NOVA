"""The live check before a lock, cover, garage or alarm command (8.46.0).

policy.authorize() and policy.authorize_now() run this first, before the
policy and the confirmation, for every command on a lock, a cover or the
alarm. It reads the device, and the state that decides whether the command
is safe, from Home Assistant at that moment. Nothing is cached.

* The device, or a sensor the check needs, reads unknown or unavailable (or
  a lock reads jammed): the command is refused. Unknown never passes.
* The device is already locked, closed or armed that way: nothing is sent,
  and the log says it was already secure.
* Locking a lock whose door reads open: Nova acting on its own skips it and
  reports it; a person's own request is warned and asked first.
* Arming the alarm with a door or window into the house open: the person is
  asked first, naming what is open. Nova never arms on its own.

Pure reads of the state machine. Nothing here calls a service.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

DOMAINS = ("lock", "cover", "alarm_control_panel")

GO = "go"
ALREADY_SECURE = "already_secure"
REFUSE = "refuse"
ASK = "ask"

# Refusal reasons, also used as the Actions log reason code.
UNREADABLE = "device_unreadable"
DOOR_OPEN = "door_open"
DOOR_UNREADABLE = "door_unreadable"
ZONES_UNREADABLE = "zones_unreadable"
ZONES_OPEN = "zones_open"

_BLANK = frozenset({"unknown", "unavailable", "none", ""})
_READ_ONLY = frozenset({"", "update", "reload"})
_DOOR_CLASSES = ("door", "garage_door", "opening")

# The state that means the command has nothing left to do.
_SECURE_STATE = {
    ("lock", "lock"): "locked",
    ("cover", "close_cover"): "closed",
    ("cover", "close"): "closed",
    ("alarm_control_panel", "alarm_arm_away"): "armed_away",
    ("alarm_control_panel", "alarm_arm_home"): "armed_home",
    ("alarm_control_panel", "alarm_arm_night"): "armed_night",
    ("alarm_control_panel", "alarm_arm_vacation"): "armed_vacation",
    ("alarm_control_panel", "alarm_arm_custom_bypass"): "armed_custom_bypass",
}


@dataclass(frozen=True)
class Check:
    """The live check's answer for one command."""
    verdict: str                 # GO, ALREADY_SECURE, REFUSE or ASK
    note: str = ""               # what the person is told
    code: str = ""               # the reason, for the Actions log
    named: tuple = ()            # the doors or zones the note names


def applies(domain: str, service: str, entity_id: str) -> bool:
    """Only commands on one named lock, cover or alarm are checked. Scenes
    and scripts cannot be seen into, and keep their confirmation."""
    return (domain in DOMAINS and bool(entity_id)
            and str(service or "").lower() not in _READ_ONLY)


def _name(hass, entity_id: str, state=None) -> str:
    st = state if state is not None else hass.states.get(entity_id)
    name = (st.attributes.get("friendly_name") if st is not None else None)
    return str(name or entity_id)


def _blank(state) -> bool:
    return state is None or str(state.state or "").lower() in _BLANK


def _names(hass, ids) -> str:
    names = [_name(hass, e) for e in ids]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def door_contacts(hass, lock_id: str) -> tuple:
    """The door contact sensors on the same device as the lock. Empty when
    the lock has no device or the registry cannot be read: a lock with no
    door sensor is checked on its own state."""
    try:
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(hass)
        entry = reg.async_get(lock_id)
        device_id = getattr(entry, "device_id", None)
        if not device_id:
            return ()
        out = []
        for e in list(getattr(reg, "entities", {}).values()):
            eid = getattr(e, "entity_id", "")
            if getattr(e, "device_id", None) != device_id or not eid.startswith("binary_sensor."):
                continue
            st = hass.states.get(eid)
            dc = (getattr(e, "device_class", None) or getattr(e, "original_device_class", None)
                  or (st.attributes.get("device_class") if st is not None else None))
            if dc in _DOOR_CLASSES:
                out.append(eid)
        return tuple(sorted(out))
    except Exception as exc:
        _LOGGER.debug("safety_check: door sensors for %s not found: %s", lock_id, exc)
        return ()


def check(hass, domain: str, service: str, entity_id: str, *,
          automatic: bool) -> Check:
    """Read the device and its related state now. Never raises: an error
    reading anything refuses the command."""
    try:
        return _check(hass, domain, str(service or "").lower(), entity_id,
                      automatic=automatic)
    except Exception as exc:
        _LOGGER.warning("safety_check: %s.%s on %s could not be checked: %s",
                        domain, service, entity_id, exc)
        return Check(REFUSE, f"Nova couldn't check {entity_id} just now, so nothing was sent.",
                     UNREADABLE, (entity_id,))


def _check(hass, domain: str, service: str, entity_id: str, *, automatic: bool) -> Check:
    st = hass.states.get(entity_id)
    name = _name(hass, entity_id, st)
    now = str(getattr(st, "state", "") or "").lower()
    if _blank(st) or (domain == "lock" and now == "jammed"):
        return Check(REFUSE, f"{name} can't be read right now ({now or 'missing'}), "
                             "so nothing was sent.", UNREADABLE, (entity_id,))
    secure = _SECURE_STATE.get((domain, service))
    if secure is not None and now == secure:
        return Check(ALREADY_SECURE,
                     f"{name} is already {secure.replace('_', ' ')}, so nothing was sent.",
                     ALREADY_SECURE, (entity_id,))

    if (domain, service) == ("lock", "lock"):
        doors = door_contacts(hass, entity_id)
        unreadable = tuple(d for d in doors if _blank(hass.states.get(d)))
        if unreadable:
            return Check(REFUSE, f"The door sensor for {name} can't be read right now, "
                                 f"so {name} was not locked.", DOOR_UNREADABLE, unreadable)
        open_ = tuple(d for d in doors if str(hass.states.get(d).state).lower() == "on")
        if open_:
            if automatic:
                return Check(REFUSE, f"{_names(hass, open_)} is open, so {name} was not "
                                     "locked.", DOOR_OPEN, open_)
            return Check(ASK, f"{_names(hass, open_)} is open.", DOOR_OPEN, open_)

    if domain == "alarm_control_panel" and service.startswith("alarm_arm"):
        from . import world
        unreadable = world._unreadable_openings(hass)
        if unreadable:
            return Check(REFUSE, f"{_names(hass, unreadable)} can't be read right now, "
                                 "so the alarm was not armed.", ZONES_UNREADABLE,
                         tuple(unreadable))
        open_ = world._open_ways_in(hass)
        if open_:
            verb = "is" if len(open_) == 1 else "are"
            if automatic:
                return Check(REFUSE, f"{_names(hass, open_)} {verb} open, so the alarm "
                                     "was not armed.", ZONES_OPEN, tuple(open_))
            return Check(ASK, f"{_names(hass, open_)} {verb} open.", ZONES_OPEN,
                         tuple(open_))
    return Check(GO)


def after_yes(hass, domain: str, service: str, entity_id: str, first: Check) -> Check:
    """The check again once the person has said yes, since waiting for the
    answer can take a while. What they were warned about may still be open;
    anything new that is open, or that can no longer be read, refuses."""
    again = check(hass, domain, service, entity_id, automatic=False)
    if again.verdict == ASK:
        new = tuple(n for n in again.named if n not in first.named)
        if not new:
            return Check(GO)
        verb = "is" if len(new) == 1 else "are"
        return Check(REFUSE, f"{_names(hass, new)} {verb} now open as well, so nothing "
                             "was sent.", again.code, new)
    return again
