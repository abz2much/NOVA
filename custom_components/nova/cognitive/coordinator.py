"""The cognitive coordinator: the one place a proactive decision is made and
the only cognitive module allowed effects.

For one flagged event it:
  1. collects an immutable EventSnapshot once;
  2. runs the pure local evaluators in their explicit priority order and
     arbitrates between them (critical safety first);
  3. replays a learned verdict from the reasoning cache when one is fresh;
  4. asks the provider only where v7.119.0 did (no rule applied, no fresh
     cache entry, the connectivity breaker allows it) — never twice for one
     decision beyond the existing transient-error backoff;
  5. validates the reply; an unreadable, incomplete or invalid reply is a
     provider failure and the Local Mind decides, exactly as for an
     unreachable provider;
  6. learns only validated provider verdicts (cache_policy);
  7. returns the decision. Delivery (the output gate, routing, speech,
     notifications, policy) stays with the observer, which calls the
     existing output gate for every announcement.

After an unreadable reply the event's signature is held for a short,
bounded time so a repeat of the same event goes straight to the fallback
instead of asking the same provider again. The hold stores no decision and
is never persisted. Cancellation always propagates.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Callable, NamedTuple, Optional

from . import cache_policy, presentation
from .arbitration import arbitrate
from .evaluators import (
    decision_from_cached,
    evaluate_basic_fallback,
    evaluate_local_rules,
    parse_summary,
)
from .models import (
    ACTION_SILENT,
    ACTION_SPEAK,
    Decision,
    EventSnapshot,
    ORIGIN_LOCAL_MIND,
    ProviderFailure,
    R_LOCAL_MIND,
)
from .provider import parse_reply

_LOGGER = logging.getLogger(__name__.rpartition(".cognitive")[0] + ".reasoning_loop")

# How long a signature whose provider reply was unreadable skips the
# provider, and how many signatures are remembered. Matches the
# connectivity breaker's cooldown; bounded so it can never grow.
UNREADABLE_HOLD_S = 60.0
UNREADABLE_HOLD_MAX = 64

_holds: "OrderedDict[str, float]" = OrderedDict()
_monotonic: Callable[[], float] = time.monotonic


def reset_provider_holds() -> None:
    _holds.clear()


def provider_held(sig: str) -> bool:
    now = _monotonic()
    until = _holds.get(sig)
    if until is None:
        return False
    if until <= now:
        _holds.pop(sig, None)
        return False
    return True


def _hold(sig: str) -> None:
    now = _monotonic()
    for key in [k for k, until in _holds.items() if until <= now]:
        _holds.pop(key, None)
    _holds[sig] = now + UNREADABLE_HOLD_S
    _holds.move_to_end(sig)
    while len(_holds) > UNREADABLE_HOLD_MAX:
        _holds.popitem(last=False)


class Hooks(NamedTuple):
    """The reasoning_loop seams the coordinator calls, resolved at call time
    so the module's long-standing monkeypatch points keep working."""

    local: Callable[..., Optional[dict]]
    rich_mode: Callable[[Any], bool]
    build_system_prompt: Callable[..., str]
    system_appendix: str


# ── Local rules ─────────────────────────────────────────────────────────────

def local_decision(snapshot: EventSnapshot, honorific: str) -> Optional[Decision]:
    """The winning local rule for this snapshot, voiced, or None."""
    winner = arbitrate(evaluate_local_rules(snapshot))
    if winner is None:
        return None
    return presentation.voiced(winner, honorific)


def decision_from_legacy(out: dict, *, origin: str = ORIGIN_LOCAL_MIND) -> Decision:
    """A typed view of a legacy decision dict (a patched seam or the Local
    Mind façade), keeping exactly the keys it carried."""
    speak = bool(out.get("speak"))
    extra = tuple(sorted((k, v) for k, v in out.items()
                         if k not in ("speak", "message", "urgency", "reason")))
    return Decision(ACTION_SPEAK if speak else ACTION_SILENT, R_LOCAL_MIND,
                    urgency=out.get("urgency") if "urgency" in out else None,
                    reason=out.get("reason") if "reason" in out else None,
                    message=str(out.get("message", "")) if speak else "",
                    origin=origin, extra=extra)


# ── Effects: history lookup, Local Mind, cache ─────────────────────────────

