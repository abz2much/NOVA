"""Read only home tools: entity state, entity search, a room's devices, the
home summary and recorded history."""
from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from . import memory as _memory

# The whole agent logs as ...nova.agent, the name it always had, so filters
# and levels set for it still apply.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")

_STATE_LIMIT = 20
_AREA_LIST_LIMIT = 30
# The attributes worth showing the model for an entity.
_USEFUL_ATTRIBUTES = (
    "friendly_name", "brightness", "temperature", "current_temperature",
    "humidity", "unit_of_measurement", "device_class", "battery_level",
    "media_title", "volume_level", "source",
)
_OPENINGS = ("door", "window", "garage_door")


def _name(state) -> str:
    return state.attributes.get("friendly_name", state.entity_id)


def _iso(moment):
    return moment.isoformat() if moment else None


async def _exec_get_entity_state(hass: HomeAssistant, args: dict) -> str:
    """State, useful attributes and change times for up to 20 entities.
    last_changed is what answers "when did this turn on?"."""
    out = []
    for eid in args.get("entity_ids", [])[:_STATE_LIMIT]:
        state = hass.states.get(eid)
        if not state:
            out.append({"entity_id": eid, "error": "not found"})
            continue
        attrs = state.attributes
        out.append({
            "entity_id": eid,
            "state": state.state,
            "attributes": {k: attrs[k] for k in _USEFUL_ATTRIBUTES if k in attrs},
            "last_changed": state.last_changed.isoformat() if state.last_changed else None,
            "last_updated": _iso(state.last_updated),
        })
    return json.dumps(out)


# ── search and clarification ────────────────────────────────────────────────

def _dedupe_candidates(items: list[dict]) -> list[dict]:
    """One candidate per entity_id, the first kept, so a clarification never
    offers the same entity twice."""
    first: dict = {}
    for item in items:
        first.setdefault(item["entity_id"], item)
    return list(first.values())


def _build_clarification(candidates: list[dict], hass=None) -> str:
    """The fixed question asked when a target is ambiguous, with no model
    call. Each candidate is named by its friendly name; two with the same
    name are told apart by area, or by entity_id when the area does not
    separate them, so it never asks "Kitchen Light or Kitchen Light?"."""
    from ..presentation import _area_name

    def tally(values):
        counts: dict = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        return counts

    names = [c.get("friendly_name") or c["entity_id"] for c in candidates]
    name_count = tally(names)
    labels = []
    for cand, name in zip(candidates, names):
        if name_count[name] == 1:
            labels.append(name)
            continue
        area = _area_name(hass, cand["entity_id"]) if hass is not None else None
        labels.append(f"{name} ({area or cand['entity_id']})")
    label_count = tally(labels)
    labels = [label if label_count[label] == 1 or cand["entity_id"] in label
              else f"{label} ({cand['entity_id']})"
              for cand, label in zip(candidates, labels)]
    if len(labels) < 2:
        return ("I found more than one possible match — could you be more "
                "specific about which one you mean?")
    return f"I found more than one match — did you mean {labels[0]} or {labels[1]}?"


def _search_candidates(hass: HomeAssistant, domains) -> list:
    """Every entity in the searched domains, read once with its name, state
    and area."""
    from ..entity_resolution import Candidate
    from ..presentation import _area_name
    return [
        Candidate(entity_id=st.entity_id, domain=st.entity_id.split(".", 1)[0],
                  friendly_name=str(st.attributes.get("friendly_name") or ""),
                  state=str(st.state), area=_area_name(hass, st.entity_id) or "")
        for domain in domains for st in hass.states.async_all(domain)
    ]


def _alias_hit(state, entity_id: str, how: str) -> dict:
    return {"entity_id": entity_id,
            "friendly_name": state.attributes.get("friendly_name", ""),
            "state": state.state, "matched_by": how}


async def _exec_search_entities(hass: HomeAssistant, args: dict) -> str:
    """Find entities by id, name, alias, area or domain.

    The order is fixed (agent_runtime.entity_resolution): an exact learned
    alias, then an explicit entity_id, an exact alias or friendly name, an
    exact object name, a domain the words name ("lights", optionally "that
    are on" or with an area or name); partial aliases next, and fuzzy
    scoring last. A ``domain`` argument limits every step.

    With ``require_unique`` the search is picking the one entity to act on:
    several exact matches, a near tie (runner up at 0.85 of the top score or
    more) or no fuzzy match good enough returns ``{"ambiguous": true,
    "candidates": [...]}``, which the agent loop turns into a fixed question
    instead of letting the model guess. Without it the result is a plain
    list. Never calls a service.
    """
    from ..entity_resolution import DEFAULT_DOMAINS, resolve

    query = args.get("query", "").lower().strip()
    domain = args.get("domain")
    unique = bool(args.get("require_unique", False))

    def allowed(eid: str) -> bool:
        return not domain or eid.split(".", 1)[0] == domain

    aliases = _memory._load_learned().get("alias", {})
    # An exact learned alias names one entity, so it is unique by nature.
    target = aliases.get(query) if query in aliases else None
    if target and allowed(target):
        state = hass.states.get(target)
        if state:
            return json.dumps([_alias_hit(state, target, f"learned alias: '{query}'")])

    searched = [domain] if domain else list(DEFAULT_DOMAINS)
    result = resolve(query, _search_candidates(hass, searched), domain=domain,
                     require_unique=unique)
    if result.stage != "fuzzy":
        return json.dumps(result.as_payload())

    # Before fuzzy scoring: every alias that partly matches, one per entity.
    # Different entities here are a real ambiguity when one is required.
    partial: dict = {}
    for alias, eid in sorted(aliases.items()):
        if eid in partial or not allowed(eid):
            continue
        if query in alias or alias in query:
            state = hass.states.get(eid)
            if state:
                partial[eid] = _alias_hit(state, eid, f"partial alias: '{alias}'")
    if not partial:
        return json.dumps(result.as_payload())
    hits = list(partial.values())
    if unique and len(hits) > 1:
        return json.dumps({"ambiguous": True, "candidates": _dedupe_candidates(hits)})
    return json.dumps(hits)


