"""The knowledge, memory and lockdown commands.

Moved verbatim out of websocket.py: nova/get_knowledge, nova/add_knowledge,
nova/forget_knowledge, nova/pending_fact_action, nova/edit_pending_fact,
nova/clear_scene_memory, nova/search_memory and nova/set_lockdown. The logger
keeps the name it had in websocket.py.

websocket.py imports every handler by name (async_register registers them).
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
    vol.Required("type"): "nova/set_lockdown",
    vol.Required("on"): bool,
})
@websocket_api.async_response
async def ws_set_lockdown(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Engage or lift the formal lockdown from the panel."""
    try:
        from . import cognitive_core
        ok = await cognitive_core.request_lockdown(
            bool(msg["on"]), reason="requested from panel", hass=hass)
        status = cognitive_core.lockdown_status()
        if not ok:
            _LOGGER.warning("Panel lockdown request returned not-ok (on=%s); status=%s",
                            bool(msg["on"]), status)
        connection.send_result(msg["id"], {"ok": ok, "lockdown": status})
    except Exception as exc:
        _LOGGER.exception("Panel lockdown request failed: %s", exc)
        connection.send_error(msg["id"], "lockdown_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_knowledge",
    vol.Optional("subject"): str,
})
@websocket_api.async_response
async def ws_get_knowledge(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the curated facts Nova knows, for the Memory panel.

    `facts` is confirmed-only (v7.88.0) -- a fact agent.py's `remember` tool
    staged as pending must not appear here as if it were already established;
    `pending` carries those separately so the panel can show a distinct
    review queue (confirm / reject / edit) instead of silently merging them
    into the trusted list.
    """
    try:
        from . import knowledge
        subject = msg.get("subject")
        facts = await hass.async_add_executor_job(
            lambda: knowledge.all_facts(subject=subject, status="confirmed"))
        pending = await hass.async_add_executor_job(
            lambda: knowledge.pending_facts(subject=subject))
        kstats = await hass.async_add_executor_job(knowledge.stats)
        connection.send_result(msg["id"], {"facts": facts, "pending": pending, "stats": kstats})
    except Exception as exc:
        _LOGGER.exception("get_knowledge failed: %s", exc)
        connection.send_error(msg["id"], "knowledge_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/add_knowledge",
    vol.Required("key"): str,
    vol.Required("value"): str,
    vol.Optional("subject"): str,
    vol.Optional("kind"): str,
})
@websocket_api.async_response
async def ws_add_knowledge(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Teach Nova a fact from the Memory panel."""
    try:
        from . import knowledge
        f = await hass.async_add_executor_job(
            lambda: knowledge.remember(
                msg["key"], msg["value"],
                subject=msg.get("subject", knowledge.DEFAULT_SUBJECT),
                kind=msg.get("kind", "fact"), source="stated"))
        facts = await hass.async_add_executor_job(knowledge.all_facts)
        connection.send_result(msg["id"], {"ok": bool(f), "facts": facts})
    except Exception as exc:
        _LOGGER.exception("add_knowledge failed: %s", exc)
        connection.send_error(msg["id"], "add_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/clear_scene_memory",
})
@websocket_api.async_response
async def ws_clear_scene_memory(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Forget every camera description scene memory has kept."""
    try:
        from . import scene_memory
        removed = await hass.async_add_executor_job(scene_memory.forget_all)
        stats = await hass.async_add_executor_job(scene_memory.stats)
        connection.send_result(msg["id"], {"removed": removed, "stats": stats})
    except Exception as exc:
        _LOGGER.exception("clear_scene_memory failed: %s", exc)
        connection.send_error(msg["id"], "clear_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/forget_knowledge",
    vol.Optional("fact_id"): int,
    vol.Optional("subject"): str,
    vol.Optional("key"): str,
})
@websocket_api.async_response
async def ws_forget_knowledge(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Forget a fact (by fact_id, or subject+key) from the Memory panel.

    NOTE: the fact id is carried as ``fact_id``, not ``id`` — ``id`` is reserved
    by the HA WebSocket protocol for the message sequence number (the frontend
    overwrites any ``id`` we send), so using it here silently deleted nothing.
    """
    try:
        from . import knowledge
        fid = msg.get("fact_id")
        removed = await hass.async_add_executor_job(
            lambda: knowledge.forget(fact_id=fid, subject=msg.get("subject"), key=msg.get("key")))
        facts = await hass.async_add_executor_job(knowledge.all_facts)
        connection.send_result(msg["id"], {"removed": removed, "facts": facts})
    except Exception as exc:
        _LOGGER.exception("forget_knowledge failed: %s", exc)
        connection.send_error(msg["id"], "forget_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/pending_fact_action",
    vol.Required("fact_id"): int,
    vol.Required("action"): vol.In(["confirm", "reject"]),
})
@websocket_api.async_response
async def ws_pending_fact_action(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Confirm or reject a fact agent.py's `remember` tool staged as pending
    (v7.88.0), from the Memory panel's review queue -- the fallback for when
    the user didn't (or couldn't) confirm it inline in the conversation that
    proposed it."""
    try:
        from . import knowledge
        fid = msg["fact_id"]
        if msg["action"] == "confirm":
            ok = await hass.async_add_executor_job(knowledge.confirm_fact, fid)
        else:
            ok = bool(await hass.async_add_executor_job(lambda: knowledge.forget(fact_id=fid)))
        facts = await hass.async_add_executor_job(lambda: knowledge.all_facts(status="confirmed"))
        pending = await hass.async_add_executor_job(knowledge.pending_facts)
        connection.send_result(msg["id"], {"ok": ok, "facts": facts, "pending": pending})
    except Exception as exc:
        _LOGGER.exception("pending_fact_action failed: %s", exc)
        connection.send_error(msg["id"], "pending_fact_action_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/edit_pending_fact",
    vol.Required("fact_id"): int,
    vol.Required("value"): str,
})
@websocket_api.async_response
async def ws_edit_pending_fact(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Correct a pending fact's value before confirming it (v7.88.0) — the
    one capability the Memory panel didn't have for any fact before this."""
    try:
        from . import knowledge
        updated = await hass.async_add_executor_job(
            lambda: knowledge.edit_fact(msg["fact_id"], msg["value"]))
        pending = await hass.async_add_executor_job(knowledge.pending_facts)
        connection.send_result(msg["id"], {"ok": bool(updated), "pending": pending})
    except Exception as exc:
        _LOGGER.exception("edit_pending_fact failed: %s", exc)
        connection.send_error(msg["id"], "edit_pending_fact_failed", safe_error_message(exc))


