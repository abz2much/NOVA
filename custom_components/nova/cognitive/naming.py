"""The one way Nova names an entity in words a person reads or hears.

A friendly name from Home Assistant when there is one; otherwise a readable
form of the entity_id ("light.hall_lamp" becomes "hall lamp"). Callers that
run where Home Assistant's state machine is out of reach (the pattern
analyzer's executor job) are handed a ``names`` map built on the event loop.

Presentation only: entity_ids stay the identity for keys, details, matching
and service calls. Pure; never raises.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

# domain.object_id as Home Assistant writes it. Only ids present in a names
# map are ever replaced, so ordinary text ("e.g.", "1.5") is left alone.
_ENTITY_ID_RE = re.compile(r"\b[a-z_][a-z0-9_]*\.[a-z0-9_]+\b")


def readable_id(entity_id: str) -> str:
    """"light.hall_lamp" -> "hall lamp". Used only when there is no name."""
    text = str(entity_id or "").strip()
    if not text:
        return "a device"
    obj = text.split(".", 1)[1] if "." in text else text
    return " ".join(obj.replace("_", " ").split()) or text


def name_for(entity_id: str, names: Optional[Mapping] = None) -> str:
    """The name to show a person for one entity."""
    try:
        name = names.get(entity_id) if names is not None else None
    except Exception:
        name = None
    name = str(name).strip() if name else ""
    return name or readable_id(entity_id)


def names_from_states(states: Iterable) -> dict:
    """entity_id -> friendly name for state objects that carry one."""
    out: dict = {}
    for st in states or ():
        try:
            name = (st.attributes or {}).get("friendly_name")
            if name and str(name).strip():
                out[str(st.entity_id)] = str(name).strip()
        except Exception:
            continue
    return out


def with_article(name: str) -> str:
    """"Kitchen Light" -> "the Kitchen Light"; a name that already starts
    with "the" or is possessive ("Abi's Lamp") is left alone."""
    text = str(name or "").strip()
    low = text.casefold()
    if not text or low.startswith("the ") or "'s " in low or low.endswith("'s"):
        return text
    return f"the {text}"


def humanize_text(text: Any, names: Optional[Mapping] = None) -> Any:
    """Replace every known entity_id in `text` with its friendly name.

    Unknown ids and anything that only looks like one stay as they are.
    Non-strings are returned unchanged. For words shown to a person when the
    stored text was written with entity_ids (older rows, log lines)."""
    if not isinstance(text, str) or not text or not names:
        return text

    def _swap(match: "re.Match[str]") -> str:
        token = match.group(0)
        try:
            name = names.get(token)
        except Exception:
            name = None
        name = str(name).strip() if name else ""
        if not name:
            return token
        # Already written as "Name (entity_id)": keep the id beside its name.
        start, end = match.start(), match.end()
        if (text[end:end + 1] == ")"
                and text[:start].endswith(f"{name} (")):
            return token
        return name

    return _ENTITY_ID_RE.sub(_swap, text)


def label_with_id(entity_id: str, names: Optional[Mapping] = None) -> str:
    """"Kitchen Light (light.kitchen)" for records where the id must stay
    visible next to the name; the bare id when there is no name."""
    try:
        name = names.get(entity_id) if names is not None else None
    except Exception:
        name = None
    name = str(name).strip() if name else ""
    return f"{name} ({entity_id})" if name and name != entity_id else str(entity_id)


def humanize_record(value: Any, names: Optional[Mapping] = None) -> Any:
    """A display copy of a stored record (dicts, lists, strings): a string
    that is exactly a known entity_id becomes "Name (entity_id)", and known
    entity_ids inside longer text become names. The original is not
    modified."""
    if not names:
        return value
    if isinstance(value, str):
        if value in names:
            return label_with_id(value, names)
        return humanize_text(value, names)
    if isinstance(value, dict):
        return {k: humanize_record(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [humanize_record(v, names) for v in value]
    if isinstance(value, tuple):
        return tuple(humanize_record(v, names) for v in value)
    return value
