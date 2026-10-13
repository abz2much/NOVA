"""Answer "what happened?" questions from Home Assistant's own records.

Two read-only views are offered: the recorder's state-change timeline for a
set of entities, and the logbook's human-readable narrative. Every failure is
reported as an empty result or an ``{"error": ...}`` dict; nothing here
raises, and nothing is invented.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

_MAX_ENTITIES = 20
_MAX_TIMELINE = 200
_MAX_LOGBOOK = 100
_HISTORY_MAX_HOURS = 720.0  # 30 days
_LOGBOOK_MAX_HOURS = 336.0  # 14 days
_LOGBOOK_FUNCTIONS = ("async_get_events", "get_events", "_get_events")


def _clamp_hours(hours: Any, upper: float) -> float:
    """Coerce ``hours`` to a float in ``[0.1, upper]``; junk becomes 24."""
    try:
        value = float(hours)
    except (TypeError, ValueError):
        return 24.0
    return max(0.1, min(upper, value))


def _resolve_entities(hass: Any, entity: str | None, area: str | None) -> list[str]:
    """Turn a user-supplied entity hint or area name into entity ids."""
    found: list[str] = []
    try:
        if entity:
            if hass.states.get(entity) is not None:
                return [entity]
            needle = entity.lower()
            for state in hass.states.async_all():
                name = str(state.attributes.get("friendly_name") or "").lower()
                if needle in name or needle in state.entity_id.lower():
                    found.append(state.entity_id)
                    if len(found) >= _MAX_ENTITIES:
                        break
            return found
        if area:
            from homeassistant.helpers import area_registry as ar
            from homeassistant.helpers import entity_registry as er

            needle = area.lower()
            match = next(
                (a for a in ar.async_get(hass).async_list_areas()
                 if a.name and needle in a.name.lower()),
                None,
            )
            if match is None:
                return []
            entries = er.async_entries_for_area(er.async_get(hass), match.id)
            found = [e.entity_id for e in entries][:_MAX_ENTITIES]
    except Exception:  # noqa: BLE001 - never raise to the caller
        pass
    return found


def _row_fields(row: Any) -> tuple[Any, Any]:
    """Return ``(state, time)`` for an object- or dict-shaped history row."""
    if isinstance(row, dict):
        return row.get("state"), row.get("last_changed") or row.get("last_updated")
    return (
        getattr(row, "state", None),
        getattr(row, "last_changed", None) or getattr(row, "last_updated", None),
    )


async def entity_history(
    hass: Any,
    entity: str | None = None,
    area: str | None = None,
    hours: float = 24.0,
) -> dict:
    """Recorder state-change timeline for the matching entities."""
    try:
        from homeassistant.components.recorder import get_instance, history
        from homeassistant.util import dt as dt_util
    except Exception:  # noqa: BLE001
        return {"error": "history recorder is not available"}

    ids = _resolve_entities(hass, entity, area)
    if not ids:
        hint = f"entity={entity}" if entity else f"area={area}"
        return {"error": f"no matching entities for {hint}"}

    span = _clamp_hours(hours, _HISTORY_MAX_HOURS)
    end = dt_util.utcnow()
    start = end - timedelta(hours=span)

    def _query() -> Any:
        return history.get_significant_states(
            hass, start, end, ids, minimal_response=True, no_attributes=True
        )

    try:
        states = await get_instance(hass).async_add_executor_job(_query)
    except Exception:  # noqa: BLE001
        return {"error": "could not read history for that period"}

    if not states:
        return {"entities": ids, "hours": span, "timeline": [], "counts": {},
                "note": "no recorded changes in that window"}

    timeline: list[dict] = []
    counts: dict[str, int] = {}
    for entity_id, rows in states.items():
        kept = 0
        for row in rows:
            state, when = _row_fields(row)
            if state is None:
                continue
            stamp = when.isoformat() if hasattr(when, "isoformat") else str(when)
            timeline.append({"entity_id": entity_id, "state": state, "when": stamp})
            kept += 1
        counts[entity_id] = kept

    try:
        # Lexical sort on the ISO text (see spec quirk Q6).
        timeline.sort(key=lambda item: item["when"], reverse=True)
    except Exception:  # noqa: BLE001
        pass

    return {"entities": ids, "hours": span, "timeline": timeline[:_MAX_TIMELINE],
            "counts": counts}


def _fetch_logbook(module: Any, hass: Any, start: Any, end: Any,
                   entity_id: str | None) -> list:
    """Try each known logbook reader in turn; return the first success."""
    for name in _LOGBOOK_FUNCTIONS:
        fn = getattr(module, name, None)
        if fn is None:
            continue
        try:
            if entity_id:
                return list(fn(hass, start, end, [entity_id]))
            return list(fn(hass, start, end))
        except TypeError:
            # Older signature without entity scoping: widen to the whole house.
            try:
                return list(fn(hass, start, end))
            except Exception:  # noqa: BLE001
                continue
        except Exception:  # noqa: BLE001
            continue
    return []


async def logbook(hass: Any, entity: str | None = None, hours: float = 24.0) -> dict:
    """Human-readable logbook entries for the window, optionally for one entity."""
    try:
        from homeassistant.util import dt as dt_util
    except Exception:  # noqa: BLE001
        return {"error": "time utilities unavailable"}

    span = _clamp_hours(hours, _LOGBOOK_MAX_HOURS)
    end = dt_util.utcnow()
    start = end - timedelta(hours=span)

    entity_id: str | None = None
    if entity:
        matches = _resolve_entities(hass, entity, None)
        entity_id = matches[0] if matches else entity

    try:
        from homeassistant.components import logbook as logbook_module
        from homeassistant.components.recorder import get_instance
    except Exception:  # noqa: BLE001
        return {"error": "logbook is not available"}

    try:
        rows = await get_instance(hass).async_add_executor_job(
            _fetch_logbook, logbook_module, hass, start, end, entity_id
        )
    except Exception:  # noqa: BLE001
        rows = None

    if not rows:
        return {"hours": span, "entries": [],
                "note": "no logbook entries for that window "
                        "(or logbook unavailable on this HA build)"}

    entries = [
        {
            "when": str(row.get("when") or row.get("timestamp") or ""),
            "name": row.get("name") or "",
            "message": row.get("message") or row.get("state") or "",
            "entity_id": row.get("entity_id"),
        }
        for row in rows[:_MAX_LOGBOOK]
        if isinstance(row, dict)
    ]
    return {"hours": span, "entries": entries}
