"""Tool dispatch: Nova's own executors first, then Home Assistant's LLM API
behind Nova's Assist policy."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from .ha_tools import _ha_kwargs
from .registry import _TOOL_MAP

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


MAX_TOOL_RETRIES    = 2


async def _execute_tool(
    hass: HomeAssistant,
    tool_name: str,
    tool_args: dict,
    hass_api: Optional[Any] = None,
    user_input: Optional[Any] = None,
) -> str:
    """Execute a tool call — custom tools first, then HA LLM API fallback."""
    # Custom Nova tools
    if tool_name in _TOOL_MAP:
        try:
            fn = _TOOL_MAP[tool_name]
            # Most tools don't need to know the request's device_id; only pass
            # it to the handful that actuate locks/covers (v7.87.0's
            # voice-blocked-opening gate needs it) so nothing else changes.
            # The few tools that act on someone's behalf also accept user_id,
            # the request's real Home Assistant user (None when there is none);
            # Nova never invents one.
            import inspect
            try:
                params = inspect.signature(fn).parameters
            except (TypeError, ValueError):
                params = {}
            extra = {}
            if "device_id" in params:
                extra["device_id"] = getattr(user_input, "device_id", None)
            if "user_id" in params:
                extra["user_id"] = getattr(
                    getattr(user_input, "context", None), "user_id", None)
            return await fn(hass, tool_args, **extra)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # Fallback to HA's built-in LLM API tools
    if hass_api:
        from ..const import DOMAIN
        # Nova's policy gate applies to Assist tools too (assist_policy.py):
        # a mutating call is classified and confirmed before it reaches Home
        # Assistant, and refused when it can't be classified safely. HA still
        # runs the call itself below, with its own context and permissions.
        from .. import assist_policy
        decision = await assist_policy.async_authorize(
            hass, hass_api, tool_name, tool_args,
            device_id=getattr(user_input, "device_id", None) or "",
            user_id=getattr(getattr(user_input, "context", None), "user_id", None),
        )
        if not decision.allowed:
            return json.dumps(decision.tool_result())
        for attempt in range(MAX_TOOL_RETRIES + 1):
            try:
                tool_input = llm.ToolInput(**_ha_kwargs(
                    llm.ToolInput,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    platform=DOMAIN,
                    context=user_input.context if user_input else None,
                    user_prompt=user_input.text if user_input else "",
                    language=user_input.language if user_input else "en",
                    assistant="conversation",
                    device_id=user_input.device_id if user_input else None,
                ))
                result = await hass_api.async_call_tool(tool_input)
                await assist_policy.async_record_execution(hass, decision, True)
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as exc:
                if attempt >= MAX_TOOL_RETRIES:
                    await assist_policy.async_record_execution(hass, decision, False)
                    return json.dumps({"error": f"{tool_name} failed: {exc}"})

    return json.dumps({"error": f"Unknown tool: {tool_name}"})
