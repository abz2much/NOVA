"""Pure helpers for area-presence conditions on learned automations."""
from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass, field
from typing import Mapping, Sequence


MIN_SAMPLES = 5
GATE_RATIO = 0.8
RELEASE_RATIO = 0.8
RELEASE_WINDOW_SECONDS = 15 * 60
MAX_SETTLE_SECONDS = 10 * 60


@dataclass(frozen=True, slots=True)
class AreaPresenceContext:
    """Recorder history and area metadata gathered on HA's event loop."""

    sensor_history: Mapping[str, tuple[tuple[float, bool], ...]] = field(
        default_factory=dict)
    entity_areas: Mapping[str, str] = field(default_factory=dict)
    area_sensors: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    area_names: Mapping[str, str] = field(default_factory=dict)


EMPTY_CONTEXT = AreaPresenceContext()


def state_at(history: Sequence[tuple[float, bool]], when: float) -> bool | None:
    """Return the last known boolean state at ``when``."""
    if not history:
        return None
    index = bisect.bisect_right(history, when, key=lambda event: event[0]) - 1
    return None if index < 0 else bool(history[index][1])


def presence_gate_condition(
    action_entity: str,
    trigger_times: Sequence[float],
    context: AreaPresenceContext,
    *,
    exclude: Sequence[str] = (),
) -> dict | None:
    """Return the strongest same-area gate measured at automation trigger time."""
    if len(trigger_times) < MIN_SAMPLES:
        return None
    area_id = context.entity_areas.get(action_entity)
    if not area_id:
        return None
    excluded = set(exclude)
    needed = max(MIN_SAMPLES, math.ceil(GATE_RATIO * len(trigger_times)))
    best: dict | None = None
    best_count = -1
    for sensor_id in context.area_sensors.get(area_id, ()):
        if sensor_id in excluded:
            continue
        history = context.sensor_history.get(sensor_id, ())
        states = [state for _epoch, state in history]
        if not states or not any(states) or all(states):
            continue
        on_count = sum(state_at(history, when) is True for when in trigger_times)
        if on_count >= needed and on_count > best_count:
            best_count = on_count
            best = {
                "entity_id": sensor_id,
                "area_id": area_id,
                "area_name": context.area_names.get(area_id, area_id),
                "condition": {
                    "condition": "state",
                    "entity_id": sensor_id,
                    "state": "on",
                },
            }
    return best


def presence_release(
    action_entity: str,
    off_times: Sequence[float],
    context: AreaPresenceContext,
    gate: Mapping[str, object] | None,
) -> dict | None:
    """Return release timing when off events reliably follow the gate clearing."""
    if not gate or len(off_times) < MIN_SAMPLES:
        return None
    area_id = context.entity_areas.get(action_entity)
    sensor_id = gate.get("entity_id")
    if not area_id or not isinstance(sensor_id, str):
        return None
    if gate.get("area_id") != area_id:
        return None
    history = sorted(context.sensor_history.get(sensor_id, ()))
    clears = [
        history[index][0]
        for index in range(1, len(history))
        if history[index - 1][1] and not history[index][1]
    ]
    if len(clears) < MIN_SAMPLES:
        return None

    used: set[int] = set()
    lags: list[float] = []
    for off_time in sorted(off_times):
        index = bisect.bisect_right(clears, off_time) - 1
        if index < 0 or index in used:
            continue
        lag = off_time - clears[index]
        if 0 <= lag <= RELEASE_WINDOW_SECONDS:
            used.add(index)
            lags.append(lag)
    needed = max(MIN_SAMPLES, math.ceil(RELEASE_RATIO * len(off_times)))
    if len(lags) < needed:
        return None
    settle = round(statistics.median(lags) / 30.0) * 30
    return {
        "entity_id": sensor_id,
        "area_id": area_id,
        "area_name": context.area_names.get(area_id, area_id),
        "settle_seconds": int(min(MAX_SETTLE_SECONDS, max(0, settle))),
    }


def numeric_trigger_times(
    sensor_history: Sequence[tuple[float, float]],
    operator: str,
    threshold: float,
    action_times: Sequence[float],
    *,
    window_seconds: float = 600.0,
) -> list[float]:
    """Pair actions with unique preceding crossings of a numeric threshold."""
    events = sorted(sensor_history)
    crossings: list[float] = []
    for previous, current in zip(events, events[1:]):
        before, after = previous[1], current[1]
        if operator == "below" and before >= threshold > after:
            crossings.append(current[0])
        elif operator == "above" and before <= threshold < after:
            crossings.append(current[0])
    used: set[int] = set()
    paired: list[float] = []
    for action_time in sorted(action_times):
        index = bisect.bisect_right(crossings, action_time) - 1
        if index < 0 or index in used:
            continue
        if action_time - crossings[index] <= window_seconds:
            used.add(index)
            paired.append(crossings[index])
    return paired
