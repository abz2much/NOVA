"""
Nova — Observer Tier 2: the reasoning loop (v5.7.00).

When the classifier flags an event as worth considering, this tier takes a
full contextual look. Local templates handle 95%+ of events — LLM fallback
is now reserved for genuinely ambiguous multi-factor decisions only.
  - The event that was flagged
  - The current home state summary
  - Nova's persona and prime directive
  - The urgency the classifier suggested
  - A list of recent announcements (so it doesn't repeat itself)

It returns either:
  - A "speak" decision: {"speak": true, "message": "...", "urgency": "..."}
  - A "stay silent" decision: {"speak": false, "reason": "..."}

The model is Gemini Flash by default — more capable than Flash-Lite but
still cheap. Uses thinking_budget normally (lets it reason).

The observer calls this, then hands the result (if speak=true) to the
output gate for rate limiting and routing.

Since Phase 8 this module is a compatibility façade: the decision is made by
cognitive.coordinator from the pure evaluators in cognitive.evaluators. The
names below (decide, _try_local_reasoning, _structured_hazard, _rich_mode,
REASONING_SYSTEM_APPENDIX, …) keep their signatures and behaviour.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from .cognitive import coordinator as _coordinator
from .cognitive import evaluators as _evaluators
from .cognitive import presentation as _presentation
from .cognitive.models import EventSnapshot
from .cognitive.provider import extract_json_text as _extract_json_text
from .directive_helper import build_system_prompt

_LOGGER = logging.getLogger(__name__)

# Kept for compatibility; the canonical definitions live in cognitive.evaluators.
_ACTIVE_TRIGGER_STATES = set(_evaluators.ACTIVE_TRIGGER_STATES)
_CRITICAL_HAZARD_CLASSES = _evaluators.CRITICAL_HAZARD_CLASSES
_HAZARD_WORDS = dict(_evaluators.HAZARD_WORDS)
_summary_new_state = _evaluators.summary_new_state
_parse_summary = _evaluators.parse_summary


def _snapshot(event_summary="", urgency="", category="", recent_announcements=(),
              anyone_home=False, from_state="", to_state="", entity_id="",
              friendly_name="", device_class="") -> EventSnapshot:
    return EventSnapshot.build(
        entity_id=entity_id, device_class=device_class, from_state=from_state,
        to_state=to_state, friendly_name=friendly_name, event_summary=event_summary,
        category=category, urgency=urgency, anyone_home=anyone_home,
        recent_announcements=recent_announcements)


def _structured_hazard(device_class: str, to_state: str, event_summary: str,
                       entity_id: str, friendly_name: str,
                       honorific: str) -> Optional[dict]:
    """Decision for a critical hazard sensor, from structured fields only —
    never from words in its name. Returns None when device_class isn't a
    critical hazard class, or when there's no new state to judge."""
    d = _evaluators.evaluate_structured_hazard(_snapshot(
        event_summary=event_summary, to_state=to_state, entity_id=entity_id,
        friendly_name=friendly_name, device_class=device_class))
    return None if d is None else _presentation.voiced(d, honorific).as_dict()


async def _decision_from_cache(hass, cached: dict, *, honorific: str,
                               friendly_name: str, entity_id: str,
                               device_class: str, to_state: str,
                               anyone_home: bool) -> dict:
    """Build a decision from a learned cache entry (no provider call)."""
    return await _coordinator.replay_cached(hass, cached, _snapshot(
        entity_id=entity_id, friendly_name=friendly_name, device_class=device_class,
        to_state=to_state, anyone_home=anyone_home), honorific)


def _local_fallback(urgency: str, friendly_name: str, to_state: str,
                    honorific: str) -> dict:
    """Deterministic local decision when the provider is unavailable and
    nothing is cached. Surfaces important events; stays quiet for routine ones."""
    d = _evaluators.evaluate_basic_fallback(_snapshot(
        urgency=urgency, friendly_name=friendly_name, to_state=to_state))
    return _presentation.voiced(d, honorific).as_dict()


def _lm_compose(honorific, friendly_name, *, device_class="", to_state="",
                away=False, escalated=False) -> str:
    """Compose template speech through the Local Mind's voice; never raises."""
    return _presentation._lm_compose(honorific, friendly_name, device_class=device_class,
                                     to_state=to_state, away=away, escalated=escalated)


def _try_local_reasoning(
    event_summary: str,
    urgency: str,
    category: str,
    honorific: str,
    recent_announcements: list[str],
    anyone_home: bool = False,
    from_state: str = "",
    to_state: str = "",
    entity_id: str = "",
    friendly_name: str = "",
    device_class: str = "",
) -> Optional[dict]:
    """
    Handle common events with templated responses: the local rules in
    cognitive.evaluators.LOCAL_RULES, in their explicit priority order.

    A critical hazard sensor is decided first, by structured device_class and
    then by the name-based fallback, before recent-announcement dedup and the
    someone-home security shortcut, so an active smoke/gas/leak/CO alarm is
    always voiced whatever the sensor is called or who is home.

    Returns a decision dict or None (fall through to the cache and provider).
    """
    d = _coordinator.local_decision(_snapshot(
        event_summary=event_summary, urgency=urgency, category=category,
        recent_announcements=recent_announcements, anyone_home=anyone_home,
        from_state=from_state, to_state=to_state, entity_id=entity_id,
        friendly_name=friendly_name, device_class=device_class), honorific)
    return None if d is None else d.as_dict()


