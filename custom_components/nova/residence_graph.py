"""Room-adjacency graph derived from the Residence tab floor plan.

The user draws rectangles per floor; rooms on the same floor that touch (or sit
within a small gap of each other) are treated as neighbours. Rooms are matched
to Home Assistant areas by slugified name, so the intrusion investigator can ask
which areas border a breach and how many rooms away each area is. Any missing or
mismatched data simply yields empty results.
"""
from __future__ import annotations

import json
import re
from collections import deque
from typing import Any, Iterator, Optional

Box = tuple[str, float, float, float, float]

_WHITESPACE = re.compile(r"\s+")


def slug(s: Any) -> str:
    """Normalise a name: trimmed, lower-case, whitespace runs become '_'."""
    if s is None or s == "":
        return ""
    return _WHITESPACE.sub("_", str(s).strip().lower())


def _parse_room(room: Any) -> Optional[Box]:
    """Turn a room dict into a box, or None if it is incomplete or malformed."""
    try:
        name = room["name"]
        x = float(room["x"])
        y = float(room["y"])
        w = float(room.get("w", 0) or 0)
        h = float(room.get("h", 0) or 0)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return (name, x, y, w, h)


def _floors(config: dict) -> Iterator[list[Box]]:
    """Yield the list of valid room boxes for each floor of the plan."""
    raw = config.get("floor_plan_rooms")
    if not raw:
        return
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return
        if not raw:
            return
    # A non-dict plan raises AttributeError here (preserved behaviour).
    for floor in raw.values():
        if not floor:
            continue
        rooms = (floor.get("rooms") or []) if isinstance(floor, dict) else floor
        boxes = [b for b in (_parse_room(r) for r in rooms) if b is not None]
        yield boxes


def _touch(b1: Box, b2: Box, gap: float = 10.0) -> bool:
    """True when two boxes overlap or are closer than ``gap`` on both axes."""
    _, x1, y1, w1, h1 = b1
    _, x2, y2, w2, h2 = b2
    near_x = x1 < x2 + w2 + gap and x1 + w1 + gap > x2
    near_y = y1 < y2 + h2 + gap and y1 + h1 + gap > y2
    return near_x and near_y


def room_adjacency(config: dict) -> dict[str, set[str]]:
    """Map each room slug to the slugs of rooms touching it on the same floor."""
    graph: dict[str, set[str]] = {}
    for boxes in _floors(config):
        for i, first in enumerate(boxes):
            for second in boxes[i + 1:]:
                a, b = slug(first[0]), slug(second[0])
                if a == b or not _touch(first, second):
                    continue
                graph.setdefault(a, set()).add(b)
                graph.setdefault(b, set()).add(a)
    return graph


def _area_slug(hass: Any, area_id: str) -> str:
    """Slug of the area's registry name, falling back to the slugged id."""
    try:
        from homeassistant.helpers import area_registry as ar

        area = ar.async_get(hass).async_get_area(area_id)
        if area is not None and area.name:
            return slug(area.name)
    except Exception:  # noqa: BLE001 - registry trouble means "use the id"
        pass
    return slug(area_id)


def _list_areas(hass: Any) -> list[Any]:
    from homeassistant.helpers import area_registry as ar

    return list(ar.async_get(hass).async_list_areas())


def adjacent_areas(hass: Any, config: dict, breach_area: Optional[str]) -> set[str]:
    """HA area ids whose rooms border the breach area's room."""
    if not breach_area:
        return set()
    graph = room_adjacency(config)
    if not graph:
        return set()
    neighbours = graph.get(_area_slug(hass, breach_area), set())
    if not neighbours:
        return set()
    try:
        return {a.id for a in _list_areas(hass) if slug(a.name) in neighbours}
    except Exception:  # noqa: BLE001
        return set()


def hops_from_breach(hass: Any, config: dict, breach_area: Optional[str]) -> dict[str, int]:
    """HA area id -> number of rooms between it and the breach area's room."""
    if not breach_area:
        return {}
    graph = room_adjacency(config)
    if not graph:
        return {}
    start = _area_slug(hass, breach_area)
    if start not in graph and not any(start in n for n in graph.values()):
        return {}

    distance = {start: 0}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for nxt in graph.get(current, ()):
            if nxt not in distance:
                distance[nxt] = distance[current] + 1
                queue.append(nxt)

    try:
        areas = _list_areas(hass)
    except Exception:  # noqa: BLE001
        return {}
    result: dict[str, int] = {}
    for area in areas:
        key = slug(area.name)
        if key in distance:
            result[area.id] = distance[key]
    return result