def _relation_payload(knowledge) -> dict:
    """The relations the Memory tab shows: pending ones for review, confirmed
    ones with a Remove button, and counts."""
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
async def ws_list_relations(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """The links between things Nova has stored (8.7.13), for the Memory tab:
    pending ones awaiting a person, and confirmed ones. A read, open to any
    signed in user like nova/get_knowledge, which already lists pending facts."""
    try:
        from . import knowledge
        payload = await hass.async_add_executor_job(_relation_payload, knowledge)
        connection.send_result(msg["id"], payload)
    except Exception as exc:
        _LOGGER.exception("list_relations failed: %s", exc)
        connection.send_error(msg["id"], "relations_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/relation_action",
    vol.Required("relation_id"): int,
    vol.Required("action"): vol.In(["confirm", "reject", "remove"]),
})
@websocket_api.async_response
async def ws_relation_action(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Confirm or reject a pending relation, or remove a confirmed one. A
    confirm only works on a pending relation. Reject and remove are soft
    deletes: the edge stays removed even if something proposes it again."""
    try:
        from . import knowledge
        rid = msg["relation_id"]
        if msg["action"] == "confirm":
            ok = await hass.async_add_executor_job(knowledge.confirm_relation, rid)
        elif msg["action"] == "reject":
            ok = await hass.async_add_executor_job(
                lambda: knowledge.remove_relation(rid, only_pending=True))
        else:
            ok = await hass.async_add_executor_job(knowledge.remove_relation, rid)
        payload = await hass.async_add_executor_job(_relation_payload, knowledge)
        connection.send_result(msg["id"], {"ok": bool(ok), **payload})
    except Exception as exc:
        _LOGGER.exception("relation_action failed: %s", exc)
        connection.send_error(msg["id"], "relation_action_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/edit_relation",
    vol.Required("relation_id"): int,
    vol.Optional("subject"): str,
    vol.Optional("predicate"): str,
    vol.Optional("object"): str,
})
@websocket_api.async_response
async def ws_edit_relation(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Correct a pending relation before confirming it. A value that fails
    validation is answered with ok false and an error code, so the panel can
    say what to fix; the relation is not changed."""
    try:
        from . import knowledge
        res = await hass.async_add_executor_job(
            lambda: knowledge.edit_relation(
                msg["relation_id"], msg.get("subject"), msg.get("predicate"),
                msg.get("object")))
        payload = await hass.async_add_executor_job(_relation_payload, knowledge)
        connection.send_result(msg["id"], {"ok": res["ok"], "error": res["error"], **payload})
    except Exception as exc:
        _LOGGER.exception("edit_relation failed: %s", exc)
        connection.send_error(msg["id"], "edit_relation_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/search_memory",
    vol.Required("query"): str,
    vol.Optional("k", default=5): int,
})
@websocket_api.async_response
async def ws_search_memory(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Search long-term memory for relevant past conversations."""
    try:
        from .memory import search_memory
        results = await hass.async_add_executor_job(
            lambda: search_memory(msg["query"], k=msg["k"])
        )
        connection.send_result(msg["id"], {"results": results})
    except Exception as exc:
        _LOGGER.warning("ws_search_memory failed: %s", exc)
        connection.send_error(msg["id"], "search_failed", safe_error_message(exc))
