"""The provider/model turn loop behind run_agent."""
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

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


MAX_TOOL_ITERATIONS = 10


def _arg(call, key: str):
    """One argument of a ToolCall, or None when its arguments are unusable."""
    args = getattr(call, "args", None)
    return args.get(key) if hasattr(args, "get") else None


SUMMARIZE_THRESHOLD = 20


SUMMARIZE_KEEP      = 6


# ── Context summarization ──────────────────────────────────────────────────

async def _maybe_summarize(
    hass: HomeAssistant, messages: list[dict],
    provider_name: str, api_key: str, model: str, base_url: Optional[str],
    *, providers: Optional["_TurnProviders"] = None,
) -> list[dict]:
    """Compress old messages when context grows too long."""
    if len(messages) <= SUMMARIZE_THRESHOLD:
        return messages

    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    if len(non_system) <= SUMMARIZE_KEEP:
        return messages

    to_summarize = non_system[:-SUMMARIZE_KEEP]
    to_keep = non_system[-SUMMARIZE_KEEP:]

    parts = []
    for m in to_summarize[-30:]:
        role = m.get("role", "?")
        content = m.get("content", "")
        if content:
            parts.append(f"{role}: {content[:200]}")

    prompt = (
        "Summarize this conversation in 2-3 sentences, preserving key facts:\n\n"
        + "\n".join(parts)
    )

    try:
        from ..providers.activity import execute_chat
        async with AsyncExitStack() as stack:
            turn = providers or _TurnProviders(hass, stack)
            summarizer = await turn.primary(provider_name, api_key, model, base_url)
            result = await execute_chat(
                hass,
                summarizer,
                [{"role": "user", "content": prompt}],
                role="llm",
                data_category="text",
                tools=None,
                max_tokens=256,
                temperature=0.3,
            )
        summary = result.text
        if summary:
            # The summary is model-written from user, assistant and tool text
            # (email, web pages, ...), so it sits in its system slot fenced
            # as quoted history, never as instructions.
            from ..prompt_fence import fence
            fenced = fence(
                summary,
                label="EARLIER_CONVERSATION",
                noun="is a summary of the earlier part of this conversation",
                callback_noun="summarised history",
            )
            return system_msgs + [
                {"role": "system", "content": f"[Previous conversation: {fenced}]"}
            ] + to_keep
    except Exception:
        pass
    return messages


# ── Provider cascade ────────────────────────────────────────────────────────

class _TurnProviders:
    """The provider clients one agent turn uses.

    Each is leased from the loaded entry's ProviderManager (a client with the
    primary's configuration is the primary itself, not a second one) or,
    without a loaded entry, from a transient manager. Everything is released
    when the turn's exit stack closes, so a turn never leaves a client
    behind and never loses one to a concurrent configuration change."""

    def __init__(self, hass: HomeAssistant, stack: AsyncExitStack):
        self._hass = hass
        self._stack = stack
        self._manager = None

    async def lease(self, spec, factory):
        if self._manager is None:
            from ..providers.manager import provider_scope
            self._manager = await self._stack.enter_async_context(
                provider_scope(self._hass))
        return await self._stack.enter_async_context(
            self._manager.lease(spec, factory=factory))

    async def primary(self, provider_name: str, api_key: str, model: str,
                      base_url: Optional[str]):
        from .. import llm_provider
        from ..providers.routing import ProviderSpec
        spec = ProviderSpec(provider=str(provider_name or ""), model=str(model or ""),
                            api_key=api_key or "", base_url=base_url)
        return await self.lease(spec, lambda: llm_provider.create_provider(
            provider_name, api_key, model, base_url))

    async def tier(self, config: dict, tier: str):
        from .. import llm_provider
        from ..providers.routing import tier_spec
        return await self.lease(tier_spec(config, tier),
                                lambda: llm_provider.create_tier_provider(config, tier))


async def _create_provider_with_fallback(
    hass: HomeAssistant,
    provider_name: str, api_key: str, model: str,
    base_url: Optional[str],
    config: Optional[dict] = None,
    *,
    providers: "_TurnProviders",
):
    """Create provider with fallback chain: primary → reasoning tier → error."""
    try:
        return await providers.primary(provider_name, api_key, model, base_url)
    except Exception as exc:
        _LOGGER.warning("Primary provider '%s' failed: %s — trying the reasoning tier",
                        provider_name, exc)

    # Fallback to the reasoning tier
    if config:
        try:
            return await providers.tier(config, "reasoning")
        except Exception as exc2:
            _LOGGER.warning("Reasoning-tier fallback also failed: %s", exc2)

    raise RuntimeError(f"No LLM providers available (tried {provider_name} + Gemini)")


