"""Nova — shared entity-state verification helpers (Phase 3).

Used by both agent.py (the LLM tool-calling path) and local_engine.py (the
fast local path) so "verified"/"accepted"/"unverified" mean exactly the
same thing regardless of which path handled the action. Deliberately
dependency-light: only needs `hass` and plain values, so importing it from
either agent.py or local_engine.py introduces no circular import — neither
of those two files imports the other today (confirmed: agent.py has zero
references to local_engine other than a comment; local_engine.py has zero
references to agent at all), and this module imports neither of them.

Everything here is OBSERVE-ONLY. Nothing in this module ever calls a
Home Assistant service or retries an action — that stays the caller's
business (agent.py's existing, unchanged _verify_control owns the
idempotent-action retry/background path).
"""
from __future__ import annotations

import asyncio

FAST_VERIFY_DOMAINS = {"light", "switch", "fan"}
FAST_POLL_INTERVAL = 0.25
FAST_POLL_TIMEOUT = 1.0

_SLEEP = asyncio.sleep  # module-level seam, same precedent as agent.py's _VERIFY_SLEEP


def check_state_once(hass, entity_id: str, expected: str) -> bool:
    """A single, synchronous observation. Never sleeps, never retries."""
    st = hass.states.get(entity_id)
    return st is not None and st.state == expected


def check_brightness_once(hass, entity_id: str, expected_pct, tolerance_pct: float = 2.0) -> bool:
    """On AND the brightness attribute is within `tolerance_pct` of the
    requested percentage. Tolerance rationale: HA quantizes brightness to
    an 0-255 integer, so a percentage round-trip (request -> int(pct/100*255)
    -> read back as a percentage) introduces an unavoidable ~0.4% rounding
    step per conversion; 2% gives comfortable headroom without being loose
    enough to falsely "verify" a materially wrong level."""
    st = hass.states.get(entity_id)
    if st is None or st.state != "on":
        return False
    brightness = st.attributes.get("brightness")
    if brightness is None or expected_pct is None:
        return False
    actual_pct = brightness / 255 * 100
    return abs(actual_pct - float(expected_pct)) <= tolerance_pct


async def wait_until(check_callable, interval: float = FAST_POLL_INTERVAL,
                      timeout: float = FAST_POLL_TIMEOUT) -> bool:
    """Predicate-based, bounded poll. Calls `check_callable()` immediately,
    before ever sleeping — an already-correct state returns instantly with
    no sleep at all. Polls every `interval` seconds until `timeout` is
    reached. Never calls a service, never retries an action — purely
    observes via whatever `check_callable` closes over."""
    if check_callable():
        return True
    elapsed = 0.0
    while elapsed < timeout:
        await _SLEEP(interval)
        elapsed += interval
        if check_callable():
            return True
    return False


async def record_unverified(hass, entity_id: str, action: str, source: str,
                             detail: str = "") -> None:
    """For fast actions that finished the bounded check unconfirmed AND must
    NOT be auto-retried (toggle, brightness) — record honestly, once, no
    retry, no service call. `source` distinguishes which caller recorded it
    ("agent" or "local_engine"), matching the existing database.save_activity
    convention. Uses the executor (this is a synchronous DB write) rather
    than agent.py's existing _verify_control's direct-call style, since this
    helper has no `hass`-bound event-loop assumptions of its own to match."""
    from . import database
    st = hass.states.get(entity_id)
    observed = st.state if st else "unknown"
    message = (
        f"{entity_id} could not be confirmed after {action} "
        f"(state: {observed}{detail}) — not retried automatically."
    )
    await hass.async_add_executor_job(
        lambda: database.save_activity(
            entity_id=entity_id, category="verify", urgency="low",
            message=message, source=source,
        )
    )
