"""Circuit breakers tracking whether Nova's cloud models are reachable.

There is one breaker per tier: "agent" (the conversation, the default) and
"reasoning" (the cognitive core's cloud fallback). A tier that keeps failing
opens only its own breaker, so a broken reasoning model cannot take the
conversation offline.

Callers report each real agent call's outcome and ask before making one. Once
enough calls fail in a row the breaker opens and Nova falls back to local-only
handling; after a cooldown a single probe call is allowed through, and its
outcome either closes the breaker or opens it again.

Nothing runs in the background: every transition happens inside a public call,
using the monotonic clock read at that moment, so wall-clock steps cannot
stretch or shorten a cooldown. State is kept in memory only.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

_FAILURE_THRESHOLD = 2
_OPEN_COOLDOWN = 60.0
_HALF_OPEN_MAX_PROBES = 1

_CLOSED = "closed"
_OPEN = "open"
_HALF_OPEN = "half_open"


@dataclass
class _BreakerState:
    """Mutable breaker state; one instance lives for the whole process."""

    state: str = _CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    half_open_probes: int = 0
    probe_at: float = 0.0  # when the latest half-open probe was handed out
    last_success: float = 0.0  # 0.0 means "never"
    last_failure: float = 0.0  # 0.0 means "never"
    total_failures: int = 0
    total_successes: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_BREAKER = _BreakerState()          # the "agent" tier
_DEFAULT_TIER = "agent"
_BREAKERS: dict[str, _BreakerState] = {_DEFAULT_TIER: _BREAKER}
_REGISTRY_LOCK = threading.Lock()


def _breaker(tier: str) -> _BreakerState:
    """The breaker for a tier, made on first use."""
    with _REGISTRY_LOCK:
        found = _BREAKERS.get(tier)
        if found is None:
            found = _BREAKERS[tier] = _BreakerState()
        return found


def _now() -> float:
    # Looked up through the module global each time so tests can swap the clock.
    return time.monotonic()


def allow_request(tier: str = _DEFAULT_TIER) -> bool:
    """Return True if a cloud call for this tier should be attempted now."""
    b = _breaker(tier)
    with b.lock:
        if b.state == _CLOSED:
            return True
        if b.state == _OPEN:
            if _now() - b.opened_at < _OPEN_COOLDOWN:
                return False
            b.state = _HALF_OPEN
            b.half_open_probes = 0
            _LOGGER.info("Agent breaker cooldown elapsed; allowing a probe call")
        # Half-open: hand out a limited number of probes. A probe whose
        # outcome was never recorded (its call was cancelled) is abandoned
        # after one cooldown, so another probe may go out.
        now = _now()
        if (b.half_open_probes >= _HALF_OPEN_MAX_PROBES
                and now - b.probe_at >= _OPEN_COOLDOWN):
            b.half_open_probes = 0
            _LOGGER.info("Agent probe call never reported back; allowing another")
        if b.half_open_probes < _HALF_OPEN_MAX_PROBES:
            b.half_open_probes += 1
            b.probe_at = now
            return True
        return False


def record_success(tier: str = _DEFAULT_TIER) -> None:
    """Report a successful call; closes the tier's breaker from any state."""
    b = _breaker(tier)
    with b.lock:
        was = b.state
        b.state = _CLOSED
        b.consecutive_failures = 0
        b.half_open_probes = 0
        b.last_success = _now()
        b.total_successes += 1
    if was != _CLOSED:
        _LOGGER.info("Agent call succeeded; breaker closed (was %s)", was)


def record_failure(tier: str = _DEFAULT_TIER) -> None:
    """Report a failed call; may open or re-open the tier's breaker."""
    b = _breaker(tier)
    with b.lock:
        now = _now()
        b.consecutive_failures += 1
        b.total_failures += 1
        b.last_failure = now
        failures = b.consecutive_failures
        if b.state == _HALF_OPEN:
            b.state = _OPEN
            b.opened_at = now
            b.half_open_probes = 0
            message: str | None = "Agent probe call failed; breaker re-opened"
        elif failures >= _FAILURE_THRESHOLD:
            first_open = b.state == _CLOSED
            b.state = _OPEN
            b.opened_at = now  # every failure while open restarts the cooldown
            message = (
                f"Agent unreachable after {failures} consecutive failures; "
                f"going local-only for {int(_OPEN_COOLDOWN)}s"
                if first_open
                else None
            )
        else:
            message = None
    if message:
        _LOGGER.warning(message)


def is_online(tier: str = _DEFAULT_TIER) -> bool:
    """Return True iff the tier's breaker is closed. Never changes state."""
    b = _breaker(tier)
    with b.lock:
        return b.state == _CLOSED


def status(tier: str = _DEFAULT_TIER) -> dict[str, Any]:
    """Return a JSON-serialisable diagnostics snapshot of one tier, with the
    state of every tier under "tiers". Never changes state."""
    b = _breaker(tier)
    with _REGISTRY_LOCK:
        everyone = list(_BREAKERS.items())
    tiers = {name: other.state for name, other in everyone}
    with b.lock:
        now = _now()
        cooldown = 0
        if b.state == _OPEN:
            cooldown = max(0, int(_OPEN_COOLDOWN - (now - b.opened_at)))
        return {
            "state": b.state,
            "online": b.state == _CLOSED,
            "consecutive_failures": b.consecutive_failures,
            "cooldown_remaining_s": cooldown,
            "total_failures": b.total_failures,
            "total_successes": b.total_successes,
            "last_success_ago_s": (
                max(0, int(now - b.last_success)) if b.last_success else None),
            "last_failure_ago_s": (
                max(0, int(now - b.last_failure)) if b.last_failure else None),
            "tiers": tiers,
        }


def reset() -> None:
    """Force every tier's breaker closed, keeping lifetime totals and timestamps."""
    with _REGISTRY_LOCK:
        everyone = list(_BREAKERS.values())
    for b in everyone:
        with b.lock:
            b.state = _CLOSED
            b.consecutive_failures = 0
            b.half_open_probes = 0
    _LOGGER.info("Agent breakers manually reset to closed")
