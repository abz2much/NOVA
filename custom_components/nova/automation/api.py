"""Payload translation for the automation capability's public surfaces.

Pure functions that turn package results into the exact dictionaries the
panel already receives. Registration, admin checks, response literals and
error codes stay in websocket.py, agent.py and services.py (the public
contract tests read them there); this module only shapes stored rows, so the
shapes can be tested without Home Assistant.
"""
from __future__ import annotations

from typing import Iterable

from .models import loads_json
from .suggestions import explain_suggestion


def panel_suggestion_items(rows: Iterable[dict]) -> list[dict]:
    """Pending suggestion rows as ``get_panel_data.suggestions`` items,
    including the evidence behind each one."""
    out = []
    for s in rows:
        ptype = s.get("pattern_type", "") or ""
        details = loads_json(s.get("details") or "{}", {})
        entities = loads_json(s.get("entity_ids") or "[]", [])
        count = s.get("pattern_count", 0) or 0
        why = explain_suggestion(ptype, details, count)
        out.append({
            "id": s.get("id"),
            "created": s.get("created", ""),
            "description": s.get("description", ""),
            "yaml": s.get("automation_yaml", ""),
            "confidence": round(float(s.get("confidence", 0) or 0), 2),
            "count": count,
            "pattern_type": ptype,
            "entities": entities,
            "why_headline": why.get("headline", ""),
            "evidence": why.get("evidence", []),
            "automation_match": details.get("automation_match") or {},
        })
    return out
