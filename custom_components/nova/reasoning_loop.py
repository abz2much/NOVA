"""The observer's second tier: should Nova speak about a flagged event?

The classifier flags an event; this decides, with the full picture, whether
it is worth saying and how. Local rules answer nearly every event; a learned
cache of past model decisions comes next, and the model only for what is left.
It returns {"speak": true, "message": ..., "urgency": ...} or
{"speak": false, "reason": ...}, and the observer hands a "speak" to the
announcement gate.

Since Phase 8 the work is done by cognitive.coordinator over the pure rules in
cognitive.evaluators. This module keeps the names other code and tests call
or patch (decide, _try_local_reasoning, _structured_hazard, _rich_mode,
REASONING_SYSTEM_APPENDIX and the rest) with the same signatures.
"""
from __future__ import annotations

import logging
from typing import Optional

from .cognitive import coordinator as _coordinator
from .cognitive import evaluators as _evaluators
from .cognitive import presentation as _presentation
from .cognitive.models import EventSnapshot
from .directive_helper import build_system_prompt

_LOGGER = logging.getLogger(__name__)

# Older names for the evaluators' own tables and helpers.
_ACTIVE_TRIGGER_STATES = set(_evaluators.ACTIVE_TRIGGER_STATES)
_CRITICAL_HAZARD_CLASSES = _evaluators.CRITICAL_HAZARD_CLASSES
_HAZARD_WORDS = dict(_evaluators.HAZARD_WORDS)
_summary_new_state = _evaluators.summary_new_state
_parse_summary = _evaluators.parse_summary

_TRUE_WORDS = ("1", "true", "yes", "on")


def _snapshot(event_summary="", urgency="", category="", recent_announcements=(),
              anyone_home=False, from_state="", to_state="", entity_id="",
              friendly_name="", device_class="") -> EventSnapshot:
    fields = dict(locals())
    return EventSnapshot.build(**fields)


def _structured_hazard(device_class: str, to_state: str, event_summary: str,
                       entity_id: str, friendly_name: str,
                       honorific: str) -> Optional[dict]:
    """The decision for a critical hazard sensor, read from its device class
    and state, never from words in its name. None for any other class, or
    with no new state to judge."""
    snap = _snapshot(device_class=device_class, to_state=to_state,
                     event_summary=event_summary, entity_id=entity_id,
                     friendly_name=friendly_name)
    found = _evaluators.evaluate_structured_hazard(snap)
    if found is None:
        return None
    return _presentation.voiced(found, honorific).as_dict()


def _lm_compose(honorific, friendly_name, *, device_class="", to_state="",
                away=False, escalated=False) -> str:
    """A template sentence in the Local Mind's voice. Never raises."""
    return _presentation._lm_compose(honorific, friendly_name, device_class=device_class,
                                     to_state=to_state, away=away, escalated=escalated)


def _try_local_reasoning(event_summary: str, urgency: str, category: str,
                         honorific: str, recent_announcements: list[str],
                         anyone_home: bool = False, from_state: str = "",
                         to_state: str = "", entity_id: str = "", friendly_name: str = "",
                         device_class: str = "",
                         trace: Optional[dict] = None) -> Optional[dict]:
    """The local rules (cognitive.evaluators.LOCAL_RULES) in their priority
    order, or None to go on to the cache and the model.

    A critical hazard sensor is judged first, by device class and then by
    its name, before the recent announcement check and the someone is home
    shortcut, so an active smoke, gas, leak or CO alarm is always spoken
    whatever the sensor is called and whoever is home. The rule that decided
    is written into trace."""
    snap = _snapshot(event_summary=event_summary, urgency=urgency, category=category,
                     recent_announcements=recent_announcements, anyone_home=anyone_home,
                     from_state=from_state, to_state=to_state, entity_id=entity_id,
                     friendly_name=friendly_name, device_class=device_class)
    chosen = _coordinator.local_decision(snap, honorific)
    if chosen is None:
        return None
    if trace is not None:
        trace["origin"] = chosen.origin
        trace["rule"] = chosen.reason_code
    return chosen.as_dict()


