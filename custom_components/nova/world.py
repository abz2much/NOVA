"""Nova's read-only world model (8.24.0), and the one place presence, sleep
and quiet hours are worked out (8.46.0).

The resolvers: every part of Nova that needs to know who is home, whether
the house is asleep or whether it is quiet hours asks here, so the answers
cannot disagree. Each answer is a Fact that says when it was read and where
it came from. Nothing is cached: each tick, each Observer event and each
request reads afresh.

  * presence(hass)     the household.py answer: residents and alarm posture
  * asleep(hass)       True, False or UNKNOWN, with sleep_detection's reason
  * quiet_hours(hass)  True, False or UNKNOWN, with the window it used
  * setting(hass, ...) the live panel setting, then the saved config

The tick's snapshot: the 30 second loop takes one snapshot when a tick
starts and drops it when the tick ends. While a tick runs,
household.snapshot() and alert_path.situation() hand out that tick's
reading, so every source in the tick (safety, lockdown, offers, the alerts
they raise) sees the same house. Outside a tick they read the house live.

The snapshot holds:
  * house           the household.py answer: residents and alarm posture
  * asleep          True, False or UNKNOWN
  * quiet_hours     True, False or UNKNOWN
  * open_ways_in    doors, windows and garage doors open into the house
  * unlocked_locks  locks reading unlocked
  * unreadable_locks locks reading unknown, unavailable or jammed
  * unreadable_openings doors, windows and garage doors into the house
                    reading unknown or unavailable (8.25.0)
  * open_covers     covers reading open or opening
  * person_motion   motion, occupancy and presence sensors showing a person
  * lockdown_active True, False or UNKNOWN
  * open_situations open situations (situations.py), or UNKNOWN
  * read_at, sources when it was read, and where asleep, quiet hours and
                    the household came from (8.46.0)

A decision keeps only the facts it used (facts()), never the whole snapshot.

Read only: nothing here calls a service or writes anything. Fails safe: a
field that cannot be read is UNKNOWN, never an empty list or False that a
caller could take to mean "away" or "secure".
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

UNKNOWN = "unknown"

_OPENING_SENSORS = ("door", "window", "garage_door", "opening")
_ENTRY_COVERS = ("door", "garage", "garage_door", "gate")
_MOTION = ("motion", "occupancy", "presence")


# ── the resolvers (8.46.0) ──────────────────────────────────────────────────

@dataclass(frozen=True)
class Fact:
    """One value Nova read, when, and from where."""
    value: Any
    source: str = ""
    read_at: float = 0.0

    def record(self) -> dict:
        """How a decision keeps it: plain values only."""
        return {"value": _plain(self.value), "source": self.source,
                "read_at": _iso(self.read_at)}


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple, frozenset, set)):
        return sorted(str(v) for v in value)
    residents = getattr(value, "residents", None)
    if residents is not None:
        return {"residents": residents, "posture": getattr(value, "posture", None)}
    return str(value)


def _iso(ts: float) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def setting(hass, key: str, default):
    """The live panel setting, then the saved config, then `default`. A
    loaded entry that has lost its runtime is logged and reads the default.
    Never raises."""
    from .runtime import NovaRuntimeUnavailable
    try:
        from . import nova_config
        from .runtime import domain_runtime_config
        rc = domain_runtime_config(hass)
        if key in rc and rc[key] not in (None, ""):
            return rc[key]
        value = nova_config.get(key, None)
        return default if value in (None, "") else value
    except NovaRuntimeUnavailable as exc:
        _LOGGER.warning("world: live settings unavailable, using defaults: %s", exc)
        return default
    except Exception:
        return default


def quiet_window(hass) -> tuple:
    """The household's quiet hours as ("HH:MM", "HH:MM")."""
    from .const import DEFAULT_OBSERVER_QUIET_END, DEFAULT_OBSERVER_QUIET_START
    return (str(setting(hass, "observer_quiet_start", DEFAULT_OBSERVER_QUIET_START)),
            str(setting(hass, "observer_quiet_end", DEFAULT_OBSERVER_QUIET_END)))


def quiet_hours(hass) -> Fact:
    """Whether it is quiet hours now. UNKNOWN if it cannot be worked out."""
    now = time.time()
    try:
        from . import sleep_detection
        start, end = quiet_window(hass)
        return Fact(bool(sleep_detection._in_quiet_hours(start, end)),
                    f"quiet hours setting {start}-{end}", now)
    except Exception as exc:
        _LOGGER.debug("world: quiet hours could not be read: %s", exc)
        return Fact(UNKNOWN, "unreadable", now)


