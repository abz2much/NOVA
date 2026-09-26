"""Home state and entity search (read only)."""
from __future__ import annotations

import json
import logging

from homeassistant.core import HomeAssistant

from . import memory as _memory

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


async def _exec_get_entity_state(hass: HomeAssistant, args: dict) -> str:
    """Get state of one or more entities."""
    entity_ids = args.get("entity_ids", [])
    results = []
    for eid in entity_ids[:20]:  # Cap at 20
        state = hass.states.get(eid)
        if state:
            attrs = dict(state.attributes)
            # Filter to useful attributes
            useful = {}
            for key in ("friendly_name", "brightness", "temperature",
                        "current_temperature", "humidity", "unit_of_measurement",
                        "device_class", "battery_level", "media_title",
                        "volume_level", "source"):
                if key in attrs:
                    useful[key] = attrs[key]
            results.append({
                "entity_id": eid,
                "state": state.state,
                "attributes": useful,
                # When the state last changed / was last written — lets Nova
                # answer "when did this turn on?" by reading history instead of
                # (wrongly) acting on a question about the past.
                "last_changed": state.last_changed.isoformat() if state.last_changed else None,
                "last_updated": state.last_updated.isoformat() if state.last_updated else None,
            })
        else:
            results.append({"entity_id": eid, "error": "not found"})
    return json.dumps(results)


def _dedupe_candidates(items: list[dict]) -> list[dict]:
    """Dedupe a candidate list by entity_id, first occurrence wins. Used
    everywhere an ambiguity candidate list is built, so the same entity can
    never appear twice in a clarification."""
    seen: dict = {}
    for it in items:
        seen.setdefault(it["entity_id"], it)
    return list(seen.values())


def _build_clarification(candidates: list[dict], hass=None) -> str:
    """Fixed, deterministic clarification question — no LLM call. Candidates
    are named by friendly name; when two share one, the area tells them
    apart, and the entity_id only when there is no area or it is the same
    (never "did you mean Kitchen Light or Kitchen Light?")."""
    from ..presentation import _area_name
    names = [c.get("friendly_name") or c["entity_id"] for c in candidates]
    counts: dict = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    labels = []
    for c, n in zip(candidates, names):
        if counts[n] > 1:
            area = _area_name(hass, c["entity_id"]) if hass is not None else None
            labels.append(f"{n} ({area})" if area else f"{n} ({c['entity_id']})")
        else:
            labels.append(n)
    seen: dict = {}
    for label in labels:
        seen[label] = seen.get(label, 0) + 1
    labels = [f"{label} ({c['entity_id']})" if seen[label] > 1 and c["entity_id"] not in label
              else label for c, label in zip(candidates, labels)]
    if len(labels) >= 2:
        return f"I found more than one match — did you mean {labels[0]} or {labels[1]}?"
    return "I found more than one possible match — could you be more specific about which one you mean?"


def _search_candidates(hass: HomeAssistant, domains) -> list:
    """The entities a search may match, read once: id, domain, friendly name,
    state and area. Bounded by the domains searched."""
    from ..entity_resolution import Candidate
    from ..presentation import _area_name
    out = []
    for domain in domains:
        for state in hass.states.async_all(domain):
            out.append(Candidate(
                entity_id=state.entity_id, domain=state.entity_id.split(".", 1)[0],
                friendly_name=str(state.attributes.get("friendly_name") or ""),
                state=str(state.state), area=_area_name(hass, state.entity_id) or ""))
    return out


async def _exec_search_entities(hass: HomeAssistant, args: dict) -> str:
    """Search for entities by entity_id, name, alias, area or domain.

    Resolution is deterministic first (agent_runtime.entity_resolution): an
    explicit entity_id, an exact learned alias or friendly name, an exact
    object name, then a domain the request names ("lights", "locks"),
    optionally with a state ("lights that are on") or an area or name; only
    then bounded fuzzy scoring. An explicit `domain` restricts every stage.

    `require_unique`: when true, this search is resolving exactly ONE target
    entity before an action. Several exact matches, a near tie
    (runner-up/top score >= 0.85) or no fuzzy candidate strong enough to act
    on returns an `{"ambiguous": true, "candidates": [...]}` marker instead
    of a plain list, which run_agent's tool-dispatch loop intercepts to stop
    the turn with a fixed clarification question rather than letting the
    model guess. Default false is discovery: a plain list, never a forced
    clarification. Read only: never calls a service.
    """
    from ..entity_resolution import DEFAULT_DOMAINS, resolve

    query = args.get("query", "").lower().strip()
    domain_filter = args.get("domain")
    require_unique = bool(args.get("require_unique", False))

    def _in_domain(eid: str) -> bool:
        return not domain_filter or eid.split(".", 1)[0] == domain_filter

    # A learned alias names exactly one entity, so it is inherently unique.
    learned = _memory._load_learned()
    aliases = learned.get("alias", {})
    if query in aliases and _in_domain(aliases[query]):
        resolved_id = aliases[query]
        state = hass.states.get(resolved_id)
        if state:
            return json.dumps([{
                "entity_id": resolved_id,
                "friendly_name": state.attributes.get("friendly_name", ""),
                "state": state.state,
                "matched_by": f"learned alias: '{query}'",
            }])

    domains = [domain_filter] if domain_filter else list(DEFAULT_DOMAINS)
    result = resolve(query, _search_candidates(hass, domains), domain=domain_filter,
                     require_unique=require_unique)
    if result.stage != "fuzzy":
        return json.dumps(result.as_payload())

    # Partial alias matches — ALL of them (deduped by entity_id), before
    # fuzzy name scoring. Several different entities matching partially is
    # genuine ambiguity when require_unique is set.
    alias_matches: dict = {}
    for alias_name, alias_id in sorted(aliases.items()):
        if (query in alias_name or alias_name in query) and _in_domain(alias_id):
            state = hass.states.get(alias_id)
            if state and alias_id not in alias_matches:
                alias_matches[alias_id] = {
                    "entity_id": alias_id,
                    "friendly_name": state.attributes.get("friendly_name", ""),
                    "state": state.state,
                    "matched_by": f"partial alias: '{alias_name}'",
                }
    if alias_matches:
        alias_results = list(alias_matches.values())
        if require_unique and len(alias_results) > 1:
            return json.dumps({
                "ambiguous": True,
                "candidates": _dedupe_candidates(alias_results),
            })
        return json.dumps(alias_results)

    return json.dumps(result.as_payload())


