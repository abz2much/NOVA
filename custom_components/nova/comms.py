"""Calendar agent for Nova.

Looks at the current/next event exposed by each Home Assistant ``calendar.*``
entity, spots double-bookings and too-tight transitions, and turns the result
into a short, speakable agenda. Every time is compared as Home Assistant's
local wall-clock time: a timestamp with a UTC offset is converted to it, and
one without is taken as already local.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from .safe_errors import safe_error_message

_DEFAULT_TIGHT_GAP_MIN = 15

# Date, a 'T' or space separator, then HH:MM with optional :SS. Fractional
# seconds are ignored; a trailing 'Z' or UTC offset is read by _offset.
_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?")
_OFFSET_RE = re.compile(r"(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})$")


def _local(dt: datetime) -> datetime:
    """An aware datetime as naive Home Assistant local time."""
    try:
        from homeassistant.util import dt as dt_util
        return dt_util.as_local(dt).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - outside HA, use the system zone
        return dt.astimezone().replace(tzinfo=None)


def _now() -> datetime:
    """Naive Home Assistant local time now."""
    return _local(datetime.now(timezone.utc))


def _offset(text: str, m: "re.Match[str]") -> Optional[timezone]:
    """The UTC offset written after the time, if any."""
    tail = _OFFSET_RE.match(text[m.end():])
    if not tail:
        return None
    raw = tail.group(1)
    if raw == "Z":
        return timezone.utc
    sign = -1 if raw[0] == "-" else 1
    digits = raw[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))


def _parse(ts: Any) -> Optional[datetime]:
    """Parse an HA calendar timestamp into a naive local datetime."""
    if not ts:
        return None
    try:
        text = str(ts).strip()
        if "T" in text or (" " in text and ":" in text):
            m = _DATETIME_RE.match(text)
            if not m:
                return None
            day = date.fromisoformat(m.group(1))
            when = datetime(day.year, day.month, day.day,
                            int(m.group(2)), int(m.group(3)), int(m.group(4) or 0))
            zone = _offset(text, m)
            return when if zone is None else _local(when.replace(tzinfo=zone))
        day = date.fromisoformat(text[:10])
        return datetime(day.year, day.month, day.day)
    except (ValueError, TypeError):
        return None


def gather_events(hass) -> list[dict]:
    """Collect one event dict per calendar entity that has a parseable start."""
    if hass is None:
        return []
    try:
        states = list(hass.states.async_all("calendar"))
    except Exception:  # noqa: BLE001 - a broken state machine means no events
        return []

    events: list[dict] = []
    for state in states:
        attrs = state.attributes or {}
        start = _parse(attrs.get("start_time"))
        if start is None:
            continue
        end = _parse(attrs.get("end_time")) or start + timedelta(hours=1)
        title = attrs.get("message") or attrs.get("friendly_name") or state.entity_id
        events.append({
            "calendar": state.entity_id,
            "title": str(title),
            "start": start,
            "end": end,
            "all_day": bool(attrs.get("all_day")),
            "location": attrs.get("location"),
            "active": state.state == "on",
        })
    return events


def find_conflicts(events: list[dict], tight_gap_min: int) -> dict:
    """Find overlapping pairs and too-short gaps between timed events."""
    gap = timedelta(minutes=max(0, int(tight_gap_min)))
    timed = sorted((e for e in events if not e["all_day"]), key=lambda e: e["start"])

    overlaps: list[tuple[dict, dict]] = []
    tight: list[tuple[dict, dict]] = []
    for i, a in enumerate(timed):
        checked_tight = False
        for b in timed[i + 1:]:
            if b["start"] < a["end"]:
                overlaps.append((a, b))
            elif not checked_tight:
                # Only the first event starting after `a` ends is checked.
                checked_tight = True
                if b["start"] - a["end"] < gap:
                    tight.append((a, b))
    return {"overlaps": overlaps, "tight": tight}


def _clock(dt: datetime) -> str:
    """12-hour clock time, e.g. '9:05 AM' (glibc-only '%-I')."""
    return dt.strftime("%-I:%M %p")


def _tight_gap() -> int:
    try:
        from . import nova_config
        value = nova_config.get("calendar_tight_gap_min", _DEFAULT_TIGHT_GAP_MIN)
    except Exception:  # noqa: BLE001 - config trouble falls back to the default
        value = _DEFAULT_TIGHT_GAP_MIN
    if value is None:
        value = _DEFAULT_TIGHT_GAP_MIN
    return int(value)


def agenda(hass, horizon_hours: int = 24) -> dict:
    """Spoken-style agenda for the next ``horizon_hours`` hours. Never raises."""
    try:
        gap = _tight_gap()
        now = _now()
        horizon = now + timedelta(hours=max(1, int(horizon_hours)))
        kept = sorted(
            (e for e in gather_events(hass) if e["start"] <= horizon and e["end"] >= now),
            key=lambda e: e["start"],
        )

        lines = [
            f"{e['title']} — {'all day' if e['all_day'] else _clock(e['start'])}"
            for e in kept
        ]

        found = find_conflicts(kept, gap)
        conflicts = [
            f"conflict: “{a['title']}” and “{b['title']}” overlap "
            f"({_clock(a['start'])} / {_clock(b['start'])})"
            for a, b in found["overlaps"]
        ]
        for a, b in found["tight"]:
            minutes = int((b["start"] - a["end"]).total_seconds() // 60)
            conflicts.append(f"tight: only {minutes} min between “{a['title']}” and “{b['title']}”")

        return {"events": lines, "conflicts": conflicts, "count": len(kept)}
    except Exception as exc:  # noqa: BLE001 - the agenda must never raise
        return {"events": [], "conflicts": [], "count": 0,
                "error": safe_error_message(exc, where="agenda", log=True)}