def asleep(hass) -> Fact:
    """Whether the house is asleep now, with sleep_detection's reason.
    UNKNOWN if it cannot be worked out."""
    now = time.time()
    try:
        from . import sleep_detection
        start, end = quiet_window(hass)
        value, why = sleep_detection.is_sleeping(
            hass, bedroom_area_ids=setting(hass, "bedroom_areas", []) or [],
            quiet_start=start, quiet_end=end)
        return Fact(bool(value), str(why or ""), now)
    except Exception as exc:
        _LOGGER.debug("world: sleep could not be read: %s", exc)
        return Fact(UNKNOWN, "unreadable", now)


def presence(hass, config: Optional[dict] = None) -> Fact:
    """The household now (household.py): who is home and the alarm posture.
    The value is None if it cannot be read."""
    try:
        from . import household
        house = household._live_snapshot(hass, config)
        return Fact(house, house.source, house.read_at)
    except Exception as exc:
        _LOGGER.debug("world: household could not be read: %s", exc)
        return Fact(None, "unreadable", time.time())


# ── the tick's snapshot ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class World:
    """One read-only snapshot of the house."""
    house: Any                       # household.Household, or None if unreadable
    asleep: Any = UNKNOWN
    quiet_hours: Any = UNKNOWN
    open_ways_in: Any = UNKNOWN
    unlocked_locks: Any = UNKNOWN
    unreadable_locks: Any = UNKNOWN
    unreadable_openings: Any = UNKNOWN
    open_covers: Any = UNKNOWN
    person_motion: Any = UNKNOWN
    lockdown_active: Any = UNKNOWN
    open_situations: Any = UNKNOWN
    tick: int = 0
    read_at: float = 0.0
    sources: dict = field(default_factory=dict, compare=False)

    def facts(self, *names: str) -> dict:
        """Only the named facts, for a decision to keep: value, source and
        when it was read. "residents" and "posture" come from the house."""
        out = {}
        for name in names:
            if name in ("residents", "posture"):
                value = (getattr(self.house, name, UNKNOWN) if self.house is not None
                         else UNKNOWN)
                src = self.sources.get("house", "")
            else:
                value = getattr(self, name, UNKNOWN)
                src = self.sources.get(name, "")
            out[name] = Fact(value, src, self.read_at).record()
        return out

    @property
    def residents(self) -> str:
        from . import household
        return getattr(self.house, "residents", household.UNKNOWN)

    def secure(self) -> Any:
        """False when something is known to be open or unlocked. UNKNOWN when
        anything could not be read, or a lock, door or window reads unknown
        (8.25.0). True only when everything was read and is shut. Never True
        on a guess."""
        parts = (self.open_ways_in, self.unlocked_locks, self.unreadable_locks,
                 self.unreadable_openings)
        if any(p == UNKNOWN for p in parts):
            return UNKNOWN
        if self.open_ways_in or self.unlocked_locks:
            return False
        if self.unreadable_locks or self.unreadable_openings:
            return UNKNOWN
        return True


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


def _unreadable_openings(hass) -> tuple:
    """Ways into the house whose sensor cannot be read: Nova cannot tell if
    they are closed, so the house is never "secure" while any are listed."""
    from . import household
    blank = ("unknown", "unavailable", "none", "")
    out = []
    for st in hass.states.async_all("binary_sensor"):
        if (st.attributes.get("device_class") in _OPENING_SENSORS
                and str(st.state).lower() in blank and household.is_way_in(hass, st)):
            out.append(st.entity_id)
    for st in hass.states.async_all("cover"):
        if (st.attributes.get("device_class") in _ENTRY_COVERS
                and str(st.state).lower() in blank and household.is_way_in(hass, st)):
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


def _value(given: Any) -> tuple:
    """(value, source) for a reading a caller passed: a Fact or a plain value."""
    if isinstance(given, Fact):
        return given.value, given.source
    return given, ("" if given == UNKNOWN else "given by the caller")


def read(hass, config: Optional[dict] = None, *, asleep: Any = UNKNOWN,
         quiet_hours: Any = UNKNOWN, tick: int = 0) -> World:
    """Read the house now. `asleep` and `quiet_hours` are Facts from the
    resolvers (or plain values). Never raises."""
    from . import household
    house = _read("household", household._live_snapshot, hass, config)
    asleep_v, asleep_src = _value(asleep)
    quiet_v, quiet_src = _value(quiet_hours)
    return World(
        house=None if house == UNKNOWN else house,
        asleep=asleep_v, quiet_hours=quiet_v,
        read_at=time.time(),
        sources={"house": getattr(house, "source", "unreadable"),
                 "asleep": asleep_src, "quiet_hours": quiet_src},
        open_ways_in=_read("open ways in", _open_ways_in, hass),
        unlocked_locks=_read("unlocked locks", _locks, hass, ("unlocked",)),
        unreadable_locks=_read("unreadable locks", _locks, hass,
                               ("unknown", "unavailable", "jammed")),
        unreadable_openings=_read("unreadable openings", _unreadable_openings, hass),
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
