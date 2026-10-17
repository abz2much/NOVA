"""Nova's old local voice commands, removed in 8.61.0.

This package only holds async_setup_intents. Home Assistant's intent loader
calls it on any integration that has an ``intent`` module, and the function
has to exist: without it Home Assistant 2026.8 logged "Unexpected error
during intent recognition"."""
from __future__ import annotations

__all__ = ["async_setup_intents"]


async def async_setup_intents(hass) -> None:
    """Does nothing, on purpose."""
    return None
