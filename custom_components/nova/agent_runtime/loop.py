"""The turn loop behind run_agent.

One turn builds the system prompt, decides the grant and the tools on offer,
then alternates between the model and the tool dispatcher until the model
answers in plain text or the round limit is reached. Provider trouble is
handled here too: a malformed tool call is retried, an oversized request is
retried once in a slim form, and any other failure moves to the reasoning
tier before Nova admits it cannot connect.
"""
from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from typing import Any, Optional

from homeassistant.core import HomeAssistant

from . import context as _context
from . import delegation as _delegation
from . import dispatcher as _dispatcher
from .capabilities.home import _build_clarification
from .context import _strip_home_state
from .grants import _MUTATING_TOOL_NAMES, _SLIM_TOOLS, _scoped_tool_list, resolve_grant
from .ha_tools import _ha_tools_to_openai_format
from .models import ToolExecutionContext, ToolResult
from .registry import trust_of

# The agent has always logged as "...nova.agent"; keep that name so existing
# log filters and levels still apply.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")

MAX_TOOL_ITERATIONS = 10

# The conversation keeps its last 20 turns and adds the new one, so a normal
# chat reaches the model with at most 21 turns. Only a history longer than
# that is folded into a summary; SUMMARIZE_KEEP turns stay word for word.
SUMMARIZE_THRESHOLD = 21
SUMMARIZE_KEEP = 6

_CONNECTIVITY_REPLY = ("I'm experiencing connectivity issues with my reasoning "
                       "systems, sir. Please try again in a moment.")
_NOT_CAUGHT_REPLY = "I'm not sure I caught that, sir."


def _arg(call, key: str):
    """One argument of a ToolCall; None when its arguments are not a mapping."""
    args: Any = getattr(call, "args", None)
    if not hasattr(args, "get"):
        return None
    return args.get(key)


def _nova_log(level: str, text: str) -> None:
    try:
        from ..websocket import nova_log
        nova_log(level, text)
    except Exception:
        pass


def _note_llm_up(hass: HomeAssistant) -> None:
    """A model call came back, so the AI backend is reachable."""
    try:
        from ..diagnostics.service_health import record_usage
        record_usage("llm", True)
        from .. import repair_notices
        repair_notices.clear_llm_problem(hass)
    except Exception:
        pass


def _note_llm_down(hass: HomeAssistant, detail: str) -> None:
    """Primary and fallback both failed: tell the health card and raise a
    Repair issue carrying the primary's own reason."""
    try:
        from ..websocket import nova_log
        from ..diagnostics.service_health import record_usage
        nova_log("ERROR", "agent: primary and fallback providers both failed — "
                          "check API keys / connectivity")
        record_usage("llm", False, detail=detail)
        from .. import repair_notices
        repair_notices.note_llm_problem(hass, detail)
    except Exception:
        pass


# Summarising an unusually long history

async def _summarise_text(hass: HomeAssistant, prompt: str, providers, primary: tuple) -> str:
    from ..providers.activity import execute_chat
    async with AsyncExitStack() as stack:
        turn = providers or _TurnProviders(hass, stack)
        client = await turn.primary(*primary)
        reply = await execute_chat(
            hass, client, [{"role": "user", "content": prompt}],
            role="llm", data_category="text", tools=None,
            max_tokens=256, temperature=0.3,
        )
    return reply.text


