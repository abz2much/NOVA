"""The convenience path's panel commands (8.48.0).

nova/convenience_status   each anticipation pattern with its shadow
                          comparison, whether it is ready to switch over and
                          whether to ask about stopping it, plus the latest
                          pairs of verdicts
nova/convenience_choice   your choice for one pattern: stop, restart, keep
                          (no, don't stop it), activate or deactivate

Both are admin only: they show what Nova noticed about the household and
change what Nova tells it.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .safe_errors import safe_error_message

_LOGGER = logging.getLogger(f"{__package__}.websocket")

CHOICES = ("stop", "restart", "keep", "activate", "deactivate")


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/convenience_status",
})
@websocket_api.async_response
async def ws_convenience_status(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    try:
        from . import convenience
        connection.send_result(msg["id"], await hass.async_add_executor_job(
            convenience.status))
    except Exception as exc:
        _LOGGER.exception("convenience_status failed: %s", exc)
        connection.send_error(msg["id"], "convenience_status_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/convenience_choice",
    vol.Required("pattern_key"): vol.All(str, vol.Length(min=1, max=300)),
    vol.Required("choice"): vol.In(CHOICES),
})
@websocket_api.async_response
async def ws_convenience_choice(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    try:
        from . import convenience
        status = await hass.async_add_executor_job(
            convenience.set_choice, msg["pattern_key"], msg["choice"])
        connection.send_result(msg["id"], {"status": status})
    except Exception as exc:
        _LOGGER.exception("convenience_choice failed: %s", exc)
        connection.send_error(msg["id"], "convenience_choice_failed", safe_error_message(exc))
