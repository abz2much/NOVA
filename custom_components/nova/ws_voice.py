"""The voice and history commands.

Moved verbatim out of websocket.py: nova/get_spoken_history,
nova/list_actions, nova/repeat_spoken, nova/voice_confirm_test and
nova/say_hello. The logger keeps the name it had in websocket.py.

websocket.py imports every handler by name (async_register registers them).
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .safe_errors import safe_error_message
from .ws_bridge import _get_entry

_LOGGER = logging.getLogger(f"{__package__}.websocket")


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_confirm_test",
})
@websocket_api.async_response
async def ws_voice_confirm_test(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Fire the assist_satellite.announce test (v6.67.0) — the 'does it come out
    the Nest?' check that decides whether native voice-confirm works. The user
    listens and picks native vs gated based on whether they heard it."""
    try:
        from . import voice_confirm
        res = await voice_confirm.announce_test(hass)
        connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_voice_confirm_test failed: %s", exc)
        connection.send_error(msg["id"], "test_failed", safe_error_message(exc))



@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/say_hello",
})
@websocket_api.async_response
async def ws_say_hello(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Welcome card "Say hello" test: sends the fixed text "Hello" through
    Nova's own conversation agent and returns {ok, reply|error}. Admin-only;
    takes no text from the caller, so it can't drive device actions."""
    from . import welcome
    res = await welcome.async_say_hello(hass, connection.context(msg))
    connection.send_result(msg["id"], res)



@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_spoken_history",
})
@websocket_api.async_response
async def ws_get_spoken_history(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Spoken History (v7.104.0): the last things Nova actually sent to a
    speaker — welcome-home, reminders, alerts, briefings, manual tests,
    confirmed Assist replies, and repeats. Text only, newest first, bounded
    to the last 100. Admin-only: this reveals what was actually said in the
    house, unlike the panel's other pure-read commands."""
    try:
        from . import spoken_history
        entries = await hass.async_add_executor_job(spoken_history.list_recent)
        connection.send_result(msg["id"], {"entries": entries})
    except Exception as exc:
        _LOGGER.exception("ws_get_spoken_history failed: %s", exc)
        connection.send_error(msg["id"], "get_spoken_history_failed", safe_error_message(exc))



@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_actions",
    vol.Optional("limit", default=20): int,
    vol.Optional("cursor_ts"): vol.Coerce(float),
    vol.Optional("cursor_request_id"): str,
})
@websocket_api.async_response
async def ws_list_actions(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Action Audit Log: actions Nova genuinely attempted or performed on
    the user's behalf — device controls, bulk controls, scene/script/
    automation execution, safety routines, suggested-automation
    installation, notifications. Request-level, keyset-paginated: one page
    is a set of COMPLETE request groups (never a request split across two
    pages), newest first. Admin-only: reveals what Nova actually did in
    the house, same tier as Spoken History.

    Each returned request carries its own aggregate `status` (success/
    partial/failed/blocked/awaiting) separate from each target row's own
    approval_result/execution_result, and — when a matching Spoken History
    entry exists for that request_id — a `spoken_history_id` reference
    (never the spoken text itself; the panel fetches that separately via
    the existing nova/get_spoken_history command if it wants to show it)."""
    try:
        from . import action_log
        limit = max(1, min(int(msg.get("limit", 20)), 100))
        result = await hass.async_add_executor_job(
            lambda: action_log.page_requests(
                limit=limit,
                cursor_ts=msg.get("cursor_ts"),
                cursor_request_id=msg.get("cursor_request_id"),
            )
        )
        request_ids = [r["request_id"] for r in result["requests"]]
        spoken_links: dict[str, int] = {}
        if request_ids:
            try:
                from . import spoken_history
                spoken_links = await hass.async_add_executor_job(
                    spoken_history.find_by_action_request_id, request_ids
                )
            except Exception:
                pass  # a Spoken History lookup failure must never break the actions list
        for r in result["requests"]:
            r["spoken_history_id"] = spoken_links.get(r["request_id"])
        connection.send_result(msg["id"], result)
    except Exception as exc:
        _LOGGER.exception("ws_list_actions failed: %s", exc)
        connection.send_error(msg["id"], "list_actions_failed", safe_error_message(exc))



@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/repeat_spoken",
    vol.Required("spoken_id"): int,
})
@websocket_api.async_response
async def ws_repeat_spoken(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Re-announce one Spoken History entry (the panel's Repeat button).
    `spoken_id` (never `id` — that field is reserved for websocket message
    correlation) names which row to repeat.

    Sends to the original speaker(s) if they are still available;
    otherwise falls back to Nova's configured default speakers
    (the same broadcast_target() the manual TTS test already uses).
    Delivery and recording both happen inside async_announce — this
    handler never calls spoken_history.record itself, so a repeat is
    recorded exactly once, by the same single recorder as every other
    path that goes through async_announce."""
    try:
        from . import spoken_history, nova_config
        from .tts_helper import async_announce, resolve_tts_entity
        from .audio_routing import broadcast_target

        row = await hass.async_add_executor_job(spoken_history.get, msg["spoken_id"])
        if row is None:
            connection.send_error(msg["id"], "not_found", "No spoken history entry with that id")
            return

        speakers = [
            s for s in row["speakers"]
            if (st := hass.states.get(s)) is not None
            and st.state not in ("unavailable", "unknown")
        ]
        entry = _get_entry(hass)
        cfg = await hass.async_add_executor_job(nova_config.effective_config, entry)
        if not speakers:
            speakers = broadcast_target(
                hass,
                broadcast_group=(cfg.get("broadcast_group") or None),
                announcement_speakers=cfg.get("announcement_speakers"),
            )
        if not speakers:
            connection.send_error(msg["id"], "no_speaker", "No speaker available to repeat through")
            return

        tts_entity = resolve_tts_entity(hass, cfg.get("tts_engine", "auto"))
        if not tts_entity:
            connection.send_error(msg["id"], "no_tts_entity", "No TTS entity available")
            return

        ok = await async_announce(
            hass, row["text"], tts_entity, speakers,
            context="repeat", repeat_of_id=row["id"],
        )
        connection.send_result(msg["id"], {"ok": ok, "spoken": row["text"] if ok else ""})
    except Exception as exc:
        _LOGGER.exception("ws_repeat_spoken failed: %s", exc)
        connection.send_error(msg["id"], "repeat_spoken_failed", safe_error_message(exc))
