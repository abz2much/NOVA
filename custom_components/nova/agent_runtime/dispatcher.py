"""Tool dispatch — the agent's security boundary.

Every tool call the model emits passes through ``precheck`` (malformed call,
the run's grant, mutation deferral) before ``execute`` runs it: Nova's own
executors first, then Home Assistant's LLM API behind Nova's Assist policy.
Both return a typed ToolResult; nothing reaches an executor without the
grant check. Prompt text and the offered tool list are guidance only."""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from .grants import _MUTATING_TOOL_NAMES
from .ha_tools import _ha_kwargs
from .models import ToolExecutionContext, ToolGrant, ToolResult
from .registry import _TOOL_MAP, trust_of

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
    *,
    ctx: Optional[ToolExecutionContext] = None,
) -> str:
    """Execute a tool call — custom tools first, then HA LLM API fallback.

    Returns the tool's text. The turn loop never calls this directly: it goes
    through precheck() and execute(), which enforce the run's grant first.
    ``ctx`` carries the request's real user and device; without it they are
    read from ``user_input`` exactly as before (None when there is none)."""
    if ctx is None:
        ctx = ToolExecutionContext.for_turn(user_input=user_input, hass_api=hass_api,
                                            depth=0, headless=False)
    # Custom Nova tools
    if tool_name in _TOOL_MAP:
        try:
            fn = _TOOL_MAP[tool_name]
            # Tools that act on someone's behalf accept the request's real
            # device_id and/or Home Assistant user_id (None when there is
            # none — Nova never invents one); every other tool gets neither.
            import inspect
            params: Any
            try:
                params = inspect.signature(fn).parameters
            except (TypeError, ValueError):
                params = {}
            extra = {}
            if "device_id" in params:
                extra["device_id"] = ctx.device_id
            if "user_id" in params:
                extra["user_id"] = ctx.user_id
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
            device_id=ctx.device_id or "",
            user_id=ctx.user_id,
        )
        if not decision.allowed:
            return json.dumps(decision.tool_result())
        # Only a read is retried. A mutating call that raised may already have
        # acted, so running it again could act twice: it gets one attempt.
        attempts = 1 if decision.classification.mutating else MAX_TOOL_RETRIES + 1
        for attempt in range(attempts):
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
                if attempt >= attempts - 1:
                    await assist_policy.async_record_execution(hass, decision, False)
                    return json.dumps({"error": f"{tool_name} failed: {exc}"})

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


def _not_granted_message(tool_name: str, grant: ToolGrant) -> str:
    if grant.name == "delegated":
        return (f"tool '{tool_name}' is not available to this "
                f"sub-agent — it was not granted")
    if grant.name == "headless":
        return (f"tool '{tool_name}' is not available to a scheduled run — it "
                f"can check and report, not act or change anything")
    return f"tool '{tool_name}' is not available here — it was not granted"


def precheck(call: Any, *, grant: ToolGrant, defer_mutating: bool) -> Optional[ToolResult]:
    """Refuse a call before anything runs, or return None to let it proceed.

    Order matters and is fixed: a malformed call, then the run's grant (the
    security boundary — checked before delegate_task or any other routing),
    then deferral of a mutating call behind an unresolved unique search."""
    name = getattr(call, "name", None)
    args = getattr(call, "args", None)
    if not isinstance(name, str) or not name or not isinstance(args, Mapping):
        return ToolResult.error("malformed_call",
                                "the tool call was malformed (it needs a tool name "
                                "and an object of arguments) and was not run")
    if not grant.allows(name):
        return ToolResult.error("not_granted", _not_granted_message(name, grant))
    if defer_mutating and name in _MUTATING_TOOL_NAMES:
        return ToolResult(json.dumps({
            "deferred": True,
            "reason": "Resolve the exact entity with "
                      "search_entities(require_unique=true) first, then "
                      "repeat this action with the resolved entity_id.",
        }), code="deferred")
    return None


async def execute(hass: HomeAssistant, call: Any, ctx: ToolExecutionContext) -> ToolResult:
    """Run one call that passed precheck() and type its result."""
    content = await _execute_tool(hass, call.name, dict(call.args),
                                  ctx.hass_api, ctx.user_input, ctx=ctx)
    return ToolResult.from_executor(content, trust_of(call.name))
