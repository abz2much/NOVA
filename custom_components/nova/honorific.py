"""
Nova — presence-aware, per-person honorifics.

Nova's global `honorific` setting ("sir" by default) used to apply to
every spoken line regardless of who was actually home — wrong for a
multi-occupant house, where a broadcast heard by everyone can't correctly
guess whose preference to use, and a direct reply may not even be to the
person who configured "sir" in the first place.

New behavior: when exactly one person is home, Nova uses THAT person's own
configured honorific (falling back to the global default if they haven't
set one). The moment more than one person is home — or nobody is, so
there's no one specific person to address — Nova drops the honorific
entirely rather than guess. Callers get back a plain string, "" meaning
"no honorific" — pair with `persona.lead_in()` to fold that into a
sentence without leaving a dangling ", ." behind.

Config (all via nova_config):
  person_honorifics   dict {person_entity_id: honorific string}, no default
                       entries — an unlisted person falls back to the
                       existing global `honorific` config key.
"""
from __future__ import annotations

from typing import Optional

from homeassistant.core import HomeAssistant

from .const import DEFAULT_HONORIFIC


def all_people(hass: HomeAssistant) -> list[dict]:
    """Every known person.* entity, home or not — {entity_id, name}.

    Presence.py's own summary helper discards entity_id (it only tracks
    display names for conversational context), so this reads
    hass.states.async_all("person") directly — the same primitive that
    powers the summary, just without dropping the one field a per-person
    config mapping actually needs to key on.
    """
    out = []
    for state in hass.states.async_all("person"):
        name = state.attributes.get("friendly_name") or state.entity_id.split(".", 1)[-1]
        out.append({"entity_id": state.entity_id, "name": name})
    return out


def home_alone_entity_id(hass: HomeAssistant) -> Optional[str]:
    """The one person.* entity_id that's home, iff exactly one person is
    home. None when the house is empty or more than one person is home —
    both cases where no single person's preference applies."""
    home_ids = [s.entity_id for s in hass.states.async_all("person") if s.state == "home"]
    return home_ids[0] if len(home_ids) == 1 else None


def effective_honorific(hass: HomeAssistant) -> str:
    """The honorific to use right now, or "" for "don't use one".

    "" whenever it isn't exactly one specific person being addressed
    (nobody home, or several people home) — never guesses. When it IS one
    person, prefers their own configured honorific, falling back to the
    global default so someone who never opened the per-person settings
    still gets today's behavior.
    """
    entity_id = home_alone_entity_id(hass)
    if entity_id is None:
        return ""
    try:
        from . import nova_config
        per_person = nova_config.get("person_honorifics", {}) or {}
        global_default = nova_config.get("honorific", DEFAULT_HONORIFIC) or DEFAULT_HONORIFIC
    except Exception:
        return DEFAULT_HONORIFIC
    return per_person.get(entity_id) or global_default
