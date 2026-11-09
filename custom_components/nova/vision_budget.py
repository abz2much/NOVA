"""One daily limit on the camera analyses Nova starts by itself.

Each camera already waits between its own reactions. Nothing limited all the
cameras together, so a fault that made every camera fire (a storm, a bad
sensor, a loop in someone's automation) could run up an unlimited bill with a
paid vision model.

Only the analyses Nova begins on its own count: motion clips, quiet visitor
observations and the package check. A request someone makes, a doorbell press
and the security alarm are never held back.

The count lives in memory and starts again at midnight and on a restart.
Standard library only, so the tests run without Home Assistant.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Optional

DEFAULT_DAILY_LIMIT = 300

_day: Optional[date] = None
_used = 0
_reported_on: Optional[date] = None


def limit_from(value: Any) -> int:
    """The limit a setting means. 0 is no limit; a missing or unreadable
    value is the default, so a typo never switches the limit off."""
    if isinstance(value, bool):
        return DEFAULT_DAILY_LIMIT
    try:
        number = float(str(value).strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMIT
    if not math.isfinite(number) or number < 0:
        return DEFAULT_DAILY_LIMIT
    return int(number)


def _today(now: Optional[datetime]) -> date:
    return (now or datetime.now()).date()


def _roll(now: Optional[datetime]) -> None:
    global _day, _used
    today = _today(now)
    if _day != today:
        _day, _used = today, 0


def exhausted(limit: int, *, now: Optional[datetime] = None) -> bool:
    """Whether today's calls are used up. Does not use one."""
    _roll(now)
    return limit > 0 and _used >= limit


def take(limit: int, *, now: Optional[datetime] = None) -> bool:
    """Use one call. False, and nothing counted, when today's are used up."""
    global _used
    _roll(now)
    if limit > 0 and _used >= limit:
        return False
    _used += 1
    return True


def used(*, now: Optional[datetime] = None) -> int:
    _roll(now)
    return _used


def note_refusal(*, now: Optional[datetime] = None) -> bool:
    """True the first time a refusal is seen on a day, so it is logged once
    rather than for every event."""
    global _reported_on
    today = _today(now)
    if _reported_on == today:
        return False
    _reported_on = today
    return True


def status(limit: int, *, now: Optional[datetime] = None) -> dict:
    _roll(now)
    return {"limit": limit, "used": _used,
            "left": None if limit <= 0 else max(0, limit - _used)}


def reset() -> None:
    global _day, _used, _reported_on
    _day, _used, _reported_on = None, 0, None
