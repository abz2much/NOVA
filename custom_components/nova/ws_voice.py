"""The panel's voice and history commands.

nova/get_spoken_history, nova/list_actions, nova/repeat_spoken,
nova/voice_confirm_test and nova/say_hello. They were moved out of
websocket.py unchanged, and the logger keeps the name it had there.

websocket.py imports each handler by name, and async_register registers them.
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
    vol.Required("type"): "nova/get_spoken_history",
})
@websocket_api.async_response
async def ws_get_spoken_history(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                                msg: dict) -> None:
    """Spoken History (v7.104.0): the last things Nova really sent to a
    speaker. That covers welcome-home lines, reminders, alerts, briefings,
    manual tests, confirmed Assist replies and repeats. It is text only,
    newest first, limited to the last 100. Admin only, because it shows what
    was said in the house, which the panel's other plain read commands do not."""
    try:
        from . import spoken_history
        recent = await hass.async_add_executor_job(spoken_history.list_recent)
        connection.send_result(msg["id"], {"entries": recent})
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
async def ws_list_actions(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                          msg: dict) -> None:
    """Action Audit Log: what Nova really attempted or did on the user's
    behalf. That is device controls, bulk controls, running scenes, scripts
    and automations, safety routines, installing suggested automations and
    notifications. The log is paged by request with a keyset, so a page
    holds whole requests and never half of one, newest first. Admin only,
    since it shows what Nova actually did in the house, at the same level as
    Spoken History.

    Each request returned has its own overall `status` (success, partial,
    failed, blocked or awaiting), separate from the approval_result and
    execution_result of each target row. When a Spoken History entry exists for
    its request_id, it also has a `spoken_history_id` reference. The spoken
    text is never included, and the panel fetches it on its own with
    nova/get_spoken_history if it wants to show it."""
    try:
        from . import action_log
        page_size = max(1, min(int(msg.get("limit", 20)), 100))

        def read_page():
            return action_log.page_requests(
                limit=page_size,
                cursor_ts=msg.get("cursor_ts"),
                cursor_request_id=msg.get("cursor_request_id"),
            )

        page = await hass.async_add_executor_job(read_page)
        wanted = [req["request_id"] for req in page["requests"]]
        links: dict[str, int] = {}
        if wanted:
            try:
                from . import spoken_history
                links = await hass.async_add_executor_job(
                    spoken_history.find_by_action_request_id, wanted)
            except Exception:
                # Never let a Spoken History problem break the actions list.
                pass
        for req in page["requests"]:
            req["spoken_history_id"] = links.get(req["request_id"])
        connection.send_result(msg["id"], page)
    except Exception as exc:
        _LOGGER.exception("ws_list_actions failed: %s", exc)
        connection.send_error(msg["id"], "list_actions_failed", safe_error_message(exc))


def _speakers_still_there(hass: HomeAssistant, speakers) -> list:
    """The listed speakers that Home Assistant can currently reach."""
    ready = []
    for speaker in speakers:
        state = hass.states.get(speaker)
        if state is not None and state.state not in ("unavailable", "unknown"):
            ready.append(speaker)
    return ready


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/repeat_spoken",
    vol.Required("spoken_id"): int,
})
@websocket_api.async_response
async def ws_repeat_spoken(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                           msg: dict) -> None:
    """Say one Spoken History entry again (the panel's Repeat button).
    `spoken_id` picks the row. It is never called `id`, which Home Assistant
    reserves for matching a reply to its message.

    It goes only to the speakers it was first said on, and only those that are
    still available. If none is (8.7.23) the request is refused with
    no_speaker. It is not said again on the house-wide default speakers, which
    may be in another room from whoever heard it. Both delivery and recording
    happen inside async_announce. This handler never calls
    spoken_history.record, so a repeat is recorded exactly once, by the same
    single recorder as every other path through async_announce."""
    try:
        from . import spoken_history, nova_config
        from .tts_helper import async_announce, resolve_tts_entity

        row = await hass.async_add_executor_job(spoken_history.get, msg["spoken_id"])
        if row is None:
            connection.send_error(msg["id"], "not_found", "No spoken history entry with that id")
            return

        speakers = _speakers_still_there(hass, row["speakers"])
        if not speakers:
            connection.send_error(
                msg["id"], "no_speaker",
                "The speaker this was said on is not available, so it was not repeated")
            return
        entry = _get_entry(hass)
        settings = await hass.async_add_executor_job(nova_config.effective_config, entry)

        voice = resolve_tts_entity(hass, settings.get("tts_engine", "auto"))
        if not voice:
            connection.send_error(msg["id"], "no_tts_entity", "No TTS entity available")
            return

        spoken = await async_announce(
            hass, row["text"], voice, speakers,
            context="repeat", repeat_of_id=row["id"],
        )
        connection.send_result(msg["id"], {"ok": spoken, "spoken": row["text"] if spoken else ""})
    except Exception as exc:
        _LOGGER.exception("ws_repeat_spoken failed: %s", exc)
        connection.send_error(msg["id"], "repeat_spoken_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_confirm_test",
})
@websocket_api.async_response
async def ws_voice_confirm_test(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                                msg: dict) -> None:
    """Fire the assist_satellite.announce test (v6.67.0). It answers "does it
    come out of the Nest?", which decides whether native voice confirmation
    works. The user listens and chooses native or gated by whether they heard
    it."""
    try:
        from . import voice_confirm
        outcome = await voice_confirm.announce_test(hass)
        connection.send_result(msg["id"], outcome)
    except Exception as exc:
        _LOGGER.exception("ws_voice_confirm_test failed: %s", exc)
        connection.send_error(msg["id"], "test_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/say_hello",
})
@websocket_api.async_response
async def ws_say_hello(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                       msg: dict) -> None:
    """The welcome card's "Say hello" test. It sends the fixed text "Hello"
    through Nova's own conversation agent and returns {ok, reply|error}. Admin
    only. It takes no text from the caller, so it cannot drive a device action.

    It never raises (8.7.23). A failure answers with the same {ok: False,
    error} shape that welcome.async_say_hello returns, with safe text, and the
    detail goes to the Home Assistant log. The pinned contract has no error
    code for this command, so there is no send_error."""
    try:
        from . import welcome
        outcome = await welcome.async_say_hello(hass, connection.context(msg))
    except Exception as exc:
        outcome = {"ok": False,
                   "error": safe_error_message(exc, where="say_hello", log=True)}
    connection.send_result(msg["id"], outcome)