async def replay_cached(hass, cached: dict, snapshot: EventSnapshot, honorific: str) -> dict:
    """A decision from a learned cache entry. The verdict comes from the
    cache; the voice from the Local Mind's composer, with a quick history
    lookup so replayed decisions carry novelty context."""
    from datetime import datetime
    hour = datetime.now().hour
    grade = "unknown"
    if cached.get("speak"):
        from .. import local_mind
        try:
            prof = await hass.async_add_executor_job(
                local_mind.history_profile, snapshot.entity_id, snapshot.to_state, hour)
            grade = prof.get("grade", "unknown")
        except Exception:
            pass
    d = decision_from_cached(snapshot, cached, grade=grade, hour=hour)
    return presentation.voiced(d, honorific).as_dict()


async def _local_mind(hass, snapshot: EventSnapshot, honorific: str) -> dict:
    try:
        from .. import local_mind
        return await local_mind.assess(
            hass, honorific=honorific, entity_id=snapshot.entity_id, domain=snapshot.domain,
            device_class=snapshot.device_class, category=snapshot.category,
            from_state=snapshot.from_state, to_state=snapshot.to_state,
            friendly_name=snapshot.friendly_name, urgency=snapshot.urgency,
            anyone_home=snapshot.anyone_home,
            recent_announcements=list(snapshot.recent_announcements))
    except Exception as exc:
        _LOGGER.debug("Local Mind error (%s) — basic fallback", exc)
        return presentation.voiced(evaluate_basic_fallback(snapshot), honorific).as_dict()


async def _provider_failure_fallback(hass, sig: str, snapshot: EventSnapshot,
                                     honorific: str) -> dict:
    """What a failed provider leaves: a stale but validated learned verdict
    when one exists, else the Local Mind."""
    from .. import reasoning_cache
    stale = reasoning_cache.get(sig, ignore_age=True)
    if stale is not None:
        reasoning_cache.note_hit(sig)
        return await replay_cached(hass, stale, snapshot, honorific)
    return await _local_mind(hass, snapshot, honorific)


def _learn(sig: str, decision: Decision, classifier_urgency: str) -> None:
    """The only reasoning-cache write in the cognitive path."""
    if not cache_policy.may_cache(decision):
        return
    from .. import reasoning_cache
    speak, urgency = cache_policy.cache_entry(decision, classifier_urgency)
    reasoning_cache.remember(sig, speak, urgency)


def _is_transient(err_str: str) -> bool:
    return ("503" in err_str or "UNAVAILABLE" in err_str or "429" in err_str
            or "RESOURCE_EXHAUSTED" in err_str or "500" in err_str
            or "timeout" in err_str.lower())


# ── The decision ────────────────────────────────────────────────────────────