# The observer's instructions to the model, added to the system prompt. Kept
# word for word: the model's decisions depend on it.
REASONING_SYSTEM_APPENDIX = "\n".join((
    "",
    "You are currently operating in OBSERVER MODE — not responding to a "
    "direct request, but deciding whether to proactively speak to the user "
    "about something happening in the house.",
    "",
    "You receive a flagged event and must decide: is this worth announcing "
    "RIGHT NOW? If yes, how would you phrase it in character?",
    "",
    "STRONG BIAS TOWARD SILENCE. The user did not ask. Interrupting is "
    "costly. Only speak if a reasonable butler in your position would feel "
    "it's their duty to mention this. When in doubt, stay silent.",
    "",
    "Consider:",
    "- Is this something the user would want to know NOW, or can it wait?",
    "- Is this something they'd consider useful or annoying?",
    "- Has this been announced recently? (If yes, stay silent unless truly "
    "changed.)",
    "- Is the user likely busy / not listening right now?",
    "- If `presence_context` says the user is sleeping, the bar for "
    "speaking is   MUCH higher. Only speak if it's a real emergency.",
    "",
    "URGENCY DEFINITIONS (be strict — misclassifying spams the user):",
    "",
    "- \"critical\" — immediate physical danger requiring the user to wake up "
    "/ act NOW.",
    "  Examples: smoke alarm, CO alarm, gas leak, water leak, glass break "
    "while   armed, active intrusion. NOTHING ELSE IS CRITICAL. A door "
    "opening is NOT   critical. A light turning on is NOT critical. Motion "
    "detected is NOT critical.",
    "",
    "- \"high\" — important, user would want to know within the hour but can "
    "handle   asynchronously. Examples: package delivery, doorbell while "
    "away, mail arrival,   unexpected window open while away.",
    "",
    "- \"medium\" — useful to mention if the user is present and awake. "
    "Examples:   laundry cycle complete, back door left open more than 15 "
    "minutes, family   member arrived home.",
    "",
    "- \"low\" — minor ambient observation. Examples: lights left on in "
    "unoccupied   room, indoor temperature drift, routine sensor state "
    "change.",
    "",
    "DEFAULT TO \"low\" OR \"medium\" UNLESS YOU HAVE STRONG EVIDENCE "
    "OTHERWISE.",
    "",
    "HOW Nova REASONS (when you do speak):",
    "- Don't merely report the event — convey what it means. \"The back door "
    "has been open twenty minutes\" is better as \"The back door has been "
    "open twenty minutes, {honorific} — worth a glance before dark.\" You "
    "connect the fact to its implication.",
    "- Anticipate. If a window is open and the temperature is dropping, the "
    "useful observation is the combination, not either fact alone.",
    "- Lead with the thing that matters. One clause of substance beats two "
    "of padding.",
    "- Stay understated even when flagging something real. Nova does not "
    "alarm; he informs, calmly, and trusts the user to act.",
    "",
    "Your response must be JSON only:",
    "  {\"speak\": false, \"reason\": \"why staying silent\"}",
    "  OR",
    "  {\"speak\": true, \"message\": \"what to say\", \"urgency\": "
    "\"low|medium|high|critical\"}",
    "",
    "Keep messages short. One or two sentences. In character. No preamble. "
    "No exclamation marks unless it is a genuine emergency.",
    "",
))


def _rich_mode(hass) -> bool:
    """The panel's Rich Reasoning switch, read live. Off unless set."""
    from .runtime import domain_runtime_config
    value = domain_runtime_config(hass).get("rich_reasoning")
    if value is None or isinstance(value, bool):
        return bool(value)
    return str(value).lower() in _TRUE_WORDS


async def decide(hass, provider, *, honorific: str, event_summary: str,
                 home_state_summary: str, classifier_urgency: str,
                 classifier_category: str, recent_announcements: list[str],
                 presence_context: str = "", anyone_home: bool = False,
                 entity_id: str = "", device_class: str = "", from_state: str = "",
                 to_state: str = "", friendly_name: str = "",
                 trace: Optional[dict] = None) -> dict:
    """Whether to speak about a flagged event: local rules first (free), then
    the cache of past model decisions, then the model, whose checked answer
    is cached. With the connection breaker open the model is skipped. The
    hooks are read now, so a patched rule or switch takes effect. See
    cognitive.coordinator."""
    fields = dict(
        honorific=honorific, event_summary=event_summary,
        home_state_summary=home_state_summary, classifier_urgency=classifier_urgency,
        classifier_category=classifier_category,
        recent_announcements=recent_announcements, presence_context=presence_context,
        anyone_home=anyone_home, entity_id=entity_id, device_class=device_class,
        from_state=from_state, to_state=to_state, friendly_name=friendly_name,
        trace=trace)
    hooks = _coordinator.Hooks(local=_try_local_reasoning, rich_mode=_rich_mode,
                               build_system_prompt=build_system_prompt,
                               system_appendix=REASONING_SYSTEM_APPENDIX)
    return await _coordinator.decide(hass, provider, hooks=hooks, **fields)
