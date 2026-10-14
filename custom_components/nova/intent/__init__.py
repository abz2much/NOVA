"""Nova's local voice commands (see intent_router)."""
from __future__ import annotations

from .intent_router import (
    EVENT_FEEDBACK_WINDOW,
    LocalIntentRouter,
    is_affirmative,
    match_intent,
)

__all__ = ["EVENT_FEEDBACK_WINDOW", "LocalIntentRouter", "async_setup_intents",
           "is_affirmative", "match_intent"]


async def async_setup_intents(hass) -> None:
    """Does nothing, on purpose.

    Home Assistant's intent loader calls async_setup_intents on any
    integration that has an ``intent`` module, and Nova's intent package is
    its own command layer, not a set of Home Assistant intent handlers. The
    function only has to exist: without it Home Assistant 2026.8 logged
    "Unexpected error during intent recognition"."""
    return None
