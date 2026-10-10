"""The Chat tab's commands: nova/chat and nova/chat_new.

nova/chat sends one typed message through Nova's own conversation agent and
returns the reply. Any Home Assistant user may use it, as with Assist; there is
no admin gate. What keeps it safe:

* The conversation id is built here, as nova_chat_<user id> from the connection,
  and is never taken from the message. Each user has their own thread, and no
  user can read or write another's.
* The conversation agent treats a thread with that id like voice for unlock,
  open, disarm and standing down an intrusion: they need a tap on the user's
  own phone (see policy.chat_turn). Every other action passes policy.authorize
  as it does for Assist.
* One message per user in flight, and at most 20 a minute per user. Over the
  limit the command answers with a plain error and makes no agent call.

nova/chat_new clears the caller's thread: the agent's memory of it and its rows
in conversations.db.

websocket.py imports the handlers by name (async_register registers them).
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .safe_errors import safe_error_message

_LOGGER = logging.getLogger(f"{__package__}.websocket")

CHAT_MAX_CHARS = 1000
CHAT_MAX_PER_MINUTE = 20
_WINDOW_SECONDS = 60.0

RATE_LIMITED = "rate_limited"
RATE_LIMIT_TEXT = "You're sending too fast, wait a moment"

# user id -> send times inside the last minute; users with a message in flight.
_SENT: dict[str, deque] = defaultdict(deque)
_IN_FLIGHT: set[str] = set()


def _take_slot(user_id: str, now: float | None = None) -> bool:
    """Whether this user may send now. Records the send when they may."""
    now = time.monotonic() if now is None else now
    if user_id in _IN_FLIGHT:
        return False
    sent = _SENT[user_id]
    while sent and now - sent[0] >= _WINDOW_SECONDS:
        sent.popleft()
    if len(sent) >= CHAT_MAX_PER_MINUTE:
        return False
    sent.append(now)
    _IN_FLIGHT.add(user_id)
    return True


def _release(user_id: str) -> None:
    _IN_FLIGHT.discard(user_id)


async def async_chat(hass, context, text: str, conversation_id: str) -> dict:
    """Run one chat turn. Returns {ok, reply} or {ok: False, error}. Never
    raises."""
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
        return {"ok": True, "reply": speech}
    except Exception as exc:
        return {"ok": False,
                "error": safe_error_message(exc, where="chat", log=True)}


def _user_id(connection, msg) -> str:
    """The Home Assistant user behind this connection, or "" if none."""
    try:
        return str(getattr(connection.context(msg), "user_id", "") or "")
    except Exception:
        return ""


@websocket_api.websocket_command({
    vol.Required("type"): "nova/chat",
    vol.Required("text"): vol.All(str, vol.Length(min=1, max=CHAT_MAX_CHARS)),
})
@websocket_api.async_response
async def ws_chat(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Chat tab: send a typed message to Nova and return {ok, reply} or
    {ok: False, error}. Never raises; a failure answers with safe text and the
    detail goes to the Home Assistant log."""
    text = str(msg["text"]).strip()
    user_id = _user_id(connection, msg)
    if not text:
        connection.send_result(msg["id"], {"ok": False, "error": "Type a message first."})
        return
    if not user_id:
        connection.send_result(msg["id"], {"ok": False, "error": "Chat needs a signed in user."})
        return
    if not _take_slot(user_id):
        connection.send_result(msg["id"], {"ok": False, "code": RATE_LIMITED,
                                           "error": RATE_LIMIT_TEXT})
        return
    try:
        from . import policy
        res = await async_chat(hass, connection.context(msg), text,
                               policy.chat_conversation_id(user_id))
    finally:
        _release(user_id)
    connection.send_result(msg["id"], res)


@websocket_api.websocket_command({
    vol.Required("type"): "nova/chat_new",
})
@websocket_api.async_response
async def ws_chat_new(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """NEW CHAT: clear the caller's own thread, in memory and in
    conversations.db. Answers {ok, removed}. Never raises."""
    user_id = _user_id(connection, msg)
    if not user_id:
        connection.send_result(msg["id"], {"ok": False, "error": "Chat needs a signed in user."})
        return
    try:
        from . import database, policy
        cid = policy.chat_conversation_id(user_id)
        from . import conversation as nova_conversation
        nova_conversation.clear_chat_thread(cid)
        removed = await hass.async_add_executor_job(database.delete_conversation, cid)
        connection.send_result(msg["id"], {"ok": True, "removed": removed})
    except Exception as exc:
        connection.send_result(msg["id"], {
            "ok": False, "error": safe_error_message(exc, where="chat_new", log=True)})

