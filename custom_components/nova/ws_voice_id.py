"""The Nova Voice ID panel commands (8.36.0).

nova/voice_id_profiles, nova/voice_id_record, nova/voice_id_record_status,
nova/voice_id_record_cancel,
nova/voice_id_delete, nova/voice_id_token_get and nova/voice_id_token_set.
All admin only. Each answers {"ok": True, ...} or {"ok": False, "code",
"error"}, where error is fixed text the panel shows as it is. The token is
only ever returned by nova/voice_id_token_get, for the panel's eye icon.

websocket.py imports every handler by name (async_register registers them).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .ws_bridge import _get_entry

_LOGGER = logging.getLogger(f"{__package__}.websocket")

_PERSON = vol.Match(r"^person\.[a-z0-9_]+$")


def _fail(connection, msg, code: str, error: str) -> None:
    connection.send_result(msg["id"], {"ok": False, "code": code, "error": error})


async def _app(hass):
    """(entry, uri, token) for a management call."""
    from . import voice_id_store
    entry = _get_entry(hass)
    uri = voice_id_store.get_uri(hass, entry) if entry is not None else ""
    token = await voice_id_store.async_get_token(hass)
    return entry, uri, token


def _people(hass, profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per person.* entity, plus any profile whose person is gone."""
    by_person = {p["person"]: p for p in profiles}
    rows = []
    for state in hass.states.async_all("person"):
        p = by_person.pop(state.entity_id, None)
        rows.append({
            "person": state.entity_id,
            "name": str((state.attributes or {}).get("friendly_name") or state.entity_id),
            "samples": p["samples"] if p else 0,
            "enrolled": p.get("enrolled", p["samples"]) if p else 0,
            "learned": p.get("learned", 0) if p else 0,
            "updated": p["updated"] if p else None,
        })
    for person, p in by_person.items():
        rows.append({"person": person, "name": person, "samples": p["samples"],
                     "enrolled": p.get("enrolled", p["samples"]), "learned": p.get("learned", 0),
                     "updated": p["updated"], "missing": True})
    return sorted(rows, key=lambda r: r["name"].lower())


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_id_profiles",
})
@websocket_api.async_response
async def ws_voice_id_profiles(hass: HomeAssistant, connection, msg: dict) -> None:
    """The People list: every person, with the app's sample count."""
    from . import voice_id
    _entry, uri, token = await _app(hass)
    try:
        listing = await voice_id.profile_list(uri, token) if uri else None
    except voice_id.VoiceIdManagementError as exc:
        connection.send_result(msg["id"], {
            "ok": False, "code": exc.code, "error": exc.message, "people": _people(hass, [])})
        return
    if listing is None:
        connection.send_result(msg["id"], {
            "ok": False, "code": "unreachable", "error": voice_id.ERROR_MESSAGES["unreachable"],
            "people": _people(hass, [])})
        return
    connection.send_result(msg["id"], {
        "ok": True, "people": _people(hass, listing["profiles"]), "model": listing["model"]})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_id_record",
    vol.Required("person"): _PERSON,
    vol.Required("satellite"): str,
})
@websocket_api.async_response
async def ws_voice_id_record(hass: HomeAssistant, connection, msg: dict) -> None:
    """Arm a recording session and announce the first sentence."""
    from . import voice_enrol
    entry = _get_entry(hass)
    try:
        state = await voice_enrol.async_start(hass, entry, msg["person"], msg["satellite"])
    except voice_enrol.EnrolError as exc:
        _fail(connection, msg, exc.code, exc.message)
        return
    connection.send_result(msg["id"], {"ok": True, "session": state})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_id_record_cancel",
})
@websocket_api.async_response
async def ws_voice_id_record_cancel(hass: HomeAssistant, connection, msg: dict) -> None:
    from . import voice_enrol
    cancelled = voice_enrol.cancel()
    connection.send_result(msg["id"], {"ok": True, "cancelled": cancelled,
                                       "session": voice_enrol.status()})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_id_record_status",
})
@websocket_api.async_response
async def ws_voice_id_record_status(hass: HomeAssistant, connection, msg: dict) -> None:
    """The recording session, for the card's live progress."""
    from . import voice_enrol
    connection.send_result(msg["id"], {"ok": True, "session": voice_enrol.status()})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_id_delete",
    vol.Exclusive("person", "who"): _PERSON,
    vol.Exclusive("all", "who"): True,
    vol.Optional("source"): "learned",
})
@websocket_api.async_response
async def ws_voice_id_delete(hass: HomeAssistant, connection, msg: dict) -> None:
    """Delete one person's voice, or all voice data, in the app."""
    from . import voice_id
    everyone = msg.get("all") is True
    if not everyone and not msg.get("person"):
        _fail(connection, msg, "bad_request", voice_id.ERROR_MESSAGES["bad_request"])
        return
    _entry, uri, token = await _app(hass)
    if not uri:
        _fail(connection, msg, "unreachable", voice_id.ERROR_MESSAGES["unreachable"])
        return
    try:
        reply = await voice_id.profile_delete(uri, token, msg.get("person"), everyone=everyone,
                                              source=msg.get("source"))
    except voice_id.VoiceIdManagementError as exc:
        _fail(connection, msg, exc.code, exc.message)
        return
    _LOGGER.info("Nova Voice ID: deleted %s%s", "all voice data" if everyone
                 else f"{len(reply['deleted'])} voice profile(s)",
                 " (learned samples only)" if msg.get("source") else "")
    connection.send_result(msg["id"], {"ok": True, **reply})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_id_token_get",
})
@websocket_api.async_response
async def ws_voice_id_token_get(hass: HomeAssistant, connection, msg: dict) -> None:
    """The token, for the panel's eye icon only."""
    from . import voice_id_store
    connection.send_result(msg["id"], {"ok": True,
                                       "token": await voice_id_store.async_get_token(hass)})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_id_token_set",
    vol.Required("token"): str,
})
@websocket_api.async_response
async def ws_voice_id_token_set(hass: HomeAssistant, connection, msg: dict) -> None:
    """Save the token the user typed (or clear it with ""). Never logged."""
    from . import voice_id_store
    token = msg["token"].strip()
    if token and not voice_id_store.valid_token(token):
        _fail(connection, msg, "bad_token",
              "That doesn't look like the app's token. Copy it from the app's Configuration tab.")
        return
    if not await voice_id_store.async_set_token(hass, token):
        _fail(connection, msg, "save_failed", "Nova couldn't save the token.")
        return
    connection.send_result(msg["id"], {"ok": True, "set": bool(token)})