# ── rooms ───────────────────────────────────────────────────────────────────

def _find_area(area_reg, wanted: str):
    """The area a spoken name means: an exact name first, then the first area
    whose name contains it. An empty name means none."""
    wanted = wanted.strip().lower()
    if not wanted:
        return None
    areas = list(area_reg.async_list_areas())
    for area in areas:
        if area.name.lower() == wanted:
            return area
    return next((a for a in areas if wanted in a.name.lower()), None)


def _area_members(hass: HomeAssistant, area_name: str):
    """(area, entities) for a spoken room name, or (None, []) when there is no
    such room. An entity is in the room it is assigned to; only one with no
    room of its own takes its device's room, as in Home Assistant."""
    from homeassistant.helpers import area_registry as areg
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    area = _find_area(areg.async_get(hass), area_name)
    if area is None:
        return None, []
    devices = dr.async_get(hass)
    members = []
    for entry in er.async_get(hass).entities.values():
        room = entry.area_id
        if not room and entry.device_id:
            device = devices.async_get(entry.device_id)
            room = device.area_id if device else None
        state = hass.states.get(entry.entity_id) if room == area.id else None
        if state:
            members.append({"entity_id": entry.entity_id,
                            "friendly_name": state.attributes.get("friendly_name", ""),
                            "state": state.state, "domain": entry.domain})
    return area, members


def area_entity_ids(hass: HomeAssistant, area_name: str, domain: str) -> list[str]:
    """Every entity of ``domain`` in the named room, for room control; [] when
    the room is not found or the registries cannot be read."""
    try:
        _area, members = _area_members(hass, area_name)
    except Exception:
        return []
    return [m["entity_id"] for m in members if m["domain"] == domain]


async def _exec_get_area_devices(hass: HomeAssistant, args: dict) -> str:
    """The entities in a room, the first 30 listed with the full count."""
    wanted = args.get("area_name", "").lower()
    try:
        area, members = _area_members(hass, wanted)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    if area is None:
        return json.dumps({"error": f"Area '{wanted}' not found"})
    return json.dumps({"area": area.name, "device_count": len(members),
                       "devices": members[:_AREA_LIST_LIMIT]})


# ── summary and history ─────────────────────────────────────────────────────

async def _exec_home_summary(hass: HomeAssistant, args: dict) -> str:
    """Who is home, lights on, unlocked locks (exempt ones left out), open
    doors, windows and covers, the thermostats and the weather."""
    from ...cognitive_core import _lockdown_exempt_locks
    states = hass.states
    lights_on = [_name(s) for s in states.async_all("light") if s.state == "on"]
    exempt = _lockdown_exempt_locks()
    opened = [_name(s) for s in states.async_all("binary_sensor")
              if s.attributes.get("device_class", "") in _OPENINGS and s.state == "on"]
    opened += [_name(s) for s in states.async_all("cover") if s.state == "open"]

    summary: dict[str, Any] = {
        "people": [{"name": _name(s), "state": s.state}
                   for s in states.async_all("person")],
        "lights_on": lights_on,
        "lights_on_count": len(lights_on),
        "locks_unlocked": [_name(s) for s in states.async_all("lock")
                           if s.state == "unlocked" and s.entity_id not in exempt],
        "open_doors_windows": opened,
        "climate": [{"name": _name(s), "state": s.state,
                     "current_temp": s.attributes.get("current_temperature"),
                     "target_temp": s.attributes.get("temperature")}
                    for s in states.async_all("climate")],
    }
    weather = next(iter(states.async_all("weather")), None)
    if weather is not None:
        summary["weather"] = {"condition": weather.state,
                              "temperature": weather.attributes.get("temperature"),
                              "humidity": weather.attributes.get("humidity")}
    return json.dumps(summary)


async def _exec_activity_history(hass: HomeAssistant, args: dict) -> str:
    """Home Assistant's recorded history, or its logbook with kind
    "logbook": what has happened."""
    try:
        from ... import activity_history
        kind = str(args.get("kind", "history") or "history").lower()
        entity, hours = args.get("entity"), args.get("hours", 24)
        if kind == "logbook":
            found = await activity_history.logbook(hass, entity=entity, hours=hours)
        else:
            found = await activity_history.entity_history(
                hass, entity=entity, area=args.get("area"), hours=hours)
        return json.dumps(found)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
