"""The Chat tab's command: nova/chat.

Sends one typed message through Nova's own conversation agent and returns the
reply. It goes through the same agent as Assist, so every action it takes
passes policy.authorize(): the command adds no path around the gate. The
caller's text is the only thing it forwards. It never passes a device_id, so
a typed message cannot pose as a voice satellite, and it is admin only
because the agent can act on the house.

websocket.py imports the handler by name (async_register registers it).
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .safe_errors import safe_error_message

_LOGGER = logging.getLogger(f"{__package__}.websocket")

CHAT_MAX_CHARS = 1000


async def async_chat(hass, context, text: str, conversation_id=None) -> dict:
    """Run one chat turn. Returns {ok, reply, conversation_id} or
    {ok: False, error}. Never raises."""
    try:
        from homeassistant.components import conversation
        from . import bootstrap
        agent = bootstrap._find_nova_agent(hass)
        if not agent:
            return {"ok": False, "error": "Nova's conversation agent was not found."}
        result = await conversation.async_converse(
            hass, text, conversation_id, context, agent_id=agent)
        resp = result.response.as_dict() if result and result.response else {}
        speech = ((resp.get("speech") or {}).get("plain") or {}).get("speech", "")
        if resp.get("response_type") == "error":
            return {"ok": False, "error": speech or "Nova returned an error."}
        if not speech:
            return {"ok": False, "error": "Nova replied with no text."}
        return {"ok": True, "reply": speech,
                "conversation_id": getattr(result, "conversation_id", None) or conversation_id}
    except Exception as exc:
        return {"ok": False,
                "error": safe_error_message(exc, where="chat", log=True)}


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/chat",
    vol.Required("text"): vol.All(str, vol.Length(min=1, max=CHAT_MAX_CHARS)),
    vol.Optional("conversation_id"): vol.Any(None, vol.All(str, vol.Length(max=64))),
})
@websocket_api.async_response
async def ws_chat(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Chat tab: send a typed message to Nova and return {ok, reply,
    conversation_id} or {ok: False, error}. Never raises; a failure answers
    with safe text and the detail goes to the Home Assistant log."""
    text = str(msg["text"]).strip()
    if not text:
        connection.send_result(msg["id"], {"ok": False, "error": "Type a message first."})
        return
    res = await async_chat(hass, connection.context(msg), text, msg.get("conversation_id"))
    connection.send_result(msg["id"], res)
