"""The words a person hears for a cognitive decision.

Evaluators describe what to say as a Phrase; this module turns it into text
through Nova's own voice (persona and the Local Mind's composer), exactly as
the templates always have. It decides nothing: speak or silent, urgency and
reason are fixed before a phrase is rendered. Persona variety may choose a
different opener each time, which is why rendering lives here and not in the
pure evaluators. Never raises.

Human-facing names use the friendly name the snapshot carries, falling back
to the entity_id only when there is no name. Entity ids stay the identity
everywhere else; nothing rendered here is used for matching, policy, storage
or service calls.
"""
from __future__ import annotations

from typing import Optional

from .models import Decision, Phrase


def _persona():
    from .. import persona
    return persona


def _local_mind():
    from .. import local_mind
    return local_mind


def _lm_compose(honorific, name, *, device_class="", to_state="", away=False,
                escalated=False) -> str:
    """Template speech through the Local Mind's voice."""
    try:
        return _local_mind().compose_announcement(
            honorific, name, "", to_state, device_class, away=away, escalated=escalated)
    except Exception:
        return _persona().lead_in(honorific, f"{name or 'a device'} requires attention.")


def render(phrase: Optional[Phrase], honorific: str) -> str:
    if phrase is None:
        return ""
    kind = phrase.kind
    if kind == "lead_in":
        return _persona().lead_in(honorific, phrase.get("sentence", ""))
    if kind == "arrival":
        if honorific:
            return f"Welcome home, {honorific.strip().lower()}."
        return f"{phrase.get('name', 'Someone')} has arrived home."
    if kind == "lm_compose":
        return _lm_compose(honorific, phrase.get("name", ""),
                           device_class=phrase.get("device_class", ""),
                           to_state=phrase.get("to_state", ""),
                           away=phrase.get("away", False),
                           escalated=phrase.get("escalated", False))
    if kind == "lm_full":
        return _local_mind()._compose(
            honorific, phrase.get("name", ""), phrase.get("entity_id", ""),
            phrase.get("to_state", ""), int(phrase.get("hour", 0)),
            novelty=phrase.get("novelty", "unknown"), away=phrase.get("away", False),
            escalated=phrase.get("escalated", False),
            device_class=phrase.get("device_class", ""))
    if kind == "lm_cached":
        return _local_mind().compose_announcement(
            honorific, phrase.get("name", ""), phrase.get("entity_id", ""),
            phrase.get("to_state", ""), phrase.get("device_class", ""),
            hour=phrase.get("hour"), novelty=phrase.get("novelty", "unknown"),
            away=phrase.get("away", False), escalated=phrase.get("escalated", False),
            urgency=phrase.get("urgency", ""))
    if kind == "fallback":
        try:
            return _local_mind().compose_announcement(
                honorific, phrase.get("name", ""), "", phrase.get("to_state", ""),
                escalated=True)
        except Exception:
            name = phrase.get("name") or "A device"
            return _persona().lead_in(honorific, f"attention required: {name}.")
    return ""


def voiced(decision: Decision, honorific: str) -> Decision:
    """The decision with its message rendered (speech only)."""
    if not decision.speak or decision.message:
        return decision
    return decision.with_message(render(decision.phrase, honorific))


def entity_label(friendly_name: str, entity_id: str, area: str = "",
                 collides: bool = False) -> str:
    """How to name one entity to a person: its friendly name, with the area
    when another entity shares that name, and the entity_id only when there
    is no usable name (or no area to tell a collision apart)."""
    name = (friendly_name or "").strip()
    if not name:
        return entity_id or "a device"
    if collides:
        return f"{name} ({area})" if area else f"{name} ({entity_id})"
    return name
