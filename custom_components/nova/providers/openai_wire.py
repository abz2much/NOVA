"""OpenAI chat-completions wire format, shared by the Groq SDK adapter and
the OpenAI-compatible adapter (OpenAI, Gemini, custom endpoints)."""
from __future__ import annotations

import json
from typing import Any

from .errors import ProviderError, ProviderErrorKind
from .models import ChatRequest, ChatResponse, ToolCall, Usage
from .reasoning import visible_text


def build_openai_kwargs(request: ChatRequest, model: str) -> dict[str, Any]:
    """The chat.completions.create keyword arguments for a request. Caller-
    built OpenAI message and tool dicts are sent exactly as built."""
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [m.to_openai() for m in request.messages],
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    if request.tools:
        kwargs["tools"] = [t.to_openai() for t in request.tools]
        kwargs["tool_choice"] = "auto"
    return kwargs


def openai_style_usage(resp: Any) -> Usage:
    """Token usage from an OpenAI-compatible chat completion response (Groq,
    OpenAI, and custom OpenAI-compatible endpoints share this shape). None
    per field when the response has no usage object, or the field itself is
    absent — never estimated."""
    u = getattr(resp, "usage", None)
    if u is None:
        return Usage()
    return Usage(
        input_tokens=getattr(u, "prompt_tokens", None),
        output_tokens=getattr(u, "completion_tokens", None),
    )


def parse_openai_completion(resp: Any, *, provider: str, model: str) -> ChatResponse:
    """Normalize a chat completion. Reads only the visible content and the
    structured tool calls: provider reasoning fields (``reasoning``,
    ``reasoning_content``, ...) are never read, and a leaked reasoning
    envelope in the content is removed."""
    try:
        choice = resp.choices[0]
        message = choice.message
    except (AttributeError, IndexError, TypeError) as exc:
        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, provider) from exc
    calls = []
    # Check message.tool_calls directly — some providers set
    # finish_reason="stop" even when tool_calls are present.
    for tc in getattr(message, "tool_calls", None) or []:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except (TypeError, ValueError) as exc:
            raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, provider,
                                detail="the provider returned invalid tool arguments") from exc
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, provider,
                                detail="the provider returned non-object tool arguments")
        calls.append(ToolCall(id=str(tc.id or ""), name=str(tc.function.name or ""), args=args))
    text = visible_text(getattr(message, "content", None))
    tool_calls = tuple(calls)
    return ChatResponse(
        text=text,
        tool_calls=tool_calls,
        usage=openai_style_usage(resp),
        provider=provider,
        model=model,
        raw=openai_raw(text, tool_calls),
    )


def openai_raw(text: str, tool_calls: tuple[ToolCall, ...]) -> dict:
    """The sanitized ``raw`` compatibility value: an OpenAI-shaped assistant
    message rebuilt from normalized data only."""
    raw: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        raw["tool_calls"] = [c.to_openai() for c in tool_calls]
    return raw
