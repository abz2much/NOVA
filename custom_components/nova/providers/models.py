"""Typed provider contracts.

Every model here is a frozen, slotted dataclass built from the standard
library only. Adapters translate between these models and one provider's wire
format; everything outside the provider package sees only these models (or
the legacy dictionaries built from them by the compatibility façade).

Nothing in this module carries hidden reasoning. A ChatResponse holds the
visible answer text, normalized tool calls and provider-reported usage, plus a
``raw`` compatibility value that each adapter rebuilds from that normalized
data, never from the provider's own response object.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping, Optional, Sequence

MessageRole = Literal["system", "user", "assistant", "tool"]
DataCategory = Literal["text", "vision"]
ExecutionLocation = Literal["local", "cloud", "unknown"]

DATA_CATEGORIES: frozenset[str] = frozenset({"text", "vision"})
EXECUTION_LOCATIONS: frozenset[str] = frozenset({"local", "cloud", "unknown"})


class Capability(str, Enum):
    """What a provider/model pairing can accept."""

    TEXT = "text"
    VISION = "vision"
    TOOLS = "tools"


# ── Message content ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class TextPart:
    text: str

    def to_openai(self) -> dict:
        return {"type": "text", "text": self.text}


@dataclass(frozen=True, slots=True)
class ImagePart:
    """An image reference: an inline ``data:`` URL or a remote URL.

    ``repr`` never shows the payload, so an image can't end up in a log line
    by way of a model's repr."""

    url: str = field(repr=False)
    detail: Optional[str] = None

    @property
    def is_inline(self) -> bool:
        return self.url.startswith("data:")

    def inline_parts(self) -> tuple[str, str]:
        """(media_type, base64 payload) of an inline image. Raises ValueError
        when the URL is not an inline data URL."""
        if not self.is_inline or "," not in self.url:
            raise ValueError("image is not an inline data URL")
        header, payload = self.url.split(",", 1)
        media_type = header.split(":", 1)[1].split(";", 1)[0] or "image/jpeg"
        return media_type, payload

    def to_openai(self) -> dict:
        image_url: dict[str, Any] = {"url": self.url}
        if self.detail is not None:
            image_url["detail"] = self.detail
        return {"type": "image_url", "image_url": image_url}


@dataclass(frozen=True, slots=True)
class OpaquePart:
    """A content part Nova does not interpret. It is passed through to
    providers that accept arbitrary parts, exactly as the caller built it."""

    data: Mapping[str, Any]

    def to_openai(self) -> dict:
        return dict(self.data)


ContentPart = TextPart | ImagePart | OpaquePart


def _part_from_openai(part: Any) -> ContentPart | None:
    if not isinstance(part, dict):
        return None
    ptype = part.get("type")
    if ptype == "text":
        return TextPart(str(part.get("text", "") if part.get("text") is not None else ""))
    if ptype == "image_url":
        image = part.get("image_url") or {}
        detail = image.get("detail") if isinstance(image, dict) else None
        url = str((image.get("url") if isinstance(image, dict) else "") or "")
        return ImagePart(url=url, detail=detail)
    return OpaquePart(dict(part))


# ── Tools ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call the model asked for. ``args`` is always a JSON object."""

    id: str
    name: str
    args: Mapping[str, Any]
    # Set only on a call parsed from caller-built history whose arguments were
    # not a JSON object: "invalid_json" or "not_object". Adapters decide
    # whether that is an error (native Ollama) or an empty object (Anthropic).
    arguments_error: Optional[str] = field(default=None, compare=False)

    def to_legacy(self) -> dict:
        return {"id": self.id, "name": self.name, "args": dict(self.args)}

    def to_openai(self) -> dict:
        """The OpenAI-shaped history entry for this call."""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(dict(self.args))},
        }

    @classmethod
    def from_legacy(cls, value: Mapping[str, Any], index: int = 0) -> "ToolCall":
        args = value.get("args")
        if not isinstance(args, dict):
            args = {}
        return cls(
            id=str(value.get("id") or f"call_{index}"),
            name=str(value.get("name") or ""),
            args=dict(args),
        )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool offered to the model (the OpenAI function-tool shape)."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    source: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_openai(cls, tool: Mapping[str, Any]) -> "ToolSpec":
        fn = tool.get("function") or {}
        return cls(
            name=str(fn.get("name", "")),
            description=str(fn.get("description", "")),
            parameters=fn.get("parameters") or {},
            source=dict(tool),
        )

    def to_openai(self) -> dict:
        # Callers already build complete OpenAI tool dicts; send exactly what
        # they built so OpenAI-compatible wire requests are unchanged.
        if self.source:
            return dict(self.source)
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": dict(self.parameters)}}


