"""Panel commands for the Memory tab and the lockdown switch.

Facts (nova/get_knowledge, add_knowledge, forget_knowledge,
pending_fact_action, edit_pending_fact), links between things
(nova/list_relations, relation_action, edit_relation), scene memory
(nova/clear_scene_memory), conversation search (nova/search_memory) and
nova/set_lockdown. websocket.py imports each handler by name to register it,
and the logger keeps websocket.py's name.

Every list of facts sent back holds confirmed facts only; pending ones go
in their own list for the review queue, so the panel never shows an
unconfirmed fact as a trusted one.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .safe_errors import safe_error_message

_LOGGER = logging.getLogger(f"{__package__}.websocket")


def _failure(what: str, exc: Exception) -> str:
    """Log a failed command with its traceback; the text safe to send back."""
    _LOGGER.exception("%s failed: %s", what, exc)
    return safe_error_message(exc)


async def _run(hass: HomeAssistant, job, *args):
    return await hass.async_add_executor_job(job, *args)


def _trusted(knowledge, **scope) -> list:
    """The confirmed facts, the only ones the Memory list shows as known."""
    return knowledge.all_facts(status="confirmed", **scope)


# ── lockdown ────────────────────────────────────────────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/set_lockdown", vol.Required("on"): bool,
})
@websocket_api.async_response
async def ws_set_lockdown(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                          msg: dict[str, Any]) -> None:
    """Turn the formal lockdown on or off from the panel."""
    on = bool(msg["on"])
    try:
        from . import cognitive_core
        ok = await cognitive_core.request_lockdown(on, reason="requested from panel", hass=hass)
        state = cognitive_core.lockdown_status()
        if not ok:
            _LOGGER.warning("Panel lockdown request returned not-ok (on=%s); status=%s",
                            on, state)
        connection.send_result(msg["id"], {"ok": ok, "lockdown": state})
    except Exception as exc:
        _LOGGER.exception("Panel lockdown request failed: %s", exc)
        connection.send_error(msg["id"], "lockdown_failed", safe_error_message(exc))


# ── facts ───────────────────────────────────────────────────────────────────

@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_knowledge", vol.Optional("subject"): str,
})
@websocket_api.async_response
async def ws_get_knowledge(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                           msg: dict[str, Any]) -> None:
    """Everything the Memory tab shows about facts: the confirmed ones, the
    pending ones awaiting review (confirm, reject or edit), and the counts."""
    try:
        from . import knowledge
        subject = msg.get("subject")
        facts = await _run(hass, lambda: _trusted(knowledge, subject=subject))
        pending = await _run(hass, lambda: knowledge.pending_facts(subject=subject))
        counts = await _run(hass, knowledge.stats)
        connection.send_result(msg["id"], {"facts": facts, "pending": pending, "stats": counts})
    except Exception as exc:
        connection.send_error(msg["id"], "knowledge_failed", _failure("get_knowledge", exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/add_knowledge",
    vol.Required("key"): str, vol.Required("value"): str,
    vol.Optional("subject"): str, vol.Optional("kind"): str,
})
@websocket_api.async_response
async def ws_add_knowledge(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                           msg: dict[str, Any]) -> None:
    """Teach Nova a fact from the Memory tab."""
    try:
        from . import knowledge
        stored = await _run(hass, lambda: knowledge.remember(
            msg["key"], msg["value"],
            subject=msg.get("subject", knowledge.DEFAULT_SUBJECT),
            kind=msg.get("kind", "fact"), source="stated"))
        facts = await _run(hass, _trusted, knowledge)
        connection.send_result(msg["id"], {"ok": bool(stored), "facts": facts})
    except Exception as exc:
        connection.send_error(msg["id"], "add_failed", _failure("add_knowledge", exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/clear_scene_memory",
})
@websocket_api.async_response
async def ws_clear_scene_memory(hass: HomeAssistant,
                                connection: websocket_api.ActiveConnection,
                                msg: dict[str, Any]) -> None:
    """Forget every camera description scene memory has kept."""
    try:
        from . import scene_memory
        removed = await _run(hass, scene_memory.forget_all)
        counts = await _run(hass, scene_memory.stats)
        connection.send_result(msg["id"], {"removed": removed, "stats": counts})
    except Exception as exc:
        connection.send_error(msg["id"], "clear_failed", _failure("clear_scene_memory", exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/forget_knowledge",
    vol.Optional("fact_id"): int,
    vol.Optional("subject"): str, vol.Optional("key"): str,
})
@websocket_api.async_response
async def ws_forget_knowledge(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                              msg: dict[str, Any]) -> None:
    """Forget a fact, by ``fact_id`` or by subject and key.

    The id travels as ``fact_id``: ``id`` belongs to the websocket protocol
    (the frontend overwrites it with the message number), and using it once
    meant nothing was ever deleted.
    """
    try:
        from . import knowledge
        removed = await _run(hass, lambda: knowledge.forget(
            fact_id=msg.get("fact_id"), subject=msg.get("subject"), key=msg.get("key")))
        facts = await _run(hass, _trusted, knowledge)
        connection.send_result(msg["id"], {"removed": removed, "facts": facts})
    except Exception as exc:
        connection.send_error(msg["id"], "forget_failed", _failure("forget_knowledge", exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/pending_fact_action",
    vol.Required("fact_id"): int,
    vol.Required("action"): vol.In(["confirm", "reject"]),
})
@websocket_api.async_response
async def ws_pending_fact_action(hass: HomeAssistant,
                                 connection: websocket_api.ActiveConnection,
                                 msg: dict[str, Any]) -> None:
    """Confirm or reject a fact the agent's remember tool left pending, from
    the review queue: the way to settle one not confirmed in the chat that
    proposed it."""
    try:
        from . import knowledge
        fact_id = msg["fact_id"]
        if msg["action"] == "confirm":
            ok = await _run(hass, knowledge.confirm_fact, fact_id)
        else:
            ok = bool(await _run(hass, lambda: knowledge.forget(fact_id=fact_id)))
        facts = await _run(hass, _trusted, knowledge)
        pending = await _run(hass, knowledge.pending_facts)
        connection.send_result(msg["id"], {"ok": ok, "facts": facts, "pending": pending})
    except Exception as exc:
        connection.send_error(msg["id"], "pending_fact_action_failed",
                              _failure("pending_fact_action", exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/edit_pending_fact",
    vol.Required("fact_id"): int, vol.Required("value"): str,
})
@websocket_api.async_response
async def ws_edit_pending_fact(hass: HomeAssistant,
                               connection: websocket_api.ActiveConnection,
                               msg: dict[str, Any]) -> None:
    """Correct a pending fact's value before it is confirmed."""
    try:
        from . import knowledge
        changed = await _run(hass, lambda: knowledge.edit_fact(msg["fact_id"], msg["value"]))
        pending = await _run(hass, knowledge.pending_facts)
        connection.send_result(msg["id"], {"ok": bool(changed), "pending": pending})
    except Exception as exc:
        connection.send_error(msg["id"], "edit_pending_fact_failed",
                              _failure("edit_pending_fact", exc))


