"""Gemini Interactions API wire format.

Nova keeps its own history, so every request is stateless (``store`` false):
the whole conversation goes in ``input`` as steps. Gemini's reasoning state
lives in ``thought`` steps, which must be sent back exactly as received. They
are recorded on the first tool call of the turn that produced them, as
``extra_content = {"google": {"steps": [...]}}``, and replayed from there. No
other provider ever sees that field (openai_wire strips it).

Nothing here does network I/O.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from .errors import ProviderError, ProviderErrorKind, ProviderRequestError
from .models import ChatRequest, ChatResponse, ToolCall, ToolSpec, Usage
from .reasoning import visible_text

PROVIDER = "gemini"
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_HOST = "generativelanguage.googleapis.com"

# Thinking models spend max_output_tokens on thought tokens before any answer
# text, so Nova's small budgets (40 to 300 tokens for scenes, the classifier
# and the camera) can run out with nothing written. Room for the reasoning is
# added on top of what the caller asked for, and the level is set low: these
# are short tasks.
THINKING_TOKEN_ROOM = 2048
THINKING_LEVEL = "low"
# Models that take a thinking level (Gemini 2.5, 3.x and later).
_THINKING_MODELS = re.compile(r"^gemini-(2\.5|[3-9])")

# Google advises leaving temperature at its default of 1.0 on Gemini 3 and
# later, because a lower value can cause looping or poorer answers. So Nova
# sends its temperature only to models known to take one (Gemini 2.x and
# Gemma). An alias such as gemini-flash-latest could point at a newer model,
# so it gets no temperature either.
_TEMPERATURE_MODELS = re.compile(r"^(gemini-2|gemma)")

# Largest recorded model step list kept for replay. Signatures are a few KB.
MAX_RECORDED_CHARS = 262144

_SAFETY_WORDS = ("safety", "blocked", "prohibited", "recitation")
SAFETY_DETAIL = "Gemini blocked the request (safety)"
TOO_LARGE_DETAIL = "request too large for model (context length exceeded)"
MALFORMED_CALL_DETAIL = "tool call validation failed (the model produced a malformed tool call)"
# Schema keys Gemini's function declarations do not take.
_SCHEMA_DROP = frozenset({"$schema", "additionalProperties", "strict"})


def bare_model(model: Optional[str]) -> str:
    """The model id without the API resource prefix a model picker may save."""
    name = str(model or "").strip()
    return name[len("models/"):] if name.startswith("models/") else name


def takes_temperature(model: Optional[str]) -> bool:
    return bool(_TEMPERATURE_MODELS.match(bare_model(model).lower()))


def thinking_level_for(model: Optional[str]) -> Optional[str]:
    return THINKING_LEVEL if _THINKING_MODELS.match(bare_model(model).lower()) else None


def is_too_large(text: str) -> bool:
    """A 400 body that says the prompt exceeded the model's input limit."""
    text = text.lower()
    return "too large" in text or ("token count" in text and "exceed" in text)


def is_incomplete(data: Any) -> bool:
    """An interaction cut off by its output limit (thought tokens count)."""
    return isinstance(data, dict) and str(data.get("status") or "").lower() == "incomplete"


def has_safety_words(text: str) -> bool:
    text = text.lower()
    return any(word in text for word in _SAFETY_WORDS)


# ── Request ─────────────────────────────────────────────────────────────────

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text") or "") for p in content
            if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def _image_part(url: str) -> dict:
    """An inline ``data:`` image as a Gemini image part. A remote URL is not
    fetched: Nova's camera features all send inline JPEGs."""
    if not url.startswith("data:") or "," not in url:
        raise ProviderRequestError(
            ProviderErrorKind.INVALID_REQUEST, PROVIDER,
            detail="Gemini images must be inline data URLs")
    header, data = url.split(",", 1)
    if ";base64" not in header:
        raise ProviderRequestError(
            ProviderErrorKind.INVALID_REQUEST, PROVIDER,
            detail="Gemini images must be base64 data URLs")
    mime = header[len("data:"):].split(";", 1)[0] or "image/jpeg"
    return {"type": "image", "data": data, "mime_type": mime}


