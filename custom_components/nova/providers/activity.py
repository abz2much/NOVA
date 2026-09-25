"""The single activity-aware execution boundary.

Every production chat call runs through execute_chat(). It:

* runs the blocking adapter call in Home Assistant's executor, bounded by the
  adapter's concurrency policy;
* normalizes every failure to a ProviderError chained to its original;
* lets task cancellation propagate untouched;
* records one Provider Activity sample: provider, model, role, execution
  location, data category, success, provider-reported token counts and
  latency.

The recorder is handed those scalar fields and nothing else. Prompts,
responses, tool arguments, images, entity states, credentials, headers, the
``raw`` compatibility value and any hidden reasoning never reach it.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from .base import LLMProvider
from .errors import ProviderError, normalize_error
from .manager import call_tracker
from .models import DATA_CATEGORIES, ChatRequest, ChatResponse
from .routing import execution_location

# Fixed role vocabulary for Provider Activity rows.
ROLES = frozenset({
    "llm",               # the conversation agent (and its summarizer)
    "classifier",        # observer tier 1
    "reasoning",         # observer tier 2
    "briefing",          # scheduled and on-demand briefings
    "vision",            # camera image analysis
    "camera_reasoning",  # judging a camera description
    "sentinel",          # sentinel alerts
    "summary",           # nova.summarise
    "scenes",            # scene-by-intent matching
    "package",           # package tracking extraction
    "coverage",          # camera coverage mapping
    "connection_test",   # settings and config-flow connection checks
})


def _limiter(provider: Any) -> Optional[asyncio.Semaphore]:
    """The per-client semaphore enforcing the adapter's concurrency policy.
    Created lazily on the event loop and kept on the adapter itself, so it
    lives and dies with the client."""
    if not isinstance(provider, LLMProvider):
        return None
    limiter = provider.__dict__.get("_limiter")
    if limiter is None:
        limiter = asyncio.Semaphore(max(1, int(provider.concurrency.max_in_flight)))
        provider.__dict__["_limiter"] = limiter
    return limiter


def _runner(provider: Any, request: ChatRequest, messages, kwargs: dict):
    """The blocking call handed to the executor."""
    if isinstance(provider, LLMProvider):
        return lambda: provider.run(request)

    # A provider-shaped object outside the adapter hierarchy (test doubles):
    # its legacy dictionary reply is normalized, and its raw value dropped.
    def _legacy():
        reply = provider.chat(messages, **kwargs)
        return ChatResponse.from_legacy(
            reply, provider=str(getattr(provider, "name", "") or ""),
            model=str(request.model or getattr(provider, "model", "") or ""))
    return _legacy


async def execute_chat(
    hass,
    provider: Any,
    messages: list[dict],
    *,
    role: str,
    data_category: str,
    tools: Optional[list[dict]] = None,
    max_tokens: int = 512,
    temperature: float = 0.7,
    model_override: Optional[str] = None,
) -> ChatResponse:
    """Run one chat call and record its Provider Activity sample.

    Returns the typed ChatResponse. Raises ProviderError (chained to the
    original) on failure, and asyncio.CancelledError untouched when the
    calling task is cancelled. Recording is best-effort and can never alter
    the result or the error."""
    provider_name = str(getattr(provider, "name", "") or "unknown")
    model = str(model_override or getattr(provider, "model", "") or "")
    category = data_category if data_category in DATA_CATEGORIES else "text"
    role = role if role in ROLES else "llm"
    request = ChatRequest.from_legacy(messages, tools, max_tokens, temperature, model_override)
    kwargs: dict[str, Any] = {"tools": tools, "max_tokens": max_tokens,
                              "temperature": temperature}
    if model_override is not None:
        kwargs["model_override"] = model_override

    start = time.monotonic()
    response: Optional[ChatResponse] = None
    success = False
    try:
        limiter = _limiter(provider)
        runner = _runner(provider, request, messages, kwargs)
        # The owning manager defers closing a replaced client until its
        # in-flight calls finish.
        tracker = call_tracker(provider)
        if tracker is not None:
            tracker.call_started(provider)
        try:
            if limiter is None:
                response = await hass.async_add_executor_job(runner)
            else:
                async with limiter:
                    response = await hass.async_add_executor_job(runner)
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except Exception as exc:
            raise normalize_error(exc, provider_name) from exc
        finally:
            if tracker is not None:
                tracker.call_finished(provider)
        success = True
        return response
    except asyncio.CancelledError:
        # Propagate cancellation at once; a cancelled call is not recorded.
        start = None
        raise
    finally:
        if start is not None:
            usage = response.usage if response is not None else None
            await _record(
                hass,
                provider=provider_name,
                model=model,
                role=role,
                location=execution_location(provider),
                data_category=category,
                success=success,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                latency_ms=int((time.monotonic() - start) * 1000),
            )


async def _record(
    hass,
    *,
    provider: str,
    model: str,
    role: str,
    location: str,
    data_category: str,
    success: bool,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    latency_ms: int,
) -> None:
    """Hand one bounded, scalar-only sample to provider_activity.record().
    The only caller of that recorder. Never raises."""
    try:
        from .. import provider_activity
        db_path = provider_activity.db_path_for(hass)
        if db_path is None:
            return  # no Home Assistant config directory to write to
        await hass.async_add_executor_job(
            lambda: provider_activity.record(
                provider, model, role, location, data_category, success,
                input_tokens, output_tokens, latency_ms, db_path=db_path))
    except Exception:
        pass  # activity logging must never affect the actual call
