"""Web research, calendars and email."""
from __future__ import annotations

import json
import logging

from homeassistant.core import HomeAssistant

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


async def _exec_web_research(hass: HomeAssistant, args: dict) -> str:
    """Web Research Agent — external knowledge retrieval (v6.51.0)."""
    try:
        from ... import web_research
        result = await web_research.research(hass, args.get("query", ""))
        try:
            from ...websocket import nova_log
            q = result.get("query", "")
            if result.get("error"):
                nova_log("AGENT", f"web research '{q}': {result['error']}")
            else:
                nova_log("AGENT", f"web research '{q}' via {result.get('backend')}")
        except Exception:
            pass
        return json.dumps(result)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_calendar_agenda(hass: HomeAssistant, args: dict) -> str:
    """Communication Agent — calendar agenda + conflict detection (v6.51.0)."""
    try:
        from ... import comms
        horizon = int(args.get("horizon_hours", 24) or 24)
        return json.dumps(comms.agenda(hass, horizon))
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_read_email(hass: HomeAssistant, args: dict) -> str:
    """Mail Agent — read-only inbox access (v6.81.0)."""
    try:
        from ... import mail
        limit = int(args.get("limit", 5) or 5)
        unread_only = bool(args.get("unread_only", False))
        folder = args.get("folder") or None
        result = await mail.fetch_recent(
            hass, limit=limit, unread_only=unread_only, folder=folder)
        return json.dumps(result)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