async def decide(hass, provider, *, hooks: Hooks, honorific: str, event_summary: str,
                 home_state_summary: str, classifier_urgency: str,
                 classifier_category: str, recent_announcements: list,
                 presence_context: str = "", anyone_home: bool = False,
                 entity_id: str = "", device_class: str = "", from_state: str = "",
                 to_state: str = "", friendly_name: str = "") -> dict:
    # A presence transition touching the literal "home" state is decided by
    # exact state comparison, never by provider judgment (see reasoning_loop).
    is_home_boundary_transition = classifier_category == "presence" and (
        (to_state == "home" and from_state != "home")
        or (from_state == "home" and to_state != "home"))

    # Rich Reasoning: medium/high events go to the provider first; low stays
    # local, the breaker still guards the call, and any failure falls back.
    rich = (hooks.rich_mode(hass) and classifier_urgency in ("medium", "high")
            and not is_home_boundary_transition)

    if not rich:
        local = hooks.local(
            event_summary, classifier_urgency, classifier_category, honorific,
            recent_announcements, anyone_home, from_state=from_state, to_state=to_state,
            entity_id=entity_id, friendly_name=friendly_name, device_class=device_class)
        if local is not None:
            _LOGGER.info("Reasoning local: %s",
                         local.get("message", local.get("reason", ""))[:80])
            return local

    from .. import connectivity, reasoning_cache

    # Backfill structured fields from the summary if not provided.
    if not (domain := (entity_id.split(".", 1)[0] if "." in entity_id else "")):
        f2, e2, d2, fs2, ts2 = parse_summary(event_summary)
        friendly_name = friendly_name or f2
        entity_id = entity_id or e2
        domain = d2
        from_state = from_state or fs2
        to_state = to_state or ts2

    snapshot = EventSnapshot.build(
        entity_id=entity_id, domain=domain, device_class=device_class,
        from_state=from_state, to_state=to_state, friendly_name=friendly_name,
        event_summary=event_summary, category=classifier_category,
        urgency=classifier_urgency, anyone_home=anyone_home,
        recent_announcements=recent_announcements)

    sig = reasoning_cache.signature(
        domain, device_class, classifier_category, from_state, to_state,
        anyone_home, classifier_urgency)

    cached = reasoning_cache.get(sig)
    if cached is not None and not rich:
        reasoning_cache.note_hit(sig)
        dec = await replay_cached(hass, cached, snapshot, honorific)
        _LOGGER.info("Reasoning cache hit [%s]: speak=%s", sig, dec.get("speak"))
        return dec

    # No fresh cache → we'd call the provider. Respect the breaker.
    if not connectivity.allow_request():
        reasoning_cache.note_hit(sig)
        _LOGGER.info("Reasoning: breaker OPEN — Local Mind for [%s]", sig)
        return await _local_mind(hass, snapshot, honorific)

    # The provider just returned an unreadable reply for this same event:
    # don't ask it again yet, decide as for any provider failure.
    if provider_held(sig):
        reasoning_cache.note_hit(sig)
        _LOGGER.info("Reasoning: recent unreadable reply — fallback for [%s]", sig)
        return await _provider_failure_fallback(hass, sig, snapshot, honorific)

    reasoning_cache.note_cloud_call()
    system = hooks.build_system_prompt(
        hass, honorific=honorific, task_context="observer") + "\n\n" + hooks.system_appendix
    recent_block = ""
    if recent_announcements:
        recent_block = "\n\nRecent announcements (avoid repeating):\n" + "\n".join(
            f"- {a}" for a in recent_announcements[-5:])
    presence_block = f"\n\nPresence: {presence_context}" if presence_context else ""
    user_msg = (
        f"FLAGGED EVENT:\n{event_summary}\n\n"
        f"Classifier suggested urgency: {classifier_urgency}\n"
        f"Category: {classifier_category}\n\n"
        f"Home state summary:\n{home_state_summary}"
        f"{presence_block}"
        f"{recent_block}\n\n"
        "Decide: speak or stay silent? Respond with JSON only."
    )
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user_msg}]

    try:
        # The activity boundary runs the blocking SDK call in the executor.
        # Transient 503/429/500/timeout errors back off and retry, as before.
        from ..providers.activity import execute_chat
        from ..providers.errors import error_text
        response = None
        last_err = None
        for attempt in range(3):
            try:
                response = await execute_chat(
                    hass, provider, messages, role="reasoning", data_category="text",
                    temperature=0.4, max_tokens=200)
                break
            except Exception as exc:
                last_err = exc
                if not _is_transient(error_text(exc)) or attempt == 2:
                    raise
                import asyncio
                backoff = 2 ** attempt  # 1s, 2s
                _LOGGER.info(
                    "Reasoning loop transient error (attempt %d), backing off %ds: %s",
                    attempt + 1, backoff, str(exc)[:120])
                await asyncio.sleep(backoff)
        if response is None:
            raise last_err or RuntimeError("no response after retries")
        # The network call succeeded — close the breaker.
        connectivity.record_success()
        outcome = parse_reply(getattr(response, "text", None),
                              classifier_urgency=classifier_urgency)
    except Exception as exc:
        _LOGGER.warning("Reasoning loop failed: %s", exc)
        connectivity.record_failure()
        return await _provider_failure_fallback(hass, sig, snapshot, honorific)

    if isinstance(outcome, ProviderFailure):
        # A reply Nova can't read is a provider failure, never a decision.
        _LOGGER.warning(
            "Reasoning: provider reply not usable (%s: %s, %d chars) — fallback",
            outcome.kind, outcome.detail, outcome.reply_length)
        _hold(sig)
        out = await _provider_failure_fallback(hass, sig, snapshot, honorific)
        out = dict(out)
        out.setdefault("provider_failure", outcome.kind)
        return out

    _learn(sig, outcome, classifier_urgency)
    return outcome.as_dict()
