"""The Faces tab commands: nova/list_faces, nova/add_resident, nova/remove_resident.

Nova has no face engine of its own. Names come from Frigate or Double Take
(recognition.py); the roster (face_roster.py) says which of them are residents.
All three commands are admin gated: the list shows who was seen on which camera
and the roster is what the opt in intrusion stand down trusts, so they are
treated like the other commands that reveal what the home saw or change who it
trusts (nova/get_spoken_history, nova/list_actions, nova/camera_snapshot).
No image is returned or stored.

websocket.py imports the handlers by name (async_register registers them).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .safe_errors import safe_error_message

_LOGGER = logging.getLogger(f"{__package__}.websocket")


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_faces",
    vol.Optional("limit"): vol.All(int, vol.Range(min=1, max=100)),
})
@websocket_api.async_response
async def ws_list_faces(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Recent faces, the roster and what identity sources Nova can see."""
    try:
        from . import face_roster, recognition
        connection.send_result(msg["id"], {
            "faces": recognition.recent_faces(hass, msg.get("limit", 20)),
            "residents": face_roster.names(),
            "sources": recognition.source_status(hass),
            "confidence_threshold": recognition.CONFIDENCE_THRESHOLD,
        })
    except Exception as exc:
        _LOGGER.exception("nova/list_faces failed: %s", exc)
        connection.send_error(msg["id"], "faces_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/add_resident",
    vol.Required("name"): str,
})
@websocket_api.async_response
async def ws_add_resident(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Add a household resident by the name Frigate or Double Take uses."""
    try:
        from . import face_roster
        res = await face_roster.async_add(hass, msg["name"])
        if res["error"] == "invalid_name":
            connection.send_error(
                msg["id"], "invalid_name",
                "A name is 1 to 60 characters, with no control characters, "
                "and cannot be 'unknown'")
            return
        if res["error"] == "roster_full":
            connection.send_error(
                msg["id"], "roster_full",
                f"The roster holds at most {face_roster.MAX_RESIDENTS} residents")
            return
        if res["error"] == "failed":
            connection.send_error(msg["id"], "add_failed",
                                  "The resident could not be added")
            return
        connection.send_result(msg["id"], {
            "added": res["added"], "residents": res["residents"],
            "saved": res["error"] != "not_saved"})
    except Exception as exc:
        _LOGGER.exception("nova/add_resident failed: %s", exc)
        connection.send_error(msg["id"], "faces_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/remove_resident",
    vol.Required("name"): str,
})
@websocket_api.async_response
async def ws_remove_resident(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Remove a resident from the roster."""
    try:
        from . import face_roster
        res = await face_roster.async_remove(hass, msg["name"])
        if res["error"] == "invalid_name":
            connection.send_error(msg["id"], "invalid_name", "Give the name to remove")
            return
        if res["error"] == "failed":
            connection.send_error(msg["id"], "remove_failed",
                                  "That name could not be removed")
            return
        connection.send_result(msg["id"], {
            "removed": res["removed"], "residents": res["residents"],
            "saved": res["error"] != "not_saved"})
    except Exception as exc:
        _LOGGER.exception("nova/remove_resident failed: %s", exc)
        connection.send_error(msg["id"], "faces_failed", safe_error_message(exc))