def _error_text(exc: BaseException) -> str:
    """The text the failure heuristics below match on: a normalized
    ProviderError's message plus the provider's original error it chains."""
    from ..providers.errors import error_text
    return error_text(exc)


def _is_tool_format_error(exc: Exception) -> bool:
    """
    True when the LLM was REACHABLE but emitted a malformed tool call.

    Groq/Llama-3.3-70b stochastically emits `<function=name{json}>` as text
    instead of a structured tool call; Groq rejects it with HTTP 400 and code
    'tool_use_failed' / 'invalid_request_error'. This is a MODEL-OUTPUT problem,
    not a connectivity failure — so it must NOT return the connectivity sentinel
    or trip the circuit breaker (the cloud is fine; the model just fumbled the
    syntax). The correct response is to retry, not to go offline.
    """
    s = _error_text(exc).lower()
    return (
        "tool_use_failed" in s
        or "tool call validation failed" in s
        or "failed to call a function" in s
        or "thought_signature" in s   # Gemini "thinking" models over the OpenAI-compat endpoint reject tool calls lacking a native thought_signature (a field the OpenAI format can't supply) — salvage by answering without tools rather than going offline
        or ("400" in s and "invalid_request_error" in s and "function" in s)
    )


def _is_model_not_found(exc: Exception) -> bool:
    """True when the provider was reachable but the MODEL doesn't exist there
    — a settings mismatch, not connectivity. Retrying the same model anywhere
    is guaranteed to fail; the fallback must switch models (v6.47.1)."""
    s = _error_text(exc).lower()
    return ("not_found" in s or "404" in s) and (
        "model" in s or "is not found" in s or "does not exist" in s
    )


def _is_connectivity_error(exc: Exception) -> bool:
    """True when the failure looks like the LLM being genuinely unreachable."""
    s = _error_text(exc).lower()
    return any(k in s for k in (
        "timeout", "timed out", "connection", "connect", "unreachable",
        "name resolution", "dns", "getaddrinfo",
        "500", "502", "503", "504",
        "429", "rate limit", "too many requests",
    ))


def _is_too_large(exc: Exception) -> bool:
    """True for a provider 'request too large' / context-length error (an HTTP
    413 or equivalent). Common on size-limited tiers when many entities are
    exposed and the HA tool schemas balloon the request."""
    s = _error_text(exc).lower()
    return any(k in s for k in (
        "request too large", "too large for model", "context length",
        "maximum context", "reduce the length", "prompt is too long",
        "input is too long", "code: 413", "code 413", "http 413",
    ))


async def run_agent(
    hass: HomeAssistant,
    *,
    messages: list[dict],
    persona: str,
    provider_name: str,
    api_key: str,
    model: str,
    base_url: Optional[str] = None,
    hass_api: Optional[Any] = None,
    user_input: Optional[Any] = None,
    temperature: float = 0.7,
    config: Optional[dict] = None,
    allowed_tools: Optional[set] = None,
    max_iterations: Optional[int] = None,
    depth: int = 0,
    profile_directive: Optional[str] = None,
) -> str:
    """Run the Nova agentic LLM loop (see _run_agent_turn). Every provider
    client the turn leases is released when it ends, however it ends."""
    async with AsyncExitStack() as stack:
        return await _run_agent_turn(
            hass, messages=messages, persona=persona,
            provider_name=provider_name, api_key=api_key, model=model,
            base_url=base_url, hass_api=hass_api, user_input=user_input,
            temperature=temperature, config=config,
            allowed_tools=allowed_tools, max_iterations=max_iterations,
            depth=depth, profile_directive=profile_directive,
            providers=_TurnProviders(hass, stack),
        )


