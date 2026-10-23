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
from ..safe_errors import safe_error_message

# The agent logs under …nova.agent from every module, so filters and levels
# set for it keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


MAX_TOOL_RETRIES = 2


def _requester_arguments(fn, ctx: ToolExecutionContext) -> dict:
    """The request's real device, user and voice identity, for just the ones
    the tool names as parameters. A request without one passes None. Nova never
    invents one, and a tool that acts for nobody in particular gets none."""
    import inspect
    wanted: Any
    try:
        wanted = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        wanted = {}
    offered = {"device_id": ctx.device_id, "user_id": ctx.user_id,
               "identification": ctx.identification}
    return {name: value for name, value in offered.items() if name in wanted}


def _assist_tool_input(tool_name: str, tool_args: dict, user_input: Optional[Any]):
    """The Home Assistant ToolInput for one Assist tool call. Only the fields
    this version of Home Assistant accepts are passed."""
    from ..const import DOMAIN
    turn = user_input
    fields = {"tool_name": tool_name, "tool_args": tool_args, "platform": DOMAIN,
              "assistant": "conversation",
              "context": turn.context if turn else None,
              "user_prompt": turn.text if turn else "",
              "language": turn.language if turn else "en",
              "device_id": turn.device_id if turn else None}
    return llm.ToolInput(**_ha_kwargs(llm.ToolInput, **fields))


async def _execute_tool(hass: HomeAssistant, tool_name: str, tool_args: dict,
                        hass_api: Optional[Any] = None, user_input: Optional[Any] = None,
                        *, ctx: Optional[ToolExecutionContext] = None) -> str:
    """Run one tool call: Nova's own tools first, then Home Assistant's LLM
    API.

    Returns the tool's text. The turn loop never calls this directly. It goes
    through precheck() and execute(), which enforce the run's grant first.
    ``ctx`` holds the request's real user and device. Without it they are read
    from ``user_input`` as they always were (None when there is none)."""
    if ctx is None:
        ctx = ToolExecutionContext.for_turn(user_input=user_input, hass_api=hass_api,
                                            depth=0, headless=False)
    # Nova's own tools
    if tool_name in _TOOL_MAP:
        try:
            fn = _TOOL_MAP[tool_name]
            return await fn(hass, tool_args, **_requester_arguments(fn, ctx))
        except Exception as exc:
            return json.dumps({"error": safe_error_message(
                exc, where=f"tool {tool_name}", log=True, keep_ha_text=True)})

    # Anything else goes to Home Assistant's own LLM API tools
    if hass_api:
        # Nova's policy gate covers Assist tools as well (assist_policy.py): a
        # call that changes something is classified and confirmed before it
        # reaches Home Assistant, and refused if it cannot be classified
        # safely. Home Assistant then runs it with its own context and
        # permissions.
        from .. import assist_policy
        decision = await assist_policy.async_authorize(
            hass, hass_api, tool_name, tool_args,
            device_id=ctx.device_id or "",
            user_id=ctx.user_id,
        )
        if not decision.allowed:
            return json.dumps(decision.tool_result())
        # A read may be retried. A call that changes something and then raised
        # might already have acted, so a second try could act twice. It gets
        # a single attempt.
        tries = 1 if decision.classification.mutating else MAX_TOOL_RETRIES + 1
        for attempt in range(tries):
            try:
                outcome = await hass_api.async_call_tool(
                    _assist_tool_input(tool_name, tool_args, user_input))
                await assist_policy.async_record_execution(hass, decision, True)
                return json.dumps(outcome) if isinstance(outcome, dict) else str(outcome)
            except Exception as exc:
                if attempt >= tries - 1:
                    await assist_policy.async_record_execution(hass, decision, False)
                    return json.dumps({"error": f"{tool_name} failed: " + safe_error_message(
                        exc, where=f"tool {tool_name}", log=True, keep_ha_text=True)})

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
