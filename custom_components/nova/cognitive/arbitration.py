"""Deterministic arbitration between candidate decisions.

Candidates arrive in the evaluators' explicit priority order (LOCAL_RULES)
and the first one decides. Critical safety holds its priority by position:
the hazard evaluators run first, so no occupancy shortcut or dedup rule can
reach an active smoke, gas, leak or carbon monoxide alarm, and their
all-clear verdicts are not overridden by a broader rule further down.

One exception keeps a critical alarm from being deduplicated away: when the
first candidate only says "recently announced", a critical safety verdict
further down (an alarm panel that has triggered) still speaks. Ties never
depend on set or dictionary order, because the order is the tuple the
evaluators were declared in. Pure.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .models import Decision, R_RECENTLY_ANNOUNCED

# Evaluators whose critical speech outranks repetition.
SAFETY_EVALUATORS = frozenset({"critical_hazard", "named_hazard", "alarm"})


def arbitrate(candidates: Iterable[Optional[Decision]]) -> Optional[Decision]:
    ordered = [d for d in candidates if d is not None]
    if not ordered:
        return None
    first = ordered[0]
    if first.reason_code == R_RECENTLY_ANNOUNCED:
        for d in ordered[1:]:
            if d.is_critical and d.evaluator in SAFETY_EVALUATORS:
                return d
    return first