# ── links between things ────────────────────────────────────────────────────

def _relation_payload(knowledge) -> dict:
    """What the Memory tab shows of relations: pending ones to review,
    confirmed ones with a Remove button, the counts and the cap."""
    return {
        "pending": knowledge.list_relations(status="pending"),
        "confirmed": knowledge.list_relations(status="confirmed"),
        "counts": knowledge.relation_counts(),
        "cap": knowledge.RELATION_LIVE_CAP,
    }


@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_relations",
})
@websocket_api.async_response
async def ws_list_relations(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                            msg: dict[str, Any]) -> None:
    """The stored links between things, pending and confirmed. A read, open
    to any signed in user, as nova/get_knowledge is."""
    try:
        from . import knowledge
        connection.send_result(msg["id"], await _run(hass, _relation_payload, knowledge))
    except Exception as exc:
        connection.send_error(msg["id"], "relations_failed", _failure("list_relations", exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/relation_action",
    vol.Required("relation_id"): int,
    vol.Required("action"): vol.In(["confirm", "reject", "remove"]),
})
@websocket_api.async_response
async def ws_relation_action(hass: HomeAssistant,
                             connection: websocket_api.ActiveConnection,
                             msg: dict[str, Any]) -> None:
    """Confirm a pending link, reject one, or remove a confirmed one. Reject
    and remove keep the row as deleted, so the same link proposed again
    stays gone."""
    try:
        from . import knowledge
        rid, action = msg["relation_id"], msg["action"]
        if action == "confirm":
            ok = await _run(hass, knowledge.confirm_relation, rid)
        elif action == "reject":
            ok = await _run(hass, lambda: knowledge.remove_relation(rid, only_pending=True))
        else:
            ok = await _run(hass, knowledge.remove_relation, rid)
        payload = await _run(hass, _relation_payload, knowledge)
        connection.send_result(msg["id"], {"ok": bool(ok), **payload})
    except Exception as exc:
        connection.send_error(msg["id"], "relation_action_failed",
                              _failure("relation_action", exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/edit_relation",
    vol.Required("relation_id"): int,
    vol.Optional("subject"): str, vol.Optional("predicate"): str, vol.Optional("object"): str,
})
@websocket_api.async_response
async def ws_edit_relation(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                           msg: dict[str, Any]) -> None:
    """Correct a pending link before confirming it. A value that fails the
    checks comes back as ok false with an error code, and nothing changes."""
    try:
        from . import knowledge
        outcome = await _run(hass, lambda: knowledge.edit_relation(
            msg["relation_id"], msg.get("subject"), msg.get("predicate"), msg.get("object")))
        payload = await _run(hass, _relation_payload, knowledge)
        connection.send_result(msg["id"], {"ok": outcome["ok"], "error": outcome["error"],
                                           **payload})
    except Exception as exc:
        connection.send_error(msg["id"], "edit_relation_failed", _failure("edit_relation", exc))


# ── conversation search ─────────────────────────────────────────────────────

@websocket_api.websocket_command({
    vol.Required("type"): "nova/search_memory",
    vol.Required("query"): str,
    vol.Optional("k", default=5): int,
})
@websocket_api.async_response
async def ws_search_memory(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                           msg: dict[str, Any]) -> None:
    """Past conversations that match the query."""
    try:
        from .memory import search_memory
        found = await _run(hass, lambda: search_memory(msg["query"], k=msg["k"]))
        connection.send_result(msg["id"], {"results": found})
    except Exception as exc:
        _LOGGER.warning("ws_search_memory failed: %s", exc)
        connection.send_error(msg["id"], "search_failed", safe_error_message(exc))
