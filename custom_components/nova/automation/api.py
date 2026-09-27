"""Payload translation for the automation capability's public surfaces.

Pure functions that turn package results into the exact dictionaries the
panel already receives. Registration, admin checks, response literals and
error codes stay in websocket.py, agent.py and services.py (the public
contract tests read them there); this module only shapes stored rows, so the
shapes can be tested without Home Assistant.
"""
from __future__ import annotations

import json
from typing import Iterable, Mapping, Optional

from .models import loads_json
from .suggestions import explain_suggestion


def _display_yaml(stored: str, names: Optional[Mapping]) -> str:
    """The stored automation payload with its alias and note named for a
    person. Triggers, conditions and actions keep their entity_ids, and a
    payload that isn't a JSON object is shown exactly as stored."""
    if not names or not stored:
        return stored
    from ..cognitive.naming import humanize_text
    try:
        data = json.loads(stored)
    except Exception:
        return stored
    if not isinstance(data, dict):
        return stored
    changed = False
    for key in ("alias", "note"):
        if isinstance(data.get(key), str):
            new = humanize_text(data[key], names)
            if new != data[key]:
                data[key] = new
                changed = True
    return json.dumps(data, indent=2) if changed else stored


def panel_suggestion_items(rows: Iterable[dict],
                           names: Optional[Mapping] = None) -> list[dict]:
    """Pending suggestion rows as ``get_panel_data.suggestions`` items,
    including the evidence behind each one. With `names` (entity_id ->
    friendly name), text stored with entity_ids by older releases is shown
    with names; the stored rows are not changed."""
    from ..cognitive.naming import humanize_text, name_for
    out = []
    for s in rows:
        ptype = s.get("pattern_type", "") or ""
        details = loads_json(s.get("details") or "{}", {})
        entities = loads_json(s.get("entity_ids") or "[]", [])
        count = s.get("pattern_count", 0) or 0
        why = explain_suggestion(ptype, details, count)
        # Names learned with the suggestion first, then the live ones.
        learned = details.get("names") if isinstance(details, dict) else None
        shown = {**(learned if isinstance(learned, dict) else {}), **(names or {})}
        out.append({
            "id": s.get("id"),
            "created": s.get("created", ""),
            "description": humanize_text(s.get("description", ""), shown),
            "yaml": _display_yaml(s.get("automation_yaml", ""), shown),
            "confidence": round(float(s.get("confidence", 0) or 0), 2),
            "count": count,
            "pattern_type": ptype,
            "entities": entities,
            "entity_labels": [name_for(str(e), shown) for e in entities]
            if isinstance(entities, list) else [],
            "why_headline": humanize_text(why.get("headline", ""), shown),
            "evidence": [humanize_text(e, shown) for e in why.get("evidence", [])],
            "automation_match": (details.get("automation_match") or {})
            if isinstance(details, dict) else {},
        })
    return out


def panel_rejected_items(rows: Iterable[dict],
                         names: Optional[Mapping] = None) -> list[dict]:
    """Suggestions the AI review turned down, as
    ``get_panel_data.suggestions_filtered`` items: what it was and why it
    was rejected."""
    from ..cognitive.naming import humanize_text
    out = []
    for s in rows:
        details = loads_json(s.get("details") or "{}", {})
        details = details if isinstance(details, dict) else {}
        learned = details.get("names")
        shown = {**(learned if isinstance(learned, dict) else {}), **(names or {})}
        raw_review = details.get("review")
        review: dict = raw_review if isinstance(raw_review, dict) else {}
        out.append({
            "id": s.get("id"),
            "pattern_type": s.get("pattern_type", "") or "",
            "description": humanize_text(s.get("description", ""), shown),
            "reason": str(review.get("reason") or ""),
            "model": str(review.get("model") or ""),
            "reviewed_at": str(review.get("ts") or ""),
        })
    return out