async def _exec_get_area_devices(hass: HomeAssistant, args: dict) -> str:
    """List all devices in an area."""
    area_name = args.get("area_name", "").lower()
    try:
        from homeassistant.helpers import (
            area_registry as areg, entity_registry as er, device_registry as dr,
        )
        area_reg = areg.async_get(hass)
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)

        target = None
        for area in area_reg.async_list_areas():
            if area_name in area.name.lower():
                target = area
                break
        if not target:
            return json.dumps({"error": f"Area '{area_name}' not found"})

        devices = []
        for entry in ent_reg.entities.values():
            in_area = entry.area_id == target.id
            if not in_area and entry.device_id:
                device = dev_reg.async_get(entry.device_id)
                in_area = device and device.area_id == target.id
            if in_area:
                state = hass.states.get(entry.entity_id)
                if state:
                    devices.append({
                        "entity_id": entry.entity_id,
                        "friendly_name": state.attributes.get("friendly_name", ""),
                        "state": state.state,
                        "domain": entry.domain,
                    })

        return json.dumps({
            "area": target.name,
            "device_count": len(devices),
            "devices": devices[:30],
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_home_summary(hass: HomeAssistant, args: dict) -> str:
    """Build a comprehensive home summary."""
    summary = {}

    # People
    people = []
    for s in hass.states.async_all("person"):
        people.append({
            "name": s.attributes.get("friendly_name", s.entity_id),
            "state": s.state,
        })
    summary["people"] = people

    # Lights
    on_lights = [
        s.attributes.get("friendly_name", s.entity_id)
        for s in hass.states.async_all("light") if s.state == "on"
    ]
    summary["lights_on"] = on_lights
    summary["lights_on_count"] = len(on_lights)

    # Locks
    from ...cognitive_core import _lockdown_exempt_locks
    exempt = _lockdown_exempt_locks()
    unlocked = [
        s.attributes.get("friendly_name", s.entity_id)
        for s in hass.states.async_all("lock")
        if s.state == "unlocked" and s.entity_id not in exempt
    ]
    summary["locks_unlocked"] = unlocked

    # Doors/Windows
    open_items = []
    for s in hass.states.async_all("binary_sensor"):
        dc = s.attributes.get("device_class", "")
        if dc in ("door", "window", "garage_door") and s.state == "on":
            open_items.append(s.attributes.get("friendly_name", s.entity_id))
    for s in hass.states.async_all("cover"):
        if s.state == "open":
            open_items.append(s.attributes.get("friendly_name", s.entity_id))
    summary["open_doors_windows"] = open_items

    # Climate
    climate = []
    for s in hass.states.async_all("climate"):
        climate.append({
            "name": s.attributes.get("friendly_name", s.entity_id),
            "state": s.state,
            "current_temp": s.attributes.get("current_temperature"),
            "target_temp": s.attributes.get("temperature"),
        })
    summary["climate"] = climate

    # Weather
    for s in hass.states.async_all("weather"):
        summary["weather"] = {
            "condition": s.state,
            "temperature": s.attributes.get("temperature"),
            "humidity": s.attributes.get("humidity"),
        }
        break

    return json.dumps(summary)


async def _exec_activity_history(hass: HomeAssistant, args: dict) -> str:
    """Read HA's recorded history or logbook — 'what has happened' (v6.72.0)."""
    try:
        from ... import activity_history
        kind = str(args.get("kind", "history") or "history").lower()
        entity = args.get("entity")
        area = args.get("area")
        hours = args.get("hours", 24)
        if kind == "logbook":
            res = await activity_history.logbook(hass, entity=entity, hours=hours)
        else:
            res = await activity_history.entity_history(
                hass, entity=entity, area=area, hours=hours)
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
