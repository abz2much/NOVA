"""Regular departures: when a person usually leaves, and when to remind them.

Input is every stable departure Nova sampled, as (local day ordinal,
seconds since local midnight). Departures are grouped by day type (weekdays,
weekends) and by time of day, so a 09:00 errand and a 12:00 school run are
two routines, and a weekday habit says nothing about Saturday.

A group is a routine when, like every other routine Nova learns, it happened
on at least MIN_DAYS days, within a MAX_SPREAD standard deviation, on at
least MIN_SHARE of that day type's days in its span, and on at least one of
the last RECENT_DAYS days of that type (so a school holiday quietens it after
a few days, and the next departure brings it back).

Pure: no Home Assistant, storage or speech. Never raises.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

MIN_DAYS = 7
MAX_SPREAD = 2700          # seconds (45 min)
MIN_SHARE = 0.6
CLUSTER_GAP = 2700         # departures further apart than this are separate
RECENT_DAYS = 3
LATE_MIN = 1800            # the "still home" check: at least 30 min late …
LATE_MAX = 3600            # … at most 60, widened by the routine's spread
LATE_WINDOW = 7200         # and only for two hours after that
WEEKDAY, WEEKEND = "weekday", "weekend"


@dataclass(frozen=True)
class Departure:
    day_type: str
    mean: float            # seconds since midnight
    spread: float          # standard deviation, seconds

    @property
    def key(self) -> str:
        return f"{self.day_type}:{hhmm(self.mean)}"

    @property
    def late_after(self) -> float:
        return self.mean + min(max(2 * self.spread, LATE_MIN), LATE_MAX)


def day_type(day: int) -> str:
    """Day type of a proleptic Gregorian ordinal (day 1 was a Monday)."""
    return WEEKEND if (int(day) - 1) % 7 >= 5 else WEEKDAY


def hhmm(secs: float) -> str:
    return f"{int(secs // 3600):02d}:{int((secs % 3600) // 60):02d}"


def _clusters(points: list) -> list:
    points = sorted(points, key=lambda p: p[1])
    groups: list = []
    for p in points:
        if groups and p[1] - groups[-1][-1][1] <= CLUSTER_GAP:
            groups[-1].append(p)
        else:
            groups.append([p])
    return groups


def _routine(group: list, kind: str, today: int) -> Optional[Departure]:
    first: dict = {}
    for d, s in group:                     # one departure per day: the earliest
        if d not in first or s < first[d]:
            first[d] = s
    if len(first) < MIN_DAYS:
        return None
    secs = list(first.values())
    mean = sum(secs) / len(secs)
    spread = (sum((x - mean) ** 2 for x in secs) / len(secs)) ** 0.5
    if spread > MAX_SPREAD:
        return None
    lo, hi = min(first), max(first)
    span = sum(1 for d in range(lo, hi + 1) if day_type(d) == kind)
    if not span or len(first) / span < MIN_SHARE:
        return None
    recent: list[int] = []
    d = today - 1
    while len(recent) < RECENT_DAYS and d >= lo:
        if day_type(d) == kind:
            recent.append(d)
        d -= 1
    if recent and not any(r in first for r in recent):
        return None
    return Departure(kind, mean, spread)


def departures(points: Iterable, today: int, kind: Optional[str] = None) -> list:
    """The regular departures for today's day type (or `kind`), earliest
    first. Points from today itself are not evidence for today's routine."""
    kind = kind or day_type(today)
    pts = []
    for p in points or ():
        try:
            d, s = int(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        if d < today and day_type(d) == kind:
            pts.append((d, s))
    out = [r for r in (_routine(g, kind, today) for g in _clusters(pts)) if r]
    return sorted(out, key=lambda r: r.mean)


def left_for(routine: Departure, today_departures: Iterable) -> bool:
    """True when one of today's departures already covers this routine."""
    return any(s >= routine.mean - CLUSTER_GAP for s in today_departures)


def reminder_due(routine: Departure, now_secs: float, lead_secs: float) -> bool:
    return routine.mean - lead_secs <= now_secs < routine.mean


def late_due(routine: Departure, now_secs: float) -> bool:
    return routine.late_after < now_secs <= routine.late_after + LATE_WINDOW


def reminder_text(routine: Departure, now_secs: float, name: str = "") -> str:
    lead = f"{name}, you" if name else "You"
    return f"{lead} usually leave around {hhmm(routine.mean)}. It's {hhmm(now_secs)} now."


def late_text(routine: Departure, name: str = "") -> str:
    if name:
        return f"{name}, you're usually out by {hhmm(routine.mean)} and you're still home."
    return f"You're usually out by {hhmm(routine.mean)} and you're still home."
