"""The one way Nova names an entity in words a person reads or hears.

A friendly name from Home Assistant when there is one; otherwise a readable
form of the entity_id ("light.hall_lamp" becomes "hall lamp"). Callers that
run where Home Assistant's state machine is out of reach (the pattern
analyzer's executor job) are handed a ``names`` map built on the event loop.

Presentation only: entity_ids stay the identity for keys, details, matching
and service calls. Pure; never raises.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional


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