async def _maybe_summarize(
    hass: HomeAssistant, messages: list[dict],
    provider_name: str, api_key: str, model: str, base_url: Optional[str],
    *, providers: Optional["_TurnProviders"] = None,
) -> list[dict]:
    """Replace the older turns of an over long history with one summary.

    System messages and the last SUMMARIZE_KEEP turns are kept as they are.
    Any failure returns ``messages`` untouched.
    """
    system = [m for m in messages if m.get("role") == "system"]
    turns = [m for m in messages if m.get("role") != "system"]
    if len(turns) <= max(SUMMARIZE_THRESHOLD, SUMMARIZE_KEEP):
        return messages
    older, recent = turns[:-SUMMARIZE_KEEP], turns[-SUMMARIZE_KEEP:]
    transcript = "\n".join(
        f"{m.get('role', '?')}: {m.get('content', '')[:200]}"
        for m in older[-30:] if m.get("content", "")
    )
    prompt = ("Summarize this conversation in 2-3 sentences, preserving key facts:\n\n"
              + transcript)
    try:
        summary = await _summarise_text(
            hass, prompt, providers, (provider_name, api_key, model, base_url))
        if not summary:
            return messages
        # The model wrote this from user, assistant and tool text (mail, web
        # pages and so on), so it goes in fenced as quoted history, never as
        # instructions.
        from ..prompt_fence import fence
        quoted = fence(
            summary,
            label="EARLIER_CONVERSATION",
            noun="is a summary of the earlier part of this conversation",
            callback_noun="summarised history",
        )
    except Exception:
        return messages
    note = {"role": "system", "content": f"[Previous conversation: {quoted}]"}
    return [*system, note, *recent]


# Provider clients

class _TurnProviders:
    """Every provider client one agent turn uses.

    Clients are leased from the loaded entry's ProviderManager (a lease that
    matches the primary's configuration is the primary itself), or from a
    short lived manager when no entry is loaded. The turn's exit stack hands
    them all back however the turn ends, so a turn never leaks a client or
    loses one to a configuration change made meanwhile.
    """

    def __init__(self, hass: HomeAssistant, stack: AsyncExitStack):
        self._hass = hass
        self._stack = stack
        self._manager = None

    async def _scope(self):
        if self._manager is None:
            from ..providers.manager import provider_scope
            self._manager = await self._stack.enter_async_context(
                provider_scope(self._hass))
        return self._manager

    async def lease(self, spec, factory):
        manager = await self._scope()
        return await self._stack.enter_async_context(manager.lease(spec, factory=factory))

    async def primary(self, provider_name: str, api_key: str, model: str,
                      base_url: Optional[str]):
        from .. import llm_provider
        from ..providers.routing import ProviderSpec

        def build():
            return llm_provider.create_provider(provider_name, api_key, model, base_url)

        spec = ProviderSpec(provider=str(provider_name or ""), model=str(model or ""),
                            api_key=api_key or "", base_url=base_url)
        return await self.lease(spec, build)

    async def tier(self, config: dict, tier: str):
        from .. import llm_provider
        from ..providers.routing import tier_spec

        def build():
            return llm_provider.create_tier_provider(config, tier)

        return await self.lease(tier_spec(config, tier), build)


async def _create_provider_with_fallback(
    hass: HomeAssistant,
    provider_name: str, api_key: str, model: str,
    base_url: Optional[str],
    config: Optional[dict] = None,
    *,
    providers: "_TurnProviders",
):
    """The primary client, else the reasoning tier's, else RuntimeError."""
    try:
        return await providers.primary(provider_name, api_key, model, base_url)
    except Exception as exc:
        _LOGGER.warning("Primary provider '%s' failed: %s — trying the reasoning tier",
                        provider_name, exc)
    tried = f"{provider_name}"
    if config:
        tried += " + the reasoning tier"
        try:
            return await providers.tier(config, "reasoning")
        except Exception as exc:
            _LOGGER.warning("Reasoning-tier fallback also failed: %s", exc)
    raise RuntimeError(f"No LLM providers available (tried {tried})")


# Reading provider errors

def _error_text(exc: BaseException) -> str:
    """What the checks below search: a ProviderError's message together with
    the original provider error it wraps."""
    from ..providers.errors import error_text
    return error_text(exc)


_TOOL_FORMAT_MARKERS = (
    "tool_use_failed", "tool call validation failed", "failed to call a function",
    # Gemini thinking models on the OpenAI style endpoint reject tool calls
    # without a native thought_signature, which that format cannot carry.
    "thought_signature",
)
_CONNECTIVITY_MARKERS = (
    "timeout", "timed out", "connection", "connect", "unreachable", "name resolution",
    "dns", "getaddrinfo", "500", "502", "503", "504", "429", "rate limit",
    "too many requests",
)
_TOO_LARGE_MARKERS = (
    "request too large", "too large for model", "context length", "maximum context",
    "reduce the length", "prompt is too long", "input is too long",
    "code: 413", "code 413", "http 413",
)