# ── Messages ────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One conversation message.

    ``content`` is a string or a tuple of parts. ``tool_calls`` is set on an
    assistant turn that called tools; ``tool_call_id`` on a tool result.
    ``source`` keeps the caller's original dict so OpenAI-compatible adapters
    send exactly what the caller built."""

    role: str
    content: str | tuple[ContentPart, ...] = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: Optional[str] = None
    source: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_openai(cls, message: Mapping[str, Any]) -> "ChatMessage":
        content = message.get("content", "")
        if isinstance(content, list):
            parts = tuple(p for p in (_part_from_openai(x) for x in content) if p is not None)
            typed_content: str | tuple[ContentPart, ...] = parts
        elif content is None:
            typed_content = ""
        else:
            typed_content = content if isinstance(content, str) else str(content)
        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            arguments = fn.get("arguments", {})
            error = None
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments else {}
                except (TypeError, ValueError):
                    arguments, error = {}, "invalid_json"
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments, error = {}, "not_object"
            calls.append(ToolCall(
                id=str(call.get("id") or ""),
                name=str(fn.get("name") or ""),
                args=arguments,
                arguments_error=error,
            ))
        return cls(
            role=str(message.get("role") or "user"),
            content=typed_content,
            tool_calls=tuple(calls),
            tool_call_id=(str(message["tool_call_id"])
                          if message.get("tool_call_id") is not None else None),
            source=dict(message),
        )

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return " ".join(p.text for p in self.content if isinstance(p, TextPart))

    @property
    def has_images(self) -> bool:
        return not isinstance(self.content, str) and any(
            isinstance(p, ImagePart) for p in self.content)

    def to_openai(self) -> dict:
        if self.source:
            return dict(self.source)
        out: dict[str, Any] = {"role": self.role}
        if isinstance(self.content, str):
            out["content"] = self.content
        else:
            out["content"] = [p.to_openai() for p in self.content]
        if self.tool_calls:
            out["tool_calls"] = [c.to_openai() for c in self.tool_calls]
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        return out


# ── Request and response ────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: int = 512
    temperature: float = 0.7
    model: Optional[str] = None     # per-call override; None = the adapter's model

    @classmethod
    def from_legacy(
        cls,
        messages: Sequence[Mapping[str, Any]],
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        model_override: Optional[str] = None,
    ) -> "ChatRequest":
        return cls(
            messages=tuple(ChatMessage.from_openai(m) for m in messages),
            tools=tuple(ToolSpec.from_openai(t) for t in (tools or [])),
            max_tokens=max_tokens,
            temperature=temperature,
            model=model_override,
        )

    @property
    def data_category(self) -> DataCategory:
        return "vision" if any(m.has_images for m in self.messages) else "text"


@dataclass(frozen=True, slots=True)
class Usage:
    """Provider-reported token counts. None when the provider did not report
    a field; never estimated."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """A normalized chat result.

    ``text`` is only the visible answer: hidden reasoning is removed by the
    adapter before this object exists. ``raw`` is a sanitized, plain-data
    compatibility value for the legacy dictionary; production code never
    reads it."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    provider: str = ""
    model: str = ""
    raw: Any = field(default=None, repr=False, compare=False)

    def to_legacy(self) -> dict:
        return {
            "text": self.text,
            "tool_calls": [c.to_legacy() for c in self.tool_calls],
            "raw": self.raw,
            "usage": self.usage.to_dict(),
        }

    def assistant_message(self) -> dict:
        """The OpenAI-shaped assistant history entry for this response, built
        only from normalized text and ToolCall values."""
        message: dict[str, Any] = {"role": "assistant", "content": self.text or ""}
        if self.tool_calls:
            message["tool_calls"] = [c.to_openai() for c in self.tool_calls]
        return message

    @classmethod
    def from_legacy(cls, value: Any, *, provider: str = "", model: str = "") -> "ChatResponse":
        """Normalize a legacy dictionary (or a test double's reply). The
        caller's ``raw`` is dropped, never carried forward."""
        if not isinstance(value, dict):
            text = (getattr(value, "content", None) or getattr(value, "text", None)
                    or ("" if value is None else str(value)))
            return cls(text=str(text), provider=provider, model=model)
        usage = value.get("usage") or {}
        calls = tuple(
            ToolCall.from_legacy(c, i)
            for i, c in enumerate(value.get("tool_calls") or [])
            if isinstance(c, dict))
        return cls(
            text=str(value.get("text") or ""),
            tool_calls=calls,
            usage=Usage(
                input_tokens=_int_or_none(usage.get("input_tokens")),
                output_tokens=_int_or_none(usage.get("output_tokens")),
            ),
            provider=provider,
            model=model,
        )


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


# ── Models and discovery ────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Display-safe metadata a provider reported for one model."""

    id: str
    capabilities: tuple[str, ...] = ()
    family: Optional[str] = None
    quantization: Optional[str] = None
    size: Optional[int] = None
    context_length: Optional[int] = None

    def to_dict(self) -> dict:
        row: dict[str, Any] = {"id": self.id, "capabilities": list(self.capabilities)}
        if self.family:
            row["family"] = self.family
        if self.quantization:
            row["quantization"] = self.quantization
        if self.size is not None:
            row["size"] = self.size
        if self.context_length is not None:
            row["context_length"] = self.context_length
        return row


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    """A server-selected model-list request. ``headers`` carry only the
    destination's own credential and are hidden from repr."""

    provider: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    pagination: Optional[str] = None
    fixed_destination: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    models: tuple[str, ...]
    truncated: bool
    details: tuple[ModelInfo, ...] = ()

    def as_tuple(self) -> tuple[list[str], bool, list[dict]]:
        return list(self.models), self.truncated, [d.to_dict() for d in self.details]
