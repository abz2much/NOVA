"""Nova's read-only world model: one snapshot of the house per tick (8.24.0).

The 30 second loop takes one snapshot when a tick starts and drops it when
the tick ends. While a tick runs, household.snapshot() and
alert_path.situation() hand out that tick's reading, so every source in the
tick (safety, lockdown, offers, the alerts they raise) sees the same house.
Outside a tick they read the house live, as before.

The snapshot holds:
  * house           the household.py answer: residents and alarm posture
  * asleep          True, False or UNKNOWN
  * quiet_hours     True, False or UNKNOWN
  * open_ways_in    doors, windows and garage doors open into the house
  * unlocked_locks  locks reading unlocked
  * unreadable_locks locks reading unknown, unavailable or jammed
  * open_covers     covers reading open or opening
  * person_motion   motion, occupancy and presence sensors showing a person
  * lockdown_active True, False or UNKNOWN
  * open_situations open situations (situations.py), or UNKNOWN

Read only: nothing here calls a service or writes anything. Fails safe: a
field that cannot be read is UNKNOWN, never an empty list or False that a
caller could take to mean "away" or "secure".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

UNKNOWN = "unknown"

_OPENING_SENSORS = ("door", "window", "garage_door", "opening")
_ENTRY_COVERS = ("door", "garage", "garage_door", "gate")
_MOTION = ("motion", "occupancy", "presence")


@dataclass(frozen=True)
class World:
    """One read-only snapshot of the house."""
    house: Any                       # household.Household, or None if unreadable
    asleep: Any = UNKNOWN
    quiet_hours: Any = UNKNOWN
    open_ways_in: Any = UNKNOWN
    unlocked_locks: Any = UNKNOWN
    unreadable_locks: Any = UNKNOWN
    open_covers: Any = UNKNOWN
    person_motion: Any = UNKNOWN
    lockdown_active: Any = UNKNOWN
    open_situations: Any = UNKNOWN
    tick: int = 0

    @property
    def residents(self) -> str:
        from . import household
        return getattr(self.house, "residents", household.UNKNOWN)

    def secure(self) -> Any:
        """True only when nothing is open, unlocked or unreadable; UNKNOWN
        when any of that cannot be read. Never True on a guess."""
        parts = (self.open_ways_in, self.unlocked_locks, self.unreadable_locks)
        if any(p == UNKNOWN for p in parts):
            return UNKNOWN
        return not any(parts)


def _read(label: str, fn, *args):
    try:
        return fn(*args)
    except Exception as exc:
        _LOGGER.debug("world: %s could not be read: %s", label, exc)
        return UNKNOWN


def _open_ways_in(hass) -> tuple:
    from . import household
    out = []
    for st in hass.states.async_all("binary_sensor"):
        if (st.attributes.get("device_class") in _OPENING_SENSORS and st.state == "on"
                and household.is_way_in(hass, st)):
            out.append(st.entity_id)
    for st in hass.states.async_all("cover"):
        if (st.attributes.get("device_class") in _ENTRY_COVERS
                and st.state in ("open", "opening") and household.is_way_in(hass, st)):
            out.append(st.entity_id)
    return tuple(sorted(out))


def _locks(hass, states) -> tuple:
    return tuple(sorted(st.entity_id for st in hass.states.async_all("lock")
                        if str(st.state).lower() in states))


def _open_covers(hass) -> tuple:
    return tuple(sorted(st.entity_id for st in hass.states.async_all("cover")
                        if st.state in ("open", "opening")))


def _person_motion(hass) -> tuple:
    from . import household
    return tuple(sorted(
        st.entity_id for st in hass.states.async_all("binary_sensor")
        if st.attributes.get("device_class") in _MOTION and st.state == "on"
        and household.is_person_motion(hass, st)))


def _lockdown_active() -> bool:
    from .core_bridge import is_lockdown
    return bool(is_lockdown())


# Whatever tracks open situations registers a reader here (situations.py
# does). With none registered the field is UNKNOWN, never "none open".
_SITUATION_SOURCE = None


def register_situation_source(fn) -> None:
    """fn(hass) -> iterable of open situation kinds."""
    global _SITUATION_SOURCE
    _SITUATION_SOURCE = fn


def _open_situations(hass) -> tuple:
    if _SITUATION_SOURCE is None:
        raise LookupError("no situation source registered")
    return tuple(_SITUATION_SOURCE(hass))


def read(hass, config: Optional[dict] = None, *, asleep: Any = UNKNOWN,
         quiet_hours: Any = UNKNOWN, tick: int = 0) -> World:
    """Read the house now. Never raises."""
    from . import household
    house = _read("household", household._live_snapshot, hass, config)
    return World(
        house=None if house == UNKNOWN else house,
        asleep=asleep, quiet_hours=quiet_hours,
        open_ways_in=_read("open ways in", _open_ways_in, hass),
        unlocked_locks=_read("unlocked locks", _locks, hass, ("unlocked",)),
        unreadable_locks=_read("unreadable locks", _locks, hass,
                               ("unknown", "unavailable", "jammed")),
        open_covers=_read("open covers", _open_covers, hass),
        person_motion=_read("person motion", _person_motion, hass),
        lockdown_active=_read("lockdown", _lockdown_active),
        open_situations=_read("situations", _open_situations, hass),
        tick=tick,
    )


# ── the tick's snapshot ─────────────────────────────────────────────────────

_CURRENT: Optional[World] = None


def begin_tick(hass, config: Optional[dict] = None, *, asleep: Any = UNKNOWN,
               quiet_hours: Any = UNKNOWN, tick: int = 0) -> World:
    """Take this tick's snapshot. Every reader sees it until end_tick()."""
    global _CURRENT
    _CURRENT = read(hass, config, asleep=asleep, quiet_hours=quiet_hours, tick=tick)
    return _CURRENT


def end_tick() -> None:
    global _CURRENT
    _CURRENT = None


def current() -> Optional[World]:
    """The running tick's snapshot, or None between ticks."""
    return _CURRENT