def _is_tool_format_error(exc: Exception) -> bool:
    """The model was reached but produced a broken tool call.

    Llama 3.3 70b on Groq sometimes writes ``<function=name{json}>`` as text,
    which Groq rejects with a 400 (tool_use_failed or invalid_request_error).
    That is a model output slip, not an outage: it is retried and must never
    trip the circuit breaker or the offline reply.
    """
    text = _error_text(exc).lower()
    if any(marker in text for marker in _TOOL_FORMAT_MARKERS):
        return True
    return "400" in text and "invalid_request_error" in text and "function" in text


def _is_model_not_found(exc: Exception) -> bool:
    """The provider answered but has no such model: a settings mismatch.
    The same model fails everywhere, so the fallback must change model."""
    text = _error_text(exc).lower()
    if "not_found" not in text and "404" not in text:
        return False
    return any(word in text for word in ("model", "is not found", "does not exist"))


def _is_connectivity_error(exc: Exception) -> bool:
    """The failure looks like the model really being unreachable."""
    text = _error_text(exc).lower()
    return any(marker in text for marker in _CONNECTIVITY_MARKERS)


def _is_too_large(exc: Exception) -> bool:
    """A request too large or context length error (HTTP 413 or similar),
    usual on size limited tiers when many exposed entities inflate the Home
    Assistant tool schemas."""
    text = _error_text(exc).lower()
    return any(marker in text for marker in _TOO_LARGE_MARKERS)


async def run_agent(
    hass: HomeAssistant,
    *,
    messages: list[dict], persona: str,
    provider_name: str, api_key: str, model: str,
    base_url: Optional[str] = None,
    hass_api: Optional[Any] = None, user_input: Optional[Any] = None,
    temperature: float = 0.7, config: Optional[dict] = None,
    allowed_tools: Optional[set] = None, max_iterations: Optional[int] = None,
    depth: int = 0, profile_directive: Optional[str] = None,
    speaker_hint: str = "", identification: Optional[Any] = None,
) -> str:
    """Run one agent turn (see _run_agent_turn). Every provider client the
    turn leases is returned when it ends, however it ends."""
    async with AsyncExitStack() as stack:
        return await _run_agent_turn(
            hass, providers=_TurnProviders(hass, stack),
            messages=messages, persona=persona, provider_name=provider_name,
            api_key=api_key, model=model, base_url=base_url, hass_api=hass_api,
            user_input=user_input, temperature=temperature, config=config,
            allowed_tools=allowed_tools, max_iterations=max_iterations, depth=depth,
            profile_directive=profile_directive, speaker_hint=speaker_hint,
            identification=identification,
        )


# Building the prompt

def _cognitive_status() -> str:
    """A short Cognitive Core paragraph for the prompt; "" when it is idle."""
    try:
        from .. import cognitive_core
        status = cognitive_core.status()
        if not status.get("running"):
            return ""
        learning = status.get("learning", {})
        text = (
            f"\n\n## Cognitive Core\n"
            f"Running: {status['tick_count']} ticks, "
            f"{status['actions_taken']} actions taken. "
            f"Learning: {learning.get('days_of_data', 0)} days of data, "
            f"{learning.get('state_changes', 0)} state changes logged, "
            f"{learning.get('commands', 0)} commands learned."
        )
        ignores = cognitive_core.list_ignores()
        if ignores:
            shown = ", ".join(f"'{r['pattern']}' ({r['remaining_min']})" for r in ignores[:5])
            text += f"\nActive ignores: {shown}"
        return text
    except Exception:
        return ""


