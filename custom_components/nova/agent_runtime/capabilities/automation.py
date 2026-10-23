"""Agent tools for automation suggestions and acceptance counts."""
from __future__ import annotations

import json
import logging
from typing import Optional

from homeassistant.core import HomeAssistant

from ...safe_errors import tool_error_text

# The agent logs under …nova.agent from every module, so filters and levels
# set for it keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


def _error_reply(exc: Exception, tool: str) -> str:
    return json.dumps({"error": tool_error_text(exc, tool)})


async def _exec_manage_autonomy(hass: HomeAssistant, args: dict) -> str:
    """Show, or reset, the counts of accepted offers. Nova never acts on its
    own."""
    try:
        from ... import cognitive_core
        if args.get("action", "list") == "revoke":
            key = args.get("pattern_key", "")
            if not key:
                return json.dumps({"error": "pattern_key required for revoke"})
            return json.dumps(cognitive_core.revoke_autonomy(key))
        counts = cognitive_core.status().get("autonomy_grants", [])
        return json.dumps({
            "acts_on_its_own": False,
            "note": "Every offer waits for a yes; these are acceptance counts only.",
            "grants": counts})
    except Exception as exc:
        return _error_reply(exc, "manage_autonomy")


async def _exec_review_suggestions(hass: HomeAssistant, args: dict) -> str:
    """Show the automation suggestions waiting for an answer."""
    try:
        from ...automation.patterns import get_analyzer
        waiting = await hass.async_add_executor_job(
            get_analyzer().get_pending_suggestions)
        if waiting:
            return json.dumps(waiting)
        return json.dumps({
            "message": "No pending suggestions. I need more data to identify patterns."})
    except Exception as exc:
        return _error_reply(exc, "review_suggestions")


async def _exec_approve_suggestion(
    hass: HomeAssistant, args: dict, *,
    user_id: Optional[str] = None, device_id: Optional[str] = None,
) -> str:
    """Approve a suggestion and install its automation in Home Assistant
    (v6.52.0). The audit log gets the real user and device of the request, and
    a request that has none is never given an invented one."""
    try:
        from ...automation.installation import install_approved_suggestion
        outcome = await install_approved_suggestion(
            hass, int(args.get("suggestion_id", 0)),
            requested_by_user_id=user_id or None,
            request_device_id=device_id or None)
        return json.dumps(outcome)
    except Exception as exc:
        return _error_reply(exc, "approve_suggestion")


async def _exec_dismiss_suggestion(hass: HomeAssistant, args: dict) -> str:
    """Turn a suggestion down."""
    try:
        from ...automation.patterns import get_analyzer
        suggestion_id = int(args.get("suggestion_id", 0))
        done = await hass.async_add_executor_job(
            get_analyzer().dismiss_suggestion, suggestion_id)
        return json.dumps({"success": done, "suggestion_id": suggestion_id})
    except Exception as exc:
        return _error_reply(exc, "dismiss_suggestion")
