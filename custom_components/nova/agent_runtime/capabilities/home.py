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


def _build_clarification(candidates: list[dict]) -> str:
    """Fixed, deterministic clarification question — no LLM call. If two or
    more candidates share the same friendly_name, the entity_id is appended
    to disambiguate (never "did you mean Kitchen Light or Kitchen Light?")."""
    names = [c.get("friendly_name") or c["entity_id"] for c in candidates]
    counts: dict = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    labels = [f"{n} ({c['entity_id']})" if counts[n] > 1 else n
              for c, n in zip(candidates, names)]
    if len(labels) >= 2:
        return f"I found more than one match — did you mean {labels[0]} or {labels[1]}?"
    return "I found more than one possible match — could you be more specific about which one you mean?"


async def _exec_search_entities(hass: HomeAssistant, args: dict) -> str:
    """Search for entities by name, area, or domain with fuzzy matching.

    `require_unique` (Phase 3): when true, this search is resolving exactly
    ONE target entity before an action. If two or more plausible candidates
    remain after scoring (ratio of runner-up/top score >= 0.85, empirically
    derived — see Phase 3 design notes), returns an `{"ambiguous": true,
    "candidates": [...]}` marker instead of a plain list, which run_agent's
    tool-dispatch loop intercepts to stop the turn with a fixed clarification
    question rather than letting the model guess. Default false preserves
    ordinary multi-result discovery exactly as before — no marker, no forced
    clarification, ever.
    """
    query = args.get("query", "").lower().strip()
    domain_filter = args.get("domain")
    require_unique = bool(args.get("require_unique", False))

    # Check learned aliases first — an exact key maps to exactly one entity,
    # so this is always inherently unique regardless of require_unique.
    learned = _memory._load_learned()
    aliases = learned.get("alias", {})
    if query in aliases:
        resolved_id = aliases[query]
        state = hass.states.get(resolved_id)
        if state:
            return json.dumps([{
                "entity_id": resolved_id,
                "friendly_name": state.attributes.get("friendly_name", ""),
                "state": state.state,
                "matched_by": f"learned alias: '{query}'",
            }])

    # Partial alias matches — collect ALL matches (deduped by entity_id),
    # not just the first. Multiple *different* entities matching partially
    # is genuine ambiguity when require_unique is set; require_unique=false
    # now returns every plausible partial-alias match instead of silently
    # hiding all but the first (closest honest match to "discovery" intent —
    # the old single-item return was an accident of early-return, not a
    # deliberate one-result contract).
    alias_matches: dict = {}
    for alias_name, alias_id in aliases.items():
        if query in alias_name or alias_name in query:
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

    domains = [domain_filter] if domain_filter else [
        "light", "switch", "lock", "cover", "climate", "fan",
        "media_player", "sensor", "binary_sensor", "scene",
        "script", "automation", "person",
    ]

    # Fuzzy bigram scorer (inline — no external deps)
    def _bigrams(s):
        return set(s[i:i+2] for i in range(len(s)-1)) if len(s) > 1 else {s}

    def _fuzzy(a, b):
        if a == b: return 100.0
        if not a or not b: return 0.0
        bg_a, bg_b = _bigrams(a), _bigrams(b)
        overlap = len(bg_a & bg_b)
        dice = (2.0 * overlap) / (len(bg_a) + len(bg_b)) * 100 if bg_a and bg_b else 0
        contain = len(a) / len(b) * 80 if a in b else (len(b) / len(a) * 80 if b in a else 0)
        return max(dice, contain)

    results = []
    query_words = set(query.split())

    for domain in domains:
        for state in hass.states.async_all(domain):
            fname = (state.attributes.get("friendly_name") or "").lower()
            eid = state.entity_id.lower()
            score = 0

            if query == fname:
                score = 100
            elif query in fname:
                score = 80
            elif query.replace(" ", "_") in eid:
                score = 70
            elif query_words and query_words.issubset(set(fname.split())):
                score = 65
            else:
                # Fuzzy matching
                fuzz = _fuzzy(query, fname)
                if fuzz > 45:
                    score = fuzz * 0.7  # Scale down fuzzy scores

                # Word-level fuzzy — check each query word
                if not score and query_words:
                    fname_words = set(fname.split())
                    word_matches = 0
                    for qw in query_words:
                        for fw in fname_words:
                            if _fuzzy(qw, fw) > 60:
                                word_matches += 1
                                break
                    if word_matches > 0:
                        score = (word_matches / len(query_words)) * 50

            if score > 25:
                results.append({
                    "entity_id": state.entity_id,
                    "friendly_name": state.attributes.get("friendly_name", ""),
                    "state": state.state,
                    "score": round(score, 1),
                })

    results.sort(key=lambda r: r["score"], reverse=True)
    results = results[:15]

    if require_unique and len(results) >= 2 and results[0]["score"] > 0:
        ratio = results[1]["score"] / results[0]["score"]
        if ratio >= 0.85:
            near_tie = [r for r in results if r["score"] / results[0]["score"] >= 0.85][:4]
            return json.dumps({
                "ambiguous": True,
                "candidates": _dedupe_candidates(near_tie),
            })

    return json.dumps(results)


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