async def _situation_block(hass: HomeAssistant, speaker_hint: str, depth: int) -> str:
    """Live signals (presence, weather, calendar, energy, recent activity),
    plus who is speaking when Voice ID named them. The speaker is fenced so a
    name can never read as an order, and only a top level turn gets it (the
    caller passes "" when Voice ID is off)."""
    try:
        from .. import situation
        now = await hass.async_add_executor_job(situation.snapshot, hass)
    except Exception:
        now = ""
    block = f"## Situation now\n{now}\n\n" if now else ""
    if speaker_hint and depth == 0:
        from ..voice_id import fenced_speaker_hint
        block = (block or "## Situation now\n") + fenced_speaker_hint(speaker_hint) + "\n\n"
    return block


async def _awareness_block(hass: HomeAssistant, config: Optional[dict],
                           depth: int, user_input) -> str:
    """Camera history for a top level, interactive turn only; delegated and
    scheduled work never reads it. The SQLite read runs on the executor."""
    if depth != 0 or user_input is None:
        return ""
    try:
        from .. import camera_awareness
        return await hass.async_add_executor_job(camera_awareness.build_prompt, config or {})
    except Exception:
        return ""


def _offered_tools(hass: HomeAssistant, grant, hass_api) -> list:
    """The tools for this grant. The main grant has every Nova tool plus Home
    Assistant's LLM API tools; any other grant has only its own tools. The
    dispatcher enforces the same grant, and a feature not set up yet is left
    out."""
    tools = _scoped_tool_list(None if grant.tools is None else set(grant.tools))
    from .. import optional_features
    not_set_up = optional_features.tools_not_set_up(hass)
    tools = [t for t in tools if t.get("function", {}).get("name") not in not_set_up]
    if hass_api and grant.include_ha_tools:
        tools.extend(_ha_tools_to_openai_format(
            hass_api.tools, getattr(hass_api, "custom_serializer", None)))
    return tools


# Talking to the model

