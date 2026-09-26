"""Which decisions may be learned into the reasoning cache.

Only a validated provider verdict (speak with a message, or stay silent) is
ever learned. Local rules, Local Mind and fallback decisions, cache replays
and anything built from an unreadable, incomplete or failed provider reply
are never written, so a reply Nova could not read can never become learned
silence. Pure.
"""
from __future__ import annotations

from .models import ACTION_SILENT, ACTION_SPEAK, Decision, ORIGIN_PROVIDER, URGENCIES


def may_cache(decision: Decision) -> bool:
    if decision is None or not decision.cacheable:
        return False
    if not decision.validated or decision.provider_failure:
        return False
    if decision.origin != ORIGIN_PROVIDER or not decision.provider_used:
        return False
    if decision.action == ACTION_SILENT:
        return True
    if decision.action == ACTION_SPEAK:
        return bool(decision.message.strip()) and decision.urgency in URGENCIES
    return False


def cache_entry(decision: Decision, classifier_urgency: str) -> tuple[bool, str]:
    """(speak, urgency) to learn for an eligible decision: silence is learned
    at the classifier's urgency, speech at the provider's validated urgency."""
    if decision.action == ACTION_SILENT:
        return False, classifier_urgency
    return True, str(decision.urgency)
