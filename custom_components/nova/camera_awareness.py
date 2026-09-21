"""Bounded historical awareness from Phase 4 semantic camera events.

This module reads only canonical ``camera_event.*`` rows from the shared
``patterns.db`` state change table. It does no perception, recognition or
model work and stores no derived copy of the observations.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import re
import sqlite3
from typing import Iterable


DB_PATH = "/config/nova/patterns.db"

DEFAULT_LOOKBACK_DAYS = 30
MIN_LOOKBACK_DAYS = 7
MAX_LOOKBACK_DAYS = 90

DEFAULT_MIN_OBSERVATIONS = 3
MIN_MIN_OBSERVATIONS = 3
MAX_MIN_OBSERVATIONS = 12

DEFAULT_MAX_OBSERVATIONS = 3
MIN_RETURNED_OBSERVATIONS = 1
MAX_RETURNED_OBSERVATIONS = 5

MAX_ROWS = 2_000
MAX_PROMPT_CHARS = 2_400
_MIN_RESIDENT_CONFIDENCE = 60.0
_CONCENTRATION_RATIO = 0.70

_CANONICAL_STATES = (
    "person",
    "vehicle",
    "animal",
    "activity",
    "package",
    "package_delivered",
    "package_stranded",
    "package_taken",
    "package_mail",
)


@dataclass(frozen=True)
class AwarenessConfig:
    lookback_days: int
    min_observations: int
    max_observations: int


@dataclass(frozen=True)
class CameraEvent:
    timestamp: datetime
    state: str
    location: str
    hour: int
    weekday: int
    resident: str = ""


@dataclass(frozen=True)
class Observation:
    state: str
    location: str
    resident: str
    count: int
    distinct_days: int
    latest: datetime
    daypart: str = ""
    weekdays: bool = False
    score: float = 0.0


def _bounded_int(value, default: int, low: int, high: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, number))


def resolve_config(config: dict | None) -> AwarenessConfig:
    """Resolve and clamp the three bounded awareness controls."""
    config = config or {}
    return AwarenessConfig(
        lookback_days=_bounded_int(
            config.get("camera_awareness_lookback_days"),
            DEFAULT_LOOKBACK_DAYS,
            MIN_LOOKBACK_DAYS,
            MAX_LOOKBACK_DAYS,
        ),
        min_observations=_bounded_int(
            config.get("camera_awareness_min_observations"),
            DEFAULT_MIN_OBSERVATIONS,
            MIN_MIN_OBSERVATIONS,
            MAX_MIN_OBSERVATIONS,
        ),
        max_observations=_bounded_int(
            config.get("camera_awareness_max_observations"),
            DEFAULT_MAX_OBSERVATIONS,
            MIN_RETURNED_OBSERVATIONS,
            MAX_RETURNED_OBSERVATIONS,
        ),
    )


def _safe_words(value, max_len: int) -> str:
    """Return bounded prose text with controls and prompt syntax removed."""
    text = str(value or "")[: max_len * 2].replace("_", " ")
    text = "".join(
        char if char.isalnum() or char in " .'’-" else " " for char in text
    )
    return re.sub(r"\s+", " ", text).strip(" .-'’")[:max_len].strip()


def _known_resident(person, confidence) -> str:
    try:
        score = float(confidence or 0.0)
    except (TypeError, ValueError, OverflowError):
        return ""
    name = _safe_words(person, 60)
    if not math.isfinite(score) or score < _MIN_RESIDENT_CONFIDENCE:
        return ""
    if name.lower() in ("", "unknown", "stranger", "none", "unavailable"):
        return ""
    return name


def read_recent_events(
    db_path: str = DB_PATH,
    *,
    now: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[CameraEvent]:
    """Read a bounded set of canonical camera rows from the shared store."""
    now = now or datetime.now()
    lookback_days = _bounded_int(
        lookback_days, DEFAULT_LOOKBACK_DAYS, MIN_LOOKBACK_DAYS, MAX_LOOKBACK_DAYS,
    )
    cutoff = (now - timedelta(days=lookback_days)).isoformat()
    placeholders = ",".join("?" for _ in _CANONICAL_STATES)
    query = f"""
        SELECT timestamp, new_state, area_id, person, person_confidence
        FROM state_changes
        WHERE domain = ?
          AND triggered_by = ?
          AND entity_id LIKE 'camera_event.%'
          AND timestamp >= ?
          AND new_state IN ({placeholders})
        ORDER BY timestamp DESC
        LIMIT ?
    """
    params = (
        "camera_event", "camera", cutoff, *_CANONICAL_STATES, MAX_ROWS,
    )
    events: list[CameraEvent] = []
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(query, params).fetchall()
    except Exception:
        return []

    for timestamp, state, area_id, person, person_confidence in rows:
        try:
            occurred = datetime.fromisoformat(str(timestamp))
            if occurred < now - timedelta(days=lookback_days) or occurred > now + timedelta(minutes=5):
                continue
            location = _safe_words(area_id, 64)
            if not location:
                continue
            resident = (
                _known_resident(person, person_confidence)
                if state == "person"
                else ""
            )
            events.append(CameraEvent(
                timestamp=occurred,
                state=state,
                location=location,
                hour=occurred.hour,
                weekday=occurred.weekday(),
                resident=resident,
            ))
        except Exception:
            continue
    return events


def _daypart(hour: int) -> str:
    if 0 <= hour <= 5:
        return "overnight"
    if 6 <= hour <= 10:
        return "in the morning"
    if 11 <= hour <= 13:
        return "around midday"
    if 14 <= hour <= 16:
        return "in the afternoon"
    if 17 <= hour <= 19:
        return "in the early evening"
    if 20 <= hour <= 22:
        return "in the evening"
    return "late at night"


def _concentrated_daypart(events: list[CameraEvent]) -> str:
    counts = Counter(_daypart(event.hour) for event in events)
    phrase, count = counts.most_common(1)[0]
    return phrase if count / len(events) >= _CONCENTRATION_RATIO else ""


def _weekday_concentration(events: list[CameraEvent]) -> bool:
    weekday_events = [event for event in events if event.weekday < 5]
    distinct_weekdays = {event.timestamp.date() for event in weekday_events}
    return (
        len(distinct_weekdays) >= 3
        and len(weekday_events) / len(events) >= 0.80
    )


def select_observations(
    events: Iterable[CameraEvent],
    *,
    now: datetime | None = None,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
    max_observations: int = DEFAULT_MAX_OBSERVATIONS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[Observation]:
    """Group, rank and diversify repeated historical observations."""
    now = now or datetime.now()
    min_observations = _bounded_int(
        min_observations,
        DEFAULT_MIN_OBSERVATIONS,
        MIN_MIN_OBSERVATIONS,
        MAX_MIN_OBSERVATIONS,
    )
    max_observations = _bounded_int(
        max_observations,
        DEFAULT_MAX_OBSERVATIONS,
        MIN_RETURNED_OBSERVATIONS,
        MAX_RETURNED_OBSERVATIONS,
    )
    lookback_days = _bounded_int(
        lookback_days, DEFAULT_LOOKBACK_DAYS, MIN_LOOKBACK_DAYS, MAX_LOOKBACK_DAYS,
    )

    grouped: dict[tuple[str, str, str], list[CameraEvent]] = defaultdict(list)
    for event in events:
        grouped[(event.state, event.location, event.resident)].append(event)

    candidates: list[Observation] = []
    for (state, location, resident), group in grouped.items():
        days = {event.timestamp.date() for event in group}
        if len(group) < min_observations or len(days) < 2:
            continue
        latest = max(event.timestamp for event in group)
        age_days = max(0.0, (now - latest).total_seconds() / 86_400)
        recency = max(0.0, 1.0 - age_days / lookback_days)
        daypart = _concentrated_daypart(group)
        weekday = _weekday_concentration(group)
        score = (
            min(len(group), 20) * 3.0
            + min(len(days), 10) * 2.0
            + recency * 5.0
            + (2.0 if daypart else 0.0)
            + (1.0 if weekday else 0.0)
        )
        candidates.append(Observation(
            state=state,
            location=location,
            resident=resident,
            count=len(group),
            distinct_days=len(days),
            latest=latest,
            daypart=daypart,
            weekdays=weekday,
            score=score,
        ))

    candidates.sort(
        key=lambda item: (item.score, item.latest, item.count), reverse=True,
    )
    selected: list[Observation] = []
    used_locations: set[str] = set()
    for candidate in candidates:
        if candidate.location in used_locations:
            continue
        selected.append(candidate)
        used_locations.add(candidate.location)
        if len(selected) >= max_observations:
            break
    return selected


def _timing_suffix(observation: Observation) -> str:
    parts = []
    if observation.daypart:
        parts.append(observation.daypart)
    if observation.weekdays:
        parts.append("on weekdays")
    return (" " + " ".join(parts)) if parts else ""


def render_observation(observation: Observation) -> str:
    """Render one deliberately historical, low precision sentence."""
    location = observation.location
    suffix = _timing_suffix(observation)
    if observation.state == "person":
        if observation.resident:
            return f"{observation.resident} has appeared at the {location}{suffix}."
        return f"I've often seen a person at the {location}{suffix}."
    if observation.state == "vehicle":
        return f"I've often seen a vehicle at the {location}{suffix}."
    if observation.state == "animal":
        return f"I've often seen an animal at the {location}{suffix}."
    if observation.state == "activity":
        return f"I've often noticed activity at the {location}{suffix}."
    if observation.state == "package":
        return f"I've often seen packages at the {location}{suffix}."
    if observation.state == "package_delivered":
        return f"Packages have often been delivered at the {location}{suffix}."
    if observation.state == "package_taken":
        return f"Packages have often been taken from the {location}{suffix}."
    if observation.state == "package_stranded":
        return f"I have repeatedly seen packages left waiting at the {location}{suffix}."
    if observation.state == "package_mail":
        return f"Mail has often arrived at the {location}{suffix}."
    return ""


def _enabled(config: dict) -> bool:
    if config.get("observer_enabled", True) is False:
        return False
    if config.get("cognition_enabled", True) is False:
        return False
    if config.get("camera_event_learning", True) is False:
        return False
    return config.get(
        "camera_historical_awareness",
        bool(config.get("camera_event_learning", True)),
    ) is not False


def build_prompt(
    config: dict | None,
    *,
    db_path: str = DB_PATH,
    now: datetime | None = None,
    _fence_token: str | None = None,
) -> str:
    """Build the fenced prompt block, or an empty string on any failure."""
    config = config or {}
    if not _enabled(config):
        return ""
    settings = resolve_config(config)
    events = read_recent_events(
        db_path, now=now, lookback_days=settings.lookback_days,
    )
    if not events:
        return ""
    observations = select_observations(
        events,
        now=now,
        min_observations=settings.min_observations,
        max_observations=settings.max_observations,
        lookback_days=settings.lookback_days,
    )
    lines = [render_observation(observation) for observation in observations]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    from .prompt_fence import fence

    while lines:
        fenced = fence(
            "\n".join(f"• {line}" for line in lines),
            label="CAMERA_AWARENESS",
            noun="are bounded summaries of repeated historical camera events",
            callback_noun="historical camera data",
            extra_instruction=(
                "never present these patterns as proof of what is happening now"
            ),
            _token=_fence_token,
        )
        block = f"## What I've noticed lately\n{fenced}\n\n"
        if len(block) <= MAX_PROMPT_CHARS:
            return block
        lines.pop()
    return ""