class _Exchange:
    """The model side of one turn: the messages so far, the tools on offer,
    the client answering, and how a failed call is recovered."""

    def __init__(self, hass, client, working, tools, *, temperature, grant,
                 providers, config, provider_name, model):
        self.hass = hass
        self.client = client
        self.working = working
        self.tools = tools
        self.temperature = temperature
        self.grant = grant
        self.providers = providers
        self.config = config
        self.provider_name = provider_name
        self.model = model
        self.slimmed = False   # the one slim retry after a 413 has been used

    async def ask(self, tools, max_tokens: int = 1024):
        """One provider round trip, recorded as activity."""
        from ..providers.activity import execute_chat
        return await execute_chat(
            self.hass, self.client, self.working,
            role="llm", data_category="text", tools=tools,
            max_tokens=max_tokens, temperature=self.temperature,
        )

    async def respond(self, iteration: int):
        """(result, None) for a model reply, (None, text) when the turn must
        end with ``text``, or (None, None) to go straight to the next round."""
        try:
            result = await self.ask(self.tools or None)
        except Exception as exc:
            if _is_tool_format_error(exc):
                return await self._after_bad_tool_call(iteration)
            if _is_too_large(exc) and self.grant.is_main and not self.slimmed:
                self._slim_down(iteration)
                return None, None
            return await self._fall_back(iteration, exc)
        _note_llm_up(self.hass)
        return result, None

    async def _after_bad_tool_call(self, iteration: int):
        # Reachable, just fumbled the syntax: try the same provider again
        # with tools, since it usually works and runs the real command.
        _LOGGER.info("Agent iter %d: model emitted malformed tool call — retrying",
                     iteration)
        try:
            return await self.ask(self.tools or None), None
        except Exception as again:
            if _is_connectivity_error(again) and not _is_tool_format_error(again):
                return None, _CONNECTIVITY_REPLY
            if not _is_tool_format_error(again):
                return None, _NOT_CAUGHT_REPLY
        # Still broken (common with garbled speech to text, such as TV
        # audio): settle for a plain answer without tools.
        _LOGGER.info("Agent iter %d: still malformed — answering without tools",
                     iteration)
        try:
            return await self.ask(None), None
        except Exception:
            return None, _NOT_CAUGHT_REPLY

    def _slim_down(self, iteration: int) -> None:
        """Retry once with a minimal request: no per entity Home Assistant
        tools, only Nova's essential tools, and a counts only pointer in
        place of the home state. Nova can still find and control things
        through search_entities, so a simple request stays online."""
        self.slimmed = True
        self.tools = _scoped_tool_list(_SLIM_TOOLS)
        first = self.working[0] if self.working else None
        if first is not None and first.get("role") == "system":
            content = _strip_home_state(first["content"]) + _context.slim_tools_note(_SLIM_TOOLS)
            self.working = [{**first, "content": content}, *self.working[1:]]
        _LOGGER.info(
            "Agent iter %d: request too large (413) — retrying slim "
            "(dropped HA tools + home-state block)", iteration,
        )
        _nova_log(
            "WARNING",
            "LLM request too large — retried with a slimmer prompt. "
            "Many exposed entities can exceed a provider's request "
            "limit; lower home_context_max_entities or reduce exposed "
            "entities if this keeps happening.",
        )

    def _failure_detail(self, exc: Exception) -> str:
        if _is_model_not_found(exc):
            return (
                f"model '{self.model}' not found on provider "
                f"'{self.provider_name}' — check llm_provider / model "
                f"settings (Ollama-tagged models need "
                f"llm_provider=ollama + llm_base_url)"
            )
        return f"agent LLM failed ({self.provider_name}/{self.model}): {str(exc)[:160]}"

    async def _fall_back(self, iteration: int, exc: Exception):
        """Any other failure: switch to the reasoning tier, which has its own
        provider and model (a missing model fails the same everywhere). The
        primary's reason is reported as an outage only if this fails too."""
        _LOGGER.warning("Agent LLM call failed (iter %d): %s — trying fallback",
                        iteration, exc)
        detail = self._failure_detail(exc)
        _nova_log("ERROR", detail)
        try:
            if not self.config:
                # No full config means no safe way to find another
                # provider's own key or endpoint; the primary key is never
                # reused elsewhere.
                raise RuntimeError("no configured fallback provider")
            self.client = await self.providers.tier(self.config, "reasoning")
            result = await self.ask(self.tools or None)
        except Exception:
            _note_llm_down(self.hass, detail)
            return None, _CONNECTIVITY_REPLY
        _note_llm_up(self.hass)
        return result, None


async def _run_tool_calls(hass, tool_calls, exchange: _Exchange, *, grant, ctx,
                          persona, provider_name, api_key, model, base_url,
                          config, depth) -> Optional[str]:
    """Run one batch of tool calls into the history. Returns a clarification
    to speak when a unique entity search came back ambiguous, else None.

    A search_entities call with require_unique must settle before a mutating
    call from the same batch runs, so those mutating calls are deferred to a
    later round. An ambiguous unique search ends the turn at once with a
    fixed question: no further model call and nothing stored.
    """
    names = {c.name for c in tool_calls}
    unique_search = any(c.name == "search_entities" and _arg(c, "require_unique")
                        for c in tool_calls)
    defer_mutating = unique_search and bool(names & _MUTATING_TOOL_NAMES)
    for call in tool_calls:
        # The dispatcher's precheck (malformed call, then the grant, which is
        # the security boundary, then deferral) runs before anything at all,
        # delegate_task included.
        outcome = _dispatcher.precheck(call, grant=grant, defer_mutating=defer_mutating)
        if outcome is None and call.name == "delegate_task":
            reply = await _delegation._run_delegated(
                hass, dict(call.args), persona=persona, provider_name=provider_name,
                api_key=api_key, model=model, base_url=base_url,
                config=config, depth=depth,
            )
            outcome = ToolResult(reply, trust=trust_of(call.name))
        elif outcome is None:
            outcome = await _dispatcher.execute(hass, call, ctx)
            if call.name == "search_entities" and _arg(call, "require_unique"):
                try:
                    found = json.loads(outcome.content)
                except Exception:
                    found = None
                if isinstance(found, dict) and found.get("ambiguous"):
                    return _build_clarification(found.get("candidates", []), hass)
        if not isinstance(outcome, ToolResult):
            # ToolResult.to_message is the one place untrusted text gets
            # fenced; anything else reaching the model would be a bug.
            raise TypeError("tool dispatch must produce a ToolResult")
        exchange.working.append(outcome.to_message(call.id, call.name))
    return None


