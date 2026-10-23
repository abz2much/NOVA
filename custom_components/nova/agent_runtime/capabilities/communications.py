"""Agent tools for web research, the calendar and email."""
from __future__ import annotations

import json
import logging

from homeassistant.core import HomeAssistant

from ...safe_errors import tool_error_text

# The agent logs under …nova.agent from every module, so filters and levels
# set for it keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


def _error_reply(exc: Exception, tool: str) -> str:
    return json.dumps({"error": tool_error_text(exc, tool)})


def _log_research(result: dict) -> None:
    """Note a web research result in the Nova log. A logging problem is not a
    research problem, so it is swallowed."""
    try:
        from ...websocket import nova_log
        asked = result.get("query", "")
        if result.get("error"):
            nova_log("AGENT", f"web research '{asked}': {result['error']}")
        else:
            nova_log("AGENT", f"web research '{asked}' via {result.get('backend')}")
    except Exception:
        pass


async def _exec_web_research(hass: HomeAssistant, args: dict) -> str:
    """Web Research Agent: look up outside knowledge (v6.51.0)."""
    try:
        from ... import web_research
        found = await web_research.research(hass, args.get("query", ""))
        _log_research(found)
        return json.dumps(found)
    except Exception as exc:
        return _error_reply(exc, "web_research")


async def _exec_calendar_agenda(hass: HomeAssistant, args: dict) -> str:
    """Communication Agent: the calendar agenda and any clashes (v6.51.0)."""
    try:
        from ... import comms
        hours = int(args.get("horizon_hours", 24) or 24)
        return json.dumps(comms.agenda(hass, hours))
    except Exception as exc:
        return _error_reply(exc, "calendar_agenda")


async def _exec_read_email(hass: HomeAssistant, args: dict) -> str:
    """Mail Agent: read only access to the inbox (v6.81.0)."""
    try:
        from ... import mail
        messages = await mail.fetch_recent(
            hass,
            limit=int(args.get("limit", 5) or 5),
            unread_only=bool(args.get("unread_only", False)),
            folder=args.get("folder") or None,
        )
        return json.dumps(messages)
    except Exception as exc:
        return _error_reply(exc, "read_email")