def _content_parts(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    parts: list[dict] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                parts.append({"type": "text", "text": str(part.get("text") or "")})
            elif part.get("type") == "image_url":
                image = part.get("image_url")
                url = image.get("url") if isinstance(image, dict) else ""
                parts.append(_image_part(str(url or "")))
    return parts or [{"type": "text", "text": ""}]


def _arguments(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value) if value else {}
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _recorded_steps(calls: list[dict], ids: list[str]) -> Optional[list[dict]]:
    """The model steps recorded on this assistant turn's tool calls, when
    they still describe exactly these calls. None for history that did not
    come from this adapter (another provider's turn, or a trimmed one)."""
    for call in calls:
        extra = call.get("extra_content")
        google = extra.get("google") if isinstance(extra, dict) else None
        steps = google.get("steps") if isinstance(google, dict) else None
        if not isinstance(steps, list) or not steps:
            continue
        if not all(isinstance(s, dict) for s in steps):
            continue
        recorded_ids = [str(s.get("id") or "") for s in steps
                        if s.get("type") == "function_call"]
        if recorded_ids == ids:
            return [dict(s) for s in steps]
    return None


def _assistant_steps(message: Mapping[str, Any], names: dict[str, str]) -> list[dict]:
    calls = [c for c in message.get("tool_calls") or [] if isinstance(c, dict)]
    ids = [str(c.get("id") or f"call_{i}") for i, c in enumerate(calls)]
    for call, call_id in zip(calls, ids):
        names[call_id] = str((call.get("function") or {}).get("name") or "")
    recorded = _recorded_steps(calls, ids)
    if recorded is not None:
        return recorded
    steps: list[dict] = []
    text = _text_of(message.get("content"))
    if text:
        steps.append({"type": "model_output", "content": [{"type": "text", "text": text}]})
    for call, call_id in zip(calls, ids):
        fn = call.get("function") or {}
        steps.append({"type": "function_call", "id": call_id,
                      "name": str(fn.get("name") or ""),
                      "arguments": _arguments(fn.get("arguments"))})
    return steps


def _flattened_assistant(message: Mapping[str, Any]) -> list[dict]:
    """A tool calling turn as plain text, for history Gemini would not take
    as function calls."""
    lines = []
    text = _text_of(message.get("content"))
    if text:
        lines.append(text)
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            fn = call.get("function") or {}
            lines.append(f"Called tool {fn.get('name') or ''} with arguments "
                         f"{json.dumps(_arguments(fn.get('arguments')))}")
    body = "\n".join(lines)
    return [{"type": "model_output", "content": [{"type": "text", "text": body}]}] if body else []


def build_input(messages: list[dict], *, flatten_tools: bool = False) -> tuple[str, list[dict]]:
    """(system instruction, input steps) for OpenAI shaped history.

    ``flatten_tools`` sends earlier tool calls and results as plain text. It
    is the retry for history Gemini refuses as function calls (for example
    one built by another provider, which carries no recorded reasoning)."""
    system: list[str] = []
    steps: list[dict] = []
    names: dict[str, str] = {}
    for message in messages:
        role = message.get("role")
        if role == "system":
            text = _text_of(message.get("content"))
            if text:
                system.append(text)
        elif role == "user":
            steps.append({"type": "user_input", "content": _content_parts(message.get("content"))})
        elif role == "assistant":
            if not message.get("tool_calls"):
                text = _text_of(message.get("content"))
                if text:
                    steps.append({"type": "model_output",
                                  "content": [{"type": "text", "text": text}]})
            elif flatten_tools:
                for call in message["tool_calls"]:
                    if isinstance(call, dict):
                        names[str(call.get("id") or "")] = str(
                            (call.get("function") or {}).get("name") or "")
                steps.extend(_flattened_assistant(message))
            else:
                steps.extend(_assistant_steps(message, names))
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            name = str(message.get("name") or names.get(call_id) or "")
            content = _text_of(message.get("content"))
            if flatten_tools:
                steps.append({"type": "user_input", "content": [
                    {"type": "text", "text": f"Result of tool {name}: {content}"}]})
            else:
                # A plain string. A list of content parts is a "multimodal"
                # function response, which Gemini 2.5 models refuse (HTTP 400).
                step: dict[str, Any] = {
                    "type": "function_result", "call_id": call_id, "result": content}
                if name:
                    step["name"] = name
                steps.append(step)
    return "\n".join(system), steps


def _clean_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean_schema(v) for k, v in value.items() if k not in _SCHEMA_DROP}
    if isinstance(value, list):
        return [_clean_schema(v) for v in value]
    return value


def build_tools(tools: tuple[ToolSpec, ...]) -> list[dict]:
    out = []
    for tool in tools:
        declaration: dict[str, Any] = {
            "type": "function", "name": tool.name, "description": tool.description}
        if tool.parameters:
            declaration["parameters"] = _clean_schema(dict(tool.parameters))
        out.append(declaration)
    return out


def has_tool_turns(messages: list[dict]) -> bool:
    return any(m.get("role") == "tool" or m.get("tool_calls") for m in messages)