async def _run_agent_turn(
    hass: HomeAssistant,
    *,
    messages: list[dict], persona: str,
    provider_name: str, api_key: str, model: str,
    base_url: Optional[str] = None,
    hass_api: Optional[Any] = None, user_input: Optional[Any] = None,
    temperature: float = 0.7, config: Optional[dict] = None,
    allowed_tools: Optional[set] = None, max_iterations: Optional[int] = None,
    depth: int = 0, profile_directive: Optional[str] = None,
    speaker_hint: str = "", identification: Optional[Any] = None,
    providers: "_TurnProviders",
) -> str:
    """One agent turn: Nova's tools plus Home Assistant's LLM API tools, the
    live home context in the prompt, and fallback to the reasoning tier."""
    home_context = await hass.async_add_executor_job(_context._build_home_context, hass)
    situation_block = await _situation_block(hass, speaker_hint, depth)
    awareness_block = await _awareness_block(hass, config, depth, user_input)

    # Grant and execution context are settled once, here. A top level run
    # with no user_input (follow ups, goal engagements) has nobody behind
    # it, so it gets the headless grant, never the main one.
    headless = depth == 0 and user_input is None
    grant = resolve_grant(allowed_tools, depth=depth, headless=headless)
    ctx = ToolExecutionContext.for_turn(
        user_input=user_input, hass_api=hass_api, depth=depth, headless=headless,
        identification=identification if depth == 0 else None)

    system_prompt = _context.build_system_prompt(
        hass, persona=persona, grant=grant, profile_directive=profile_directive,
        home_context=home_context, situation_block=situation_block,
        awareness_block=awareness_block, cog_status=_cognitive_status(),
    )
    full_messages = await _maybe_summarize(
        hass, [{"role": "system", "content": system_prompt}, *messages],
        provider_name, api_key, model, base_url, providers=providers,
    )
    tools = _offered_tools(hass, grant, hass_api)

    try:
        client = await _create_provider_with_fallback(
            hass, provider_name, api_key, model, base_url, config, providers=providers)
    except RuntimeError as exc:
        return f"I'm having trouble connecting to my reasoning systems, sir. {exc}"

    exchange = _Exchange(
        hass, client, list(full_messages), tools, temperature=temperature,
        grant=grant, providers=providers, config=config,
        provider_name=provider_name, model=model,
    )
    rounds = MAX_TOOL_ITERATIONS
    if max_iterations is not None:
        rounds = min(MAX_TOOL_ITERATIONS, max(1, int(max_iterations)))

    for iteration in range(rounds):
        result, final = await exchange.respond(iteration)
        if final is not None:
            return final
        if result is None:
            continue
        tool_calls = list(result.tool_calls)
        if not tool_calls:
            return result.text
        _LOGGER.info(
            "Agent iteration %d: %d tool call(s): %s",
            iteration + 1, len(tool_calls), ", ".join(str(tc.name) for tc in tool_calls),
        )
        # History gets the normalised text and ToolCall values only, never
        # the provider's own objects.
        exchange.working.append(result.assistant_message())
        clarification = await _run_tool_calls(
            hass, tool_calls, exchange, grant=grant, ctx=ctx, persona=persona,
            provider_name=provider_name, api_key=api_key, model=model,
            base_url=base_url, config=config, depth=depth,
        )
        if clarification is not None:
            return clarification

    exchange.working.append({"role": "user", "content": "Summarize what you've done briefly."})
    try:
        return (await exchange.ask(None, 512)).text
    except Exception:
        # Never claim it was done: the round limit was hit and nothing could
        # report what actually happened.
        return ("I couldn't finish that. I ran out of steps before I could check "
                "what was done, so please check before relying on it.")