async def _run_agent_turn(
    hass: HomeAssistant,
    *,
    messages: list[dict],
    persona: str,
    provider_name: str,
    api_key: str,
    model: str,
    base_url: Optional[str] = None,
    hass_api: Optional[Any] = None,
    user_input: Optional[Any] = None,
    temperature: float = 0.7,
    config: Optional[dict] = None,
    allowed_tools: Optional[set] = None,
    max_iterations: Optional[int] = None,
    depth: int = 0,
    profile_directive: Optional[str] = None,
    providers: "_TurnProviders",
) -> str:
    """
    Run the Nova agentic LLM loop (v5.7.07).

    Multi-turn tool-calling agent with:
      - Custom HA tools + HA LLM API tools
      - Provider fallback (Groq → Gemini)
      - Home context injection
      - Persistent learning
    """
    from ..providers.activity import execute_chat

    # Build system prompt with home context
    home_context = await hass.async_add_executor_job(
        _context._build_home_context, hass,
    )
    # v6.87.0: composite the live situational signals (presence, weather,
    # calendar, energy, recent activity) into one picture so judgments are
    # grounded in what is happening now, not just the static device inventory.
    situation_now = ""
    try:
        from .. import situation
        situation_now = await hass.async_add_executor_job(situation.snapshot, hass)
    except Exception:
        situation_now = ""
    situation_block = f"## Situation now\n{situation_now}\n\n" if situation_now else ""
    # Historical camera awareness stays separate from current situation. Its
    # bounded SQLite read runs in HA's executor and only for a top-level,
    # interactive conversation, never delegated or scheduled agent work.
    awareness_block = ""
    if depth == 0 and user_input is not None:
        try:
            from .. import camera_awareness
            awareness_block = await hass.async_add_executor_job(
                camera_awareness.build_prompt, config or {},
            )
        except Exception:
            awareness_block = ""
    # Inject cognitive core status
    cog_status = ""
    try:
        from .. import cognitive_core
        cstat = cognitive_core.status()
        if cstat.get("running"):
            ignores = cognitive_core.list_ignores()
            cog_status = (
                f"\n\n## Cognitive Core\n"
                f"Running: {cstat['tick_count']} ticks, "
                f"{cstat['actions_taken']} actions taken. "
                f"Learning: {cstat.get('learning', {}).get('days_of_data', 0)} days of data, "
                f"{cstat.get('learning', {}).get('state_changes', 0)} state changes logged, "
                f"{cstat.get('learning', {}).get('commands', 0)} commands learned."
            )
            if ignores:
                ig_strs = [f"'{r['pattern']}' ({r['remaining_min']})" for r in ignores[:5]]
                cog_status += f"\nActive ignores: {', '.join(ig_strs)}"
    except Exception:
        pass

    # The run's grant and execution context, decided once, server-side. A
    # top-level run with no user_input has no person behind it (follow-ups,
    # goal engagements): it gets the headless grant, never the main one.
    headless = depth == 0 and user_input is None
    grant = resolve_grant(allowed_tools, depth=depth, headless=headless)
    ctx = ToolExecutionContext.for_turn(user_input=user_input, hass_api=hass_api,
                                        depth=depth, headless=headless)

    system_prompt = _context.build_system_prompt(
        hass, persona=persona, grant=grant, profile_directive=profile_directive,
        home_context=home_context, situation_block=situation_block,
        awareness_block=awareness_block, cog_status=cog_status,
    )

    full_messages = [{"role": "system", "content": system_prompt}] + messages

    # Summarize if needed
    full_messages = await _maybe_summarize(
        hass, full_messages, provider_name, api_key, model, base_url,
        providers=providers,
    )

    # Build tool list from the grant: the main grant offers every Nova tool
    # plus Home Assistant's LLM API tools; any other grant offers exactly its
    # own tools and no HA API tools. The dispatcher enforces the same grant.
    tools = _scoped_tool_list(None if grant.is_main else set(grant.tools))
    if hass_api and grant.include_ha_tools:
        tools.extend(_ha_tools_to_openai_format(
            hass_api.tools, getattr(hass_api, "custom_serializer", None)))

    # Create provider with fallback
    try:
        client = await _create_provider_with_fallback(
            hass, provider_name, api_key, model, base_url, config,
            providers=providers,
        )
    except RuntimeError as exc:
        return f"I'm having trouble connecting to my reasoning systems, sir. {exc}"

    working = list(full_messages)
    slim_retried = False   # one-shot 413 recovery (drop HA tools + home-state)

    async def _chat_agent(message_list, tool_list, max_tokens):
        """One activity-recorded provider round trip for this agent loop.
        Returns the normalized ChatResponse."""
        return await execute_chat(
            hass,
            client,
            message_list,
            role="llm",
            data_category="text",
            tools=tool_list,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    _cap = MAX_TOOL_ITERATIONS
    if max_iterations is not None:
        _cap = max(1, min(int(max_iterations), MAX_TOOL_ITERATIONS))
    for iteration in range(_cap):
        try:
            result = await _chat_agent(working, tools or None, 1024)
            # A real agent call round-tripped → LLM is genuinely up.
            try:
                from ..diagnostics.service_health import record_usage
                record_usage("llm", True)
                from .. import repair_notices
                repair_notices.clear_llm_problem(hass)
            except Exception:
                pass
        except Exception as exc:
            if _is_tool_format_error(exc):
                # The model is reachable but emitted malformed tool syntax —
                # stochastic with Llama-3.3-70b. This is NOT connectivity, so we
                # must not return the connectivity sentinel (which trips the
                # breaker and forces offline mode). Retry the SAME provider once
                # with tools — it usually succeeds and runs the real command.
                _LOGGER.info(
                    "Agent iter %d: model emitted malformed tool call — retrying",
                    iteration,
                )
                try:
                    result = await _chat_agent(working, tools or None, 1024)
                except Exception as exc2:
                    if _is_tool_format_error(exc2):
                        # Still malformed — drop tools to salvage a plain answer.
                        # (Common with garbled speech-to-text, e.g. TV audio.)
                        _LOGGER.info(
                            "Agent iter %d: still malformed — answering without tools",
                            iteration,
                        )
                        try:
                            result = await _chat_agent(working, None, 1024)
                        except Exception:
                            return "I'm not sure I caught that, sir."
                    elif _is_connectivity_error(exc2):
                        return (
                            "I'm experiencing connectivity issues with my "
                            "reasoning systems, sir. Please try again in a moment."
                        )
                    else:
                        return "I'm not sure I caught that, sir."
            elif _is_too_large(exc) and grant.is_main and not slim_retried:
                # The request exceeded the provider's size limit (a 413 — common
                # on Groq's on-demand tier when many entities are exposed, which
                # bloats the HA tool schemas). Retry ONCE with a MINIMAL request:
                # drop the HA per-entity tools, trim Nova's own tools to the
                # essentials (the full schema is ~7K tokens on its own), and
                # replace the home-state snapshot with a counts-only pointer.
                # Nova can still answer and control via its core tools +
                # search_entities, so a simple query stops dropping to offline.
                slim_retried = True
                tools = _scoped_tool_list(_SLIM_TOOLS)
                if working and working[0].get("role") == "system":
                    working = ([{**working[0],
                                 "content": _strip_home_state(working[0]["content"])
                                 + _context.slim_tools_note(_SLIM_TOOLS)}]
                               + working[1:])
                _LOGGER.info(
                    "Agent iter %d: request too large (413) — retrying slim "
                    "(dropped HA tools + home-state block)", iteration,
                )
                try:
                    from ..websocket import nova_log
                    nova_log(
                        "WARNING",
                        "LLM request too large — retried with a slimmer prompt. "
                        "Many exposed entities can exceed a provider's request "
                        "limit; lower home_context_max_entities or reduce exposed "
                        "entities if this keeps happening.",
                    )
                except Exception:
                    pass
                continue
            else:
                # Genuine call failure (unreachable / 5xx / bad model / etc.)
                # — try the fallback. v6.47.1: the fallback is the REASONING
                # TIER (its own provider+model), not the same model replayed
                # on gemini — a 404'd model 404s everywhere identically.
                _LOGGER.warning(
                    "Agent LLM call failed (iter %d): %s — trying fallback",
                    iteration, exc,
                )
                # Capture the specific reason here (it's known at this point).
                # We only surface it to the health panel as DOWN if the FALLBACK
                # also fails, so a call the fallback recovers doesn't read as an
                # outage — but when it does surface, the user sees exactly why
                # (e.g. a decommissioned Groq model) instead of "breaker OPEN".
                if _is_model_not_found(exc):
                    _fail_detail = (
                        f"model '{model}' not found on provider "
                        f"'{provider_name}' — check llm_provider / model "
                        f"settings (Ollama-tagged models need "
                        f"llm_provider=ollama + llm_base_url)"
                    )
                else:
                    _fail_detail = (
                        f"agent LLM failed ({provider_name}/{model}): {str(exc)[:160]}"
                    )
                try:
                    from ..websocket import nova_log
                    nova_log("ERROR", _fail_detail)
                except Exception:
                    pass
                try:
                    if config:
                        client = await providers.tier(config, "reasoning")
                    else:
                        # Without the full config there is no safe way to
                        # resolve another provider's dedicated credential or
                        # endpoint. Never reuse the primary key for Gemini.
                        raise RuntimeError("no configured fallback provider")
                    result = await _chat_agent(working, tools or None, 1024)
                    # Fallback tier recovered — the reasoning backend is up.
                    try:
                        from ..diagnostics.service_health import record_usage
                        record_usage("llm", True)
                        from .. import repair_notices
                        repair_notices.clear_llm_problem(hass)
                    except Exception:
                        pass
                except Exception:
                    try:
                        from ..websocket import nova_log
                        from ..diagnostics.service_health import record_usage
                        nova_log(
                            "ERROR",
                            "agent: primary and fallback providers both failed — "
                            "check API keys / connectivity",
                        )
                        # Report the SPECIFIC primary reason so the diagnostics
                        # card shows the actual cause of the offline state.
                        record_usage("llm", False, detail=_fail_detail)
                        # And raise an actionable HA Repair issue (non-blocking).
                        from .. import repair_notices
                        repair_notices.note_llm_problem(hass, _fail_detail)
                    except Exception:
                        pass
                    return (
                        "I'm experiencing connectivity issues with my reasoning "
                        "systems, sir. Please try again in a moment."
                    )

        text = result.text
        tool_calls = list(result.tool_calls)

        if not tool_calls:
            return text

        _LOGGER.info(
            "Agent iteration %d: %d tool call(s): %s",
            iteration + 1, len(tool_calls),
            ", ".join(str(tc.name) for tc in tool_calls),
        )

        # The assistant turn for the history, built only from the normalized
        # text and ToolCall values (never from the provider's own objects).
        working.append(result.assistant_message())

        # Execute tools. A search_entities(require_unique=true) call must
        # resolve before any mutating tool call from the SAME batch — if the
        # model asked for both in one response, run only the search now and
        # defer the mutating calls to a later iteration once it has a clear
        # entity_id. An ambiguous unique search stops the whole batch and
        # returns a fixed clarification immediately, with no further LLM
        # call and no pending state stored anywhere.
        batch_names = {c.name for c in tool_calls}
        has_unique_search = any(
            c.name == "search_entities" and _arg(c, "require_unique")
            for c in tool_calls
        )
        defer_mutating = has_unique_search and bool(batch_names & _MUTATING_TOOL_NAMES)

        for call in tool_calls:
            # Every call passes the dispatcher's precheck first — malformed
            # call, then the run's grant (the security boundary), then
            # deferral — before anything is routed or run, delegate_task
            # included.
            outcome = _dispatcher.precheck(call, grant=grant, defer_mutating=defer_mutating)
            if outcome is None and call.name == "delegate_task":
                outcome = ToolResult(await _delegation._run_delegated(
                    hass, dict(call.args),
                    persona=persona, provider_name=provider_name,
                    api_key=api_key, model=model, base_url=base_url,
                    config=config, depth=depth,
                ))
            elif outcome is None:
                outcome = await _dispatcher.execute(hass, call, ctx)
                if call.name == "search_entities" and _arg(call, "require_unique"):
                    try:
                        parsed = json.loads(outcome.content)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, dict) and parsed.get("ambiguous"):
                        return _build_clarification(parsed.get("candidates", []), hass)
            if not isinstance(outcome, ToolResult):
                # Every result reaches the model through ToolResult.to_message
                # (the one place untrusted text is fenced); anything else is a
                # bug, and failing loudly beats sending unfenced text.
                raise TypeError("tool dispatch must produce a ToolResult")
            working.append(outcome.to_message(call.id, call.name))

    # Max iterations — ask for summary
    working.append({
        "role": "user",
        "content": "Summarize what you've done briefly.",
    })
    try:
        result = await _chat_agent(working, None, 512)
        return result.text
    except Exception:
        try:
            from .. import persona
            from .. import honorific as honorific_mod
            hon = honorific_mod.effective_honorific(hass)  # Phase C: presence-aware
            return persona.completed(hon)
        except Exception:
            return "I've completed the requested actions, sir."
