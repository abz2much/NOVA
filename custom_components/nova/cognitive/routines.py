"""Per-person routines: which learned habits are a person's, how each is
identified, and how Nova says one.

A person's routine is something they do to the home: turning a light on,
opening the blinds, locking the door. Trackers, sensors and helpers change
state too, but those are measurements or other automations, not a habit to
remind anyone of. Only the domains and states below count.

A routine's identity is what it is (person, kind, entity, state, hour), never
its measured counts, so the same habit measured again is the same routine.
The same key limits an alert to once a day.

Pure: no Home Assistant, storage or speech. Never raises.
"""
from __future__ import annotations

from typing import Mapping, Optional

from .naming import name_for, with_article

# domain -> states that count (None: any real state).
ROUTINE_DOMAINS: dict[str, Optional[frozenset]] = {
    "light": frozenset({"on", "off"}),
    "switch": frozenset({"on", "off"}),
    "fan": frozenset({"on", "off"}),
    "cover": frozenset({"open", "closed"}),
    "lock": frozenset({"locked", "unlocked"}),
    "climate": None,
    "media_player": frozenset({"playing"}),
}
_NOT_STATES = frozenset({"", "unknown", "unavailable", "none"})

TIME_ROUTINE = "time_routine"
REPEATED_COMMAND = "repeated_command"


def is_person_routine(entity_id: str, state: str) -> bool:
    """True when changing `entity_id` to `state` is something a person does."""
    entity_id = str(entity_id or "")
    state = str(state or "").strip().lower()
    if "." not in entity_id or state in _NOT_STATES:
        return False
    domain = entity_id.split(".", 1)[0]
    if domain not in ROUTINE_DOMAINS:
        return False
    allowed = ROUTINE_DOMAINS[domain]
    return allowed is None or state in allowed


def routine_key(pattern_type: str, entity_id: str = "", details: Optional[Mapping] = None
                ) -> Optional[str]:
    """Stable identity of a stored routine within one person and kind.

    time_routine: "<entity_id>|<state>|<hour>"; repeated_command:
    "cmd|<command>|<hour>". None when the pattern carries neither. The
    person_patterns upgrade (persistence/schema.py) builds the same strings
    for rows stored before this key existed."""
    d = details if isinstance(details, Mapping) else {}
    try:
        if pattern_type == TIME_ROUTINE:
            if not entity_id or d.get("state") is None or d.get("hour") is None:
                return None
            return f"{entity_id}|{d['state']}|{int(d['hour'])}"
        if pattern_type == REPEATED_COMMAND:
            if not d.get("command") or d.get("hour") is None:
                return None
            return f"cmd|{d['command']}|{int(d['hour'])}"
    except (TypeError, ValueError):
        return None
    return None


def routine_action(entity_id: str, state: str, names: Optional[Mapping] = None) -> str:
    """What the person does, in words: "turn the Kitchen Light on"."""
    domain = str(entity_id or "").split(".", 1)[0]
    state = str(state or "").strip().lower()
    thing = with_article(name_for(entity_id, names))
    if domain in ("light", "switch", "fan"):
        return f"turn {thing} {state}"
    if domain == "cover":
        return f"{'open' if state == 'open' else 'close'} {thing}"
    if domain == "lock":
        return f"{'lock' if state == 'locked' else 'unlock'} {thing}"
    if domain == "climate":
        if state == "off":
            return f"turn {thing} off"
        return f"set {thing} to {state.replace('_', ' ')}"
    if domain == "media_player":
        return f"start playing on {thing}"
    return f"set {thing} to {state.replace('_', ' ')}"


def routine_sentence(entity_id: str, state: str, names: Optional[Mapping] = None,
                     person_name: str = "") -> str:
    """"You usually turn the Kitchen Light on around now." Addressed to the
    person; `person_name` is added in front when others may be listening."""
    action = routine_action(entity_id, state, names)
    who = str(person_name or "").strip()
    if who:
        return f"{who}, you usually {action} around now."
    return f"You usually {action} around now."
