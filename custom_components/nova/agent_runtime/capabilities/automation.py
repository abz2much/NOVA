"""Automation suggestions and autonomy grants."""
from __future__ import annotations

import json
import logging
from typing import Optional

from homeassistant.core import HomeAssistant

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


async def _exec_manage_autonomy(hass: HomeAssistant, args: dict) -> str:
    """View or revoke graduated-autonomy grants."""
    try:
        from ... import cognitive_core
        action = args.get("action", "list")
        if action == "revoke":
            pkey = args.get("pattern_key", "")
            if not pkey:
                return json.dumps({"error": "pattern_key required for revoke"})
            return json.dumps(cognitive_core.revoke_autonomy(pkey))
        # default: list
        status = cognitive_core.status()
        return json.dumps({"grants": status.get("autonomy_grants", [])})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_review_suggestions(hass: HomeAssistant, args: dict) -> str:
    """List pending automation suggestions."""
    try:
        from ...automation.patterns import get_analyzer
        suggestions = await hass.async_add_executor_job(
            get_analyzer().get_pending_suggestions)
        if not suggestions:
            return json.dumps({"message": "No pending suggestions. I need more data to identify patterns."})
        return json.dumps(suggestions)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_approve_suggestion(
    hass: HomeAssistant, args: dict, *,
    user_id: Optional[str] = None, device_id: Optional[str] = None,
) -> str:
    """Approve a suggestion — and install its automation into HA (v6.52.0).
    The request's real user and device go to the audit log; neither is
    invented when the request has none."""
    try:
        from ...automation.installation import install_approved_suggestion
        sid = int(args.get("suggestion_id", 0))
        res = await install_approved_suggestion(
            hass, sid, requested_by_user_id=user_id or None,
            request_device_id=device_id or None)
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_dismiss_suggestion(hass: HomeAssistant, args: dict) -> str:
    """Dismiss a suggestion."""
    try:
        from ...automation.patterns import get_analyzer
        sid = int(args.get("suggestion_id", 0))
        ok = await hass.async_add_executor_job(
            get_analyzer().dismiss_suggestion, sid)
        return json.dumps({"success": ok, "suggestion_id": sid})
    except Exception as exc:
        return json.dumps({"error": str(exc)})
