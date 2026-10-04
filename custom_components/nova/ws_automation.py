"""The suggestion, automation trial, goal and routine commands.

Moved verbatim out of websocket.py: nova/suggestion_action,
nova/list_automation_inventory, nova/list_automation_trials,
nova/automation_trial_feedback, nova/goal_action and nova/get_person_routines.
The logger keeps the name it had in websocket.py.

websocket.py imports every handler by name (async_register registers them).
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


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/suggestion_action",
    vol.Required("suggestion_id"): int,
    vol.Required("action"): vol.In(["approve", "dismiss", "restore"]),
})
@websocket_api.async_response
async def ws_suggestion_action(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Approve or dismiss a pattern-engine automation suggestion. Approval now
    installs the automation into HA, not just flags it (v6.52.0). "restore"
    brings back a suggestion the AI review rejected (v7.126.0)."""
    try:
        from .automation.installation import install_approved_suggestion
        from .automation.patterns import get_analyzer
        analyzer = get_analyzer()
        sid = int(msg["suggestion_id"])
        if msg["action"] == "approve":
            res = await install_approved_suggestion(
                hass, sid,
                requested_by_user_id=getattr(connection.user, "id", None),
                requested_by_name=getattr(connection.user, "name", None),
            )
            if res.get("installed"):
                nova_log("LEARN", f"Suggestion #{sid} approved & installed "
                                    f"as '{res.get('alias')}'")
            elif res.get("ok"):
                nova_log("LEARN", f"Suggestion #{sid} approved "
                                    f"(advisory — {res.get('reason')})")
            connection.send_result(msg["id"], {
                "ok": bool(res.get("ok")),
                "installed": bool(res.get("installed")),
                "reason": res.get("reason"),
                "alias": res.get("alias"),
            })
            return
        if msg["action"] == "restore":
            ok = await hass.async_add_executor_job(analyzer.restore_suggestion, sid)
            nova_log("LEARN", f"Suggestion #{sid} restored after AI review (ok={ok})")
            connection.send_result(msg["id"], {"ok": bool(ok)})
            return
        ok = await hass.async_add_executor_job(analyzer.dismiss_suggestion, sid)
        nova_log("LEARN", f"Suggestion #{sid} dismissed (ok={ok})")
        connection.send_result(msg["id"], {"ok": bool(ok)})
    except Exception as exc:
        _LOGGER.exception("ws_suggestion_action failed: %s", exc)
        connection.send_error(msg["id"], "suggestion_action_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_automation_inventory",
})
@websocket_api.async_response
async def ws_list_automation_inventory(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return Nova's cached, read-only Home Assistant automation inventory.

    Raw automation configuration never leaves the backend. This endpoint
    reads the startup/reload cache, so opening Suggestions adds no inventory
    scan to Home Assistant's normal dashboard polling.
    """
    try:
        from .automation.inventory import get_inventory
        inventory = get_inventory(hass)
        connection.send_result(msg["id"], {
            "available": inventory is not None,
            "refreshed_at": inventory.refreshed_at if inventory else None,
            "automations": inventory.public_items() if inventory else [],
        })
    except Exception as exc:
        _LOGGER.exception("ws_list_automation_inventory failed: %s", exc)
        connection.send_error(
            msg["id"], "list_automation_inventory_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_automation_trials",
})
@websocket_api.async_response
async def ws_list_automation_trials(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Installed-automation run counts + manual feedback for the Suggestions
    tab. Installation only means the suggestion was accepted — this reports
    what's actually observed running, never a claim that it works."""
    try:
        from .automation import trials as automation_trials
        trials = await hass.async_add_executor_job(automation_trials.list_trials)
        connection.send_result(msg["id"], {"trials": trials})
    except Exception as exc:
        _LOGGER.exception("ws_list_automation_trials failed: %s", exc)
        connection.send_error(msg["id"], "list_automation_trials_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/automation_trial_feedback",
    vol.Required("trial_id"): int,
    vol.Required("verdict"): vol.In(["working", "needs_adjustment"]),
})
@websocket_api.async_response
async def ws_automation_trial_feedback(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Manual Working / Needs adjustment feedback — never inferred, only ever
    what the household actually reports."""
    try:
        from .automation import trials as automation_trials
        ok = await hass.async_add_executor_job(
            automation_trials.set_manual_outcome, msg["trial_id"], msg["verdict"])
        connection.send_result(msg["id"], {"ok": bool(ok)})
    except Exception as exc:
        _LOGGER.exception("ws_automation_trial_feedback failed: %s", exc)
        connection.send_error(msg["id"], "automation_trial_feedback_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/goal_action",
    vol.Required("action"): vol.In(["cancel", "delete", "create"]),
    vol.Optional("goal_id"): int,
    vol.Optional("title"): str,
    vol.Optional("outcome"): str,
    vol.Optional("interval_min"): vol.Coerce(float),
})
@websocket_api.async_response
async def ws_goal_action(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Manage goals from the panel: create a new one, cancel an active one
    (keeps it in history), or delete one entirely (tidies the list). Goals also
    close themselves via the headless runner as before."""
    try:
        from . import goals
        action = msg["action"]
        if action == "create":
            outcome = str(msg.get("outcome", "") or "").strip()
            if not outcome:
                connection.send_error(msg["id"], "empty_outcome",
                                      "a goal needs an outcome to work toward")
                return
            title = str(msg.get("title", "") or "").strip()
            kwargs = {}
            if msg.get("interval_min") is not None:
                kwargs["check_interval_min"] = float(msg["interval_min"])
            res = await hass.async_add_executor_job(
                lambda: goals.create(title, outcome, **kwargs))
            if res.get("error"):
                connection.send_error(msg["id"], "create_failed", res["error"])
                return
            nova_log("LEARN", f"Goal created from panel: {title or outcome[:50]}")
            connection.send_result(msg["id"], {"ok": True, "goal": res,
                                               "goals": _get_goals()})
            return

        # cancel / delete both need a goal_id
        gid = msg.get("goal_id")
        if gid is None:
            connection.send_error(msg["id"], "missing_goal_id",
                                  f"{action} needs a goal_id")
            return
        gid = int(gid)
        if action == "delete":
            ok = await hass.async_add_executor_job(goals.delete, gid)
            nova_log("LEARN", f"Goal #{gid} deleted from panel (ok={ok})")
        else:  # cancel
            ok = await hass.async_add_executor_job(goals.cancel, gid)
            nova_log("LEARN", f"Goal #{gid} cancelled from panel (ok={ok})")
        connection.send_result(msg["id"], {"ok": bool(ok), "goals": _get_goals()})
    except Exception as exc:
        _LOGGER.exception("ws_goal_action failed: %s", exc)
        connection.send_error(msg["id"], "goal_action_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_person_routines",
})
@websocket_api.async_response
async def ws_get_person_routines(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Per-person learned routines, grouped by person, for the Memory panel."""
    try:
        routines = await hass.async_add_executor_job(
            _get_person_routines, _entity_names(hass))
        connection.send_result(msg["id"], {"routines": routines})
    except Exception as exc:
        _LOGGER.exception("get_person_routines failed: %s", exc)
        connection.send_error(msg["id"], "person_routines_failed", safe_error_message(exc))