REASONING_SYSTEM_APPENDIX = """
You are currently operating in OBSERVER MODE — not responding to a direct \
request, but deciding whether to proactively speak to the user about something \
happening in the house.

You receive a flagged event and must decide: is this worth announcing RIGHT \
NOW? If yes, how would you phrase it in character?

STRONG BIAS TOWARD SILENCE. The user did not ask. Interrupting is costly. \
Only speak if a reasonable butler in your position would feel it's their duty \
to mention this. When in doubt, stay silent.

Consider:
- Is this something the user would want to know NOW, or can it wait?
- Is this something they'd consider useful or annoying?
- Has this been announced recently? (If yes, stay silent unless truly changed.)
- Is the user likely busy / not listening right now?
- If `presence_context` says the user is sleeping, the bar for speaking is \
  MUCH higher. Only speak if it's a real emergency.

URGENCY DEFINITIONS (be strict — misclassifying spams the user):

- "critical" — immediate physical danger requiring the user to wake up / act NOW.
  Examples: smoke alarm, CO alarm, gas leak, water leak, glass break while \
  armed, active intrusion. NOTHING ELSE IS CRITICAL. A door opening is NOT \
  critical. A light turning on is NOT critical. Motion detected is NOT critical.

- "high" — important, user would want to know within the hour but can handle \
  asynchronously. Examples: package delivery, doorbell while away, mail arrival, \
  unexpected window open while away.

- "medium" — useful to mention if the user is present and awake. Examples: \
  laundry cycle complete, garage door left open more than 15 minutes, family \
  member arrived home.

- "low" — minor ambient observation. Examples: lights left on in unoccupied \
  room, indoor temperature drift, routine sensor state change.

DEFAULT TO "low" OR "medium" UNLESS YOU HAVE STRONG EVIDENCE OTHERWISE.

HOW Nova REASONS (when you do speak):
- Don't merely report the event — convey what it means. "The garage has been \
open twenty minutes" is better as "The garage has been open twenty minutes, \
{honorific} — worth a glance before dark." You connect the fact to its \
implication.
- Anticipate. If a window is open and the temperature is dropping, the useful \
observation is the combination, not either fact alone.
- Lead with the thing that matters. One clause of substance beats two of padding.
- Stay understated even when flagging something real. Nova does not alarm; he \
informs, calmly, and trusts the user to act.

Your response must be JSON only:
  {"speak": false, "reason": "why staying silent"}
  OR
  {"speak": true, "message": "what to say", "urgency": "low|medium|high|critical"}

Keep messages short. One or two sentences. In character. No preamble. No \
exclamation marks unless it is a genuine emergency.
"""


def _parse_reasoning_json(raw: str) -> dict:
    """Lenient legacy parse of the reply, kept for compatibility. Decisions
    no longer use it: cognitive.provider.parse_reply validates replies."""
    raw = _extract_json_text(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        _LOGGER.debug("Reasoning: could not parse JSON from: %s", raw[:300])
        return {"speak": False, "reason": "parse_failure"}


def _rich_mode(hass) -> bool:
    """Live read of the panel's Rich Reasoning toggle (runtime_config), with the
    persisted store as fallback. Defaults off — efficiency stays the baseline."""
    from .runtime import domain_runtime_config
    v = domain_runtime_config(hass).get("rich_reasoning")
    if v is not None:
        return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    return False


async def decide(
    hass,
    provider,
    *,
    honorific: str,
    event_summary: str,
    home_state_summary: str,
    classifier_urgency: str,
    classifier_category: str,
    recent_announcements: list[str],
    presence_context: str = "",
    anyone_home: bool = False,
    entity_id: str = "",
    device_class: str = "",
    from_state: str = "",
    to_state: str = "",
    friendly_name: str = "",
) -> dict:
    """
    Decide whether to speak about a flagged event.
    Tries local rules first (zero cost), then a learned cache of past
    provider decisions, then the provider (whose validated decision is then
    learned). When the connectivity breaker is OPEN, skips the provider and
    decides locally. See cognitive.coordinator.
    """
    hooks = _coordinator.Hooks(
        local=_try_local_reasoning, rich_mode=_rich_mode,
        build_system_prompt=build_system_prompt,
        system_appendix=REASONING_SYSTEM_APPENDIX)
    return await _coordinator.decide(
        hass, provider, hooks=hooks, honorific=honorific, event_summary=event_summary,
        home_state_summary=home_state_summary, classifier_urgency=classifier_urgency,
        classifier_category=classifier_category,
        recent_announcements=recent_announcements, presence_context=presence_context,
        anyone_home=anyone_home, entity_id=entity_id, device_class=device_class,
        from_state=from_state, to_state=to_state, friendly_name=friendly_name)
