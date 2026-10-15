"""Panel commands for suggestions, automation trials, goals and routines.

nova/suggestion_action, nova/list_automation_inventory,
nova/list_automation_trials, nova/automation_trial_feedback, nova/goal_action
and nova/get_person_routines. websocket.py imports each handler by name to
register it, and the logger keeps websocket.py's name.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .safe_errors import safe_error_message
from .ws_log import nova_log
from .ws_panel_stats import _entity_names, _get_goals, _get_person_routines

_LOGGER = logging.getLogger(f"{__package__}.websocket")


def _failure(handler: str, exc: Exception) -> str:
    """Log a failed command with its traceback; the text safe to send back."""
    _LOGGER.exception("%s failed: %s", handler, exc)
    return safe_error_message(exc)


def _who(connection) -> dict:
    user = connection.user
    return {"requested_by_user_id": getattr(user, "id", None),
            "requested_by_name": getattr(user, "name", None)}


# ── suggestions ─────────────────────────────────────────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/suggestion_action",
    vol.Required("suggestion_id"): int,
    vol.Required("action"): vol.In(["approve", "dismiss", "restore"]),
})
@websocket_api.async_response
async def ws_suggestion_action(hass: HomeAssistant,
                               connection: websocket_api.ActiveConnection,
                               msg: dict) -> None:
    """Approve (and install), dismiss, or restore a suggestion the AI review
    turned down."""
    try:
        from .automation.installation import install_approved_suggestion
        from .automation.patterns import get_analyzer
        analyzer = get_analyzer()
        sid, action = int(msg["suggestion_id"]), msg["action"]
        if action == "approve":
            # Approving installs the automation in Home Assistant.
            done = await install_approved_suggestion(hass, sid, **_who(connection))
            if done.get("installed"):
                nova_log("LEARN", f"Suggestion #{sid} approved & installed as "
                                  f"'{done.get('alias')}'")
            elif done.get("ok"):
                nova_log("LEARN", f"Suggestion #{sid} approved (advisory — "
                                  f"{done.get('reason')})")
            connection.send_result(msg["id"], {
                "ok": bool(done.get("ok")), "installed": bool(done.get("installed")),
                "reason": done.get("reason"), "alias": done.get("alias"),
            })
            return
        if action == "restore":
            ok = await hass.async_add_executor_job(analyzer.restore_suggestion, sid)
            nova_log("LEARN", f"Suggestion #{sid} restored after AI review (ok={ok})")
        else:
            ok = await hass.async_add_executor_job(analyzer.dismiss_suggestion, sid)
            nova_log("LEARN", f"Suggestion #{sid} dismissed (ok={ok})")
        connection.send_result(msg["id"], {"ok": bool(ok)})
    except Exception as exc:
        connection.send_error(msg["id"], "suggestion_action_failed",
                              _failure("ws_suggestion_action", exc))


# ── automations already in Home Assistant ───────────────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_automation_inventory",
})
@websocket_api.async_response
async def ws_list_automation_inventory(hass: HomeAssistant,
                                       connection: websocket_api.ActiveConnection,
                                       msg: dict) -> None:
    """Nova's read only list of Home Assistant's automations, from the cache
    made at startup or reload, so opening Suggestions scans nothing. No raw
    automation configuration ever leaves the backend."""
    try:
        from .automation.inventory import get_inventory
        inventory = get_inventory(hass)
        connection.send_result(msg["id"], {
            "available": inventory is not None,
            "refreshed_at": inventory.refreshed_at if inventory else None,
            "automations": inventory.public_items() if inventory else [],
        })
    except Exception as exc:
        connection.send_error(msg["id"], "list_automation_inventory_failed",
                              _failure("ws_list_automation_inventory", exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_automation_trials",
})
@websocket_api.async_response
async def ws_list_automation_trials(hass: HomeAssistant,
                                    connection: websocket_api.ActiveConnection,
                                    msg: dict) -> None:
    """How often each installed suggestion has run, with the household's
    feedback. Installed only means accepted: this reports what was seen
    running, never that it works."""
    try:
        from .automation import trials
        connection.send_result(msg["id"], {
            "trials": await hass.async_add_executor_job(trials.list_trials)})
    except Exception as exc:
        connection.send_error(msg["id"], "list_automation_trials_failed",
                              _failure("ws_list_automation_trials", exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/automation_trial_feedback",
    vol.Required("trial_id"): int,
    vol.Required("verdict"): vol.In(["working", "needs_adjustment"]),
})
@websocket_api.async_response
async def ws_automation_trial_feedback(hass: HomeAssistant,
                                       connection: websocket_api.ActiveConnection,
                                       msg: dict) -> None:
    """The household's own Working or Needs adjustment verdict; never
    guessed."""
    try:
        from .automation import trials
        ok = await hass.async_add_executor_job(
            trials.set_manual_outcome, msg["trial_id"], msg["verdict"])
        connection.send_result(msg["id"], {"ok": bool(ok)})
    except Exception as exc:
        connection.send_error(msg["id"], "automation_trial_feedback_failed",
                              _failure("ws_automation_trial_feedback", exc))


# ── goals and routines ──────────────────────────────────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/goal_action",
    vol.Required("action"): vol.In(["cancel", "delete", "create"]),
    vol.Optional("goal_id"): int,
    vol.Optional("title"): str, vol.Optional("outcome"): str,
    vol.Optional("interval_min"): vol.Coerce(float),
})
@websocket_api.async_response
async def ws_goal_action(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                         msg: dict) -> None:
    """Goals from the panel: create one, cancel one (it stays in the
    history) or delete one (it leaves the list). Goals still close
    themselves through the headless runner too."""
    action = msg["action"]
    try:
        from . import goals
        if action == "create":
            title = str(msg.get("title", "") or "").strip()
            outcome = str(msg.get("outcome", "") or "").strip()
            if not outcome:
                connection.send_error(msg["id"], "empty_outcome",
                                      "a goal needs an outcome to work toward")
                return
            extra = {}
            if msg.get("interval_min") is not None:
                extra["check_interval_min"] = float(msg["interval_min"])
            made = await hass.async_add_executor_job(
                lambda: goals.create(title, outcome, **extra))
            if made.get("error"):
                connection.send_error(msg["id"], "create_failed", made["error"])
                return
            nova_log("LEARN", f"Goal created from panel: {title or outcome[:50]}")
            connection.send_result(msg["id"], {"ok": True, "goal": made, "goals": _get_goals()})
            return
        if msg.get("goal_id") is None:
            connection.send_error(msg["id"], "missing_goal_id", f"{action} needs a goal_id")
            return
        gid = int(msg["goal_id"])
        if action == "delete":
            ok = await hass.async_add_executor_job(goals.delete, gid)
            nova_log("LEARN", f"Goal #{gid} deleted from panel (ok={ok})")
        else:
            ok = await hass.async_add_executor_job(goals.cancel, gid)
            nova_log("LEARN", f"Goal #{gid} cancelled from panel (ok={ok})")
        connection.send_result(msg["id"], {"ok": bool(ok), "goals": _get_goals()})
    except Exception as exc:
        connection.send_error(msg["id"], "goal_action_failed", _failure("ws_goal_action", exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_person_routines",
})
@websocket_api.async_response
async def ws_get_person_routines(hass: HomeAssistant,
                                 connection: websocket_api.ActiveConnection,
                                 msg: dict[str, Any]) -> None:
    """Each person's learned routines, for the Memory tab."""
    try:
        routines = await hass.async_add_executor_job(_get_person_routines, _entity_names(hass))
        connection.send_result(msg["id"], {"routines": routines})
    except Exception as exc:
        connection.send_error(msg["id"], "person_routines_failed",
                              _failure("get_person_routines", exc))