def build_request(request: ChatRequest, model: str, *, flatten_tools: bool = False,
                  thinking: bool = True) -> dict[str, Any]:
    """The POST body for one stateless interaction."""
    system, steps = build_input(
        [m.to_openai() for m in request.messages], flatten_tools=flatten_tools)
    level = thinking_level_for(model)
    config: dict[str, Any] = {
        "max_output_tokens": request.max_tokens + (THINKING_TOKEN_ROOM if level else 0)}
    if level and thinking:
        config["thinking_level"] = level
    if takes_temperature(model) and request.temperature is not None:
        config["temperature"] = request.temperature
    body: dict[str, Any] = {
        "model": model, "input": steps, "store": False, "generation_config": config}
    if system:
        body["system_instruction"] = system
    if request.tools:
        body["tools"] = build_tools(request.tools)
    return body


# ── Response ────────────────────────────────────────────────────────────────

def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage(data: Mapping[str, Any]) -> Usage:
    """Reported token counts. Thought tokens are billed as output, so they
    are counted with it."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return Usage()
    out, thought = _count(usage.get("total_output_tokens")), _count(usage.get("total_thought_tokens"))
    return Usage(
        input_tokens=_count(usage.get("total_input_tokens")),
        output_tokens=None if out is None and thought is None else (out or 0) + (thought or 0),
    )


def _failure(data: Mapping[str, Any], status: str) -> ProviderError:
    """A failed or cancelled interaction. Only fixed phrases leave here."""
    errors = data.get("errors")
    text = " ".join(
        f"{e.get('code') or ''} {e.get('message') or ''}"
        for e in errors if isinstance(e, dict)) if isinstance(errors, list) else ""
    if status == "cancelled":
        return ProviderError(ProviderErrorKind.CANCELLED, PROVIDER)
    if has_safety_words(text):
        return ProviderError(ProviderErrorKind.INVALID_REQUEST, PROVIDER, detail=SAFETY_DETAIL)
    if "malformed" in text.lower():
        return ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, PROVIDER,
                             detail=MALFORMED_CALL_DETAIL)
    return ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, PROVIDER,
                         detail="the interaction failed")


def gemini_raw(text: str, tool_calls: tuple[ToolCall, ...]) -> dict:
    """The sanitized ``raw`` compatibility value: Gemini shaped steps rebuilt
    from normalized data only (no thought steps, signatures or usage)."""
    steps: list[dict] = []
    if text:
        steps.append({"type": "model_output", "content": [{"type": "text", "text": text}]})
    for call in tool_calls:
        steps.append({"type": "function_call", "id": call.id, "name": call.name,
                      "arguments": dict(call.args)})
    return {"steps": steps}


def parse_interaction(data: Any, *, model: str) -> ChatResponse:
    """Normalize a completed interaction: the visible text, the function
    calls (carrying the recorded model steps) and the usage. Thought
    summaries are never read."""
    if not isinstance(data, dict):
        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, PROVIDER)
    status = str(data.get("status") or "").lower()
    if status in ("failed", "cancelled"):
        raise _failure(data, status)
    if status in ("in_progress", "queued"):
        raise ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, PROVIDER,
                            detail="the interaction did not finish")
    steps = data.get("steps")
    if not isinstance(steps, list):
        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, PROVIDER)

    text_parts: list[str] = []
    pending: list[tuple[str, str, dict]] = []
    recorded: list[dict] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        kind = step.get("type")
        if kind == "model_output":
            content = step.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                text_parts.extend(
                    str(p.get("text") or "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text")
        elif kind == "function_call":
            arguments = step.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments else {}
                except ValueError as exc:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE, PROVIDER,
                        detail="the provider returned invalid tool arguments") from exc
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE, PROVIDER,
                    detail="the provider returned non-object tool arguments")
            pending.append((str(step.get("id") or f"call_{len(pending)}"),
                            str(step.get("name") or ""), arguments))
        if kind in ("thought", "model_output", "function_call"):
            recorded.append(step)

    extra: Optional[dict] = None
    if pending and recorded and len(json.dumps(recorded)) <= MAX_RECORDED_CHARS:
        # Replayed with the first call: the id of each function_call step
        # stays what the model sent, so the list still matches the calls.
        extra = {"google": {"steps": recorded}}
    calls = tuple(
        ToolCall(id=call_id, name=name, args=args, extra_content=extra if i == 0 else None)
        for i, (call_id, name, args) in enumerate(pending))
    text = visible_text("".join(text_parts))
    return ChatResponse(
        text=text, tool_calls=calls, usage=_usage(data), provider=PROVIDER,
        model=model, raw=gemini_raw(text, calls))
