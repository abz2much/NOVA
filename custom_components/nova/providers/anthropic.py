"""Anthropic adapter: the Messages API, reached through the anthropic SDK."""
from __future__ import annotations

import json
from typing import Any, ClassVar, Optional

from .base import LLMProvider
from .capabilities import SDK_CLIENT_POLICY, ProviderCapabilities, anthropic_vision
from .errors import ProviderConfigurationError, ProviderErrorKind, error_text
from .models import ChatRequest, ChatResponse, ToolCall, Usage
from .reasoning import visible_text

_DEFAULT_IMAGE_TYPE = "image/jpeg"


def _system_text(content) -> str:
    """A system message's text. A list of parts keeps only its text parts,
    joined with spaces."""
    if not isinstance(content, list):
        return content
    return " ".join(
        part.get("text", "") for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def _tool_result_turn(message: dict, turns: list[dict]) -> None:
    """Add a tool result to ``turns``.

    Anthropic wants every result for one assistant turn's tool calls, which
    may have run in parallel, inside a single user message. A result
    therefore joins the last turn when that turn already holds only tool
    results."""
    block = {
        "type": "tool_result",
        "tool_use_id": message.get("tool_call_id", ""),
        "content": message.get("content") or "",
    }
    last = turns[-1] if turns else None
    holds_only_results = bool(
        last and last["role"] == "user"
        and isinstance(last["content"], list) and last["content"]
        and all(item.get("type") == "tool_result" for item in last["content"])
    )
    if holds_only_results:
        last["content"].append(block)
    else:
        turns.append({"role": "user", "content": [block]})


def _tool_use_input(raw) -> Any:
    """The arguments of one call as a dict. Text is parsed as JSON, and
    anything that is not valid JSON counts as no arguments."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            raw = {}
    return raw or {}


def _assistant_tool_turn(message: dict) -> dict:
    """An assistant message that made tool calls: its text, if any, then one
    tool_use block for each call."""
    blocks = []
    text = message.get("content") or ""
    if text:
        blocks.append({"type": "text", "text": text})
    for call in message["tool_calls"]:
        function = call.get("function", {}) or {}
        blocks.append({
            "type": "tool_use",
            "id": call.get("id", ""),
            "name": function.get("name", ""),
            "input": _tool_use_input(function.get("arguments", {})),
        })
    return {"role": "assistant", "content": blocks}


def _split_data_url(url: str) -> tuple[str, str]:
    """(media type, base64 data) of a ``data:`` URL. A URL that will not split
    gives the default image type and no data."""
    try:
        header, data = url.split(",", 1)
        media_type = header.split(":", 1)[1].split(";", 1)[0]
    except Exception:
        return _DEFAULT_IMAGE_TYPE, ""
    return media_type or _DEFAULT_IMAGE_TYPE, data


def _image_block(part: dict) -> Optional[dict]:
    """Anthropic's form of an OpenAI ``image_url`` part, or None when the part
    has no address."""
    url = (part.get("image_url") or {}).get("url", "") or ""
    if url.startswith("data:"):
        media_type, data = _split_data_url(url)
        return {"type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data}}
    if url:
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


class AnthropicProvider(LLMProvider):
    name: ClassVar[str] = "anthropic"
    concurrency = SDK_CLIENT_POLICY
    requires_credential = True

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        super().__init__(api_key, model, base_url)
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise ProviderConfigurationError(
                ProviderErrorKind.PROVIDER_UNAVAILABLE, self.name,
                detail="the anthropic package is not installed") from exc
        options = {"api_key": api_key}
        if base_url:
            options["base_url"] = base_url
        self._client = Anthropic(**options)

    # ── Building the request ────────────────────────────────────────────────

    @classmethod
    def _to_anthropic_messages(cls, messages: list[dict]) -> tuple[str, list[dict]]:
        """Separate the system text from the conversation and translate the
        OpenAI-shaped history Nova keeps.

        Anthropic has no "tool" role and no top-level tool_calls. Tool calls
        and their results are content blocks inside ordinary assistant and user
        turns, and images use a different block than OpenAI's image_url."""
        system = ""
        turns: list[dict] = []
        for message in messages:
            role = message["role"]
            if role == "system":
                text = _system_text(message["content"])
                system = text if not system else system + "\n\n" + text
            elif role == "tool":
                _tool_result_turn(message, turns)
            elif role == "assistant" and message.get("tool_calls"):
                turns.append(_assistant_tool_turn(message))
            else:
                turns.append({
                    "role": role,
                    "content": cls._to_anthropic_content(message["content"]),
                })
        return system, turns

    @staticmethod
    def _to_anthropic_content(content):
        """Translate OpenAI-style message content. The camera pipeline sends
        {"type": "image_url", ...} parts, which Anthropic wants as
        {"type": "image", "source": {...}}. Strings and text parts are kept as
        they are; a part of any other type is passed through unchanged."""
        if not isinstance(content, list):
            return content
        converted = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "image_url":
                block = _image_block(part)
                if block is not None:
                    converted.append(block)
            elif kind == "text":
                converted.append({"type": "text", "text": part.get("text", "")})
            else:
                converted.append(part)
        return converted

    def _request_kwargs(self, request: ChatRequest) -> dict[str, Any]:
        system, turns = self._to_anthropic_messages(
            [message.to_openai() for message in request.messages])
        body: dict[str, Any] = {
            "model": request.model or self.model,
            "system": system,
            "messages": turns,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.tools:
            body["tools"] = [
                {"name": tool.name, "description": tool.description,
                 "input_schema": tool.parameters}
                for tool in request.tools
            ]
        return body

    # ── Running it ──────────────────────────────────────────────────────────

    def _create(self, body: dict[str, Any]):
        """Send the request. If the model refuses `temperature` as deprecated,
        send it once more without it.

        Newer Claude generations answer 400 to a temperature setting where
        older ones ignored it, and they changed one model at a time, so the
        retry reacts to the error text instead of listing model ids."""
        try:
            return self._client.messages.create(**body)
        except Exception as exc:
            text = error_text(exc).lower()
            refused = "temperature" in text and "deprecated" in text
            if "temperature" not in body or not refused:
                raise
            body.pop("temperature")
            return self._client.messages.create(**body)

    def complete(self, request: ChatRequest) -> ChatResponse:
        body = self._request_kwargs(request)
        reply = self._create(body)
        pieces: list[str] = []
        calls: list[ToolCall] = []
        # Thinking and redacted_thinking blocks are hidden reasoning. Only
        # text and tool_use blocks are ever read.
        for block in reply.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                pieces.append(block.text)
            elif kind == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=str(block.id), name=str(block.name), args=args))
        used = getattr(reply, "usage", None)
        text = visible_text("".join(pieces))
        tool_calls = tuple(calls)
        return ChatResponse(
            text=text,
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=getattr(used, "input_tokens", None) if used is not None else None,
                output_tokens=getattr(used, "output_tokens", None) if used is not None else None,
            ),
            provider=self.name,
            model=str(body["model"]),
            raw=anthropic_raw(text, tool_calls),
        )

    def capabilities(self, model: Optional[str] = None) -> ProviderCapabilities:
        return ProviderCapabilities(vision=anthropic_vision(model or self.model))


def anthropic_raw(text: str, tool_calls: tuple[ToolCall, ...]) -> dict:
    """The cleaned ``raw`` value kept for compatibility: an Anthropic-style
    message rebuilt only from the normalised reply, so it holds no thinking
    blocks, ids or usage."""
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    for call in tool_calls:
        content.append({"type": "tool_use", "id": call.id, "name": call.name,
                        "input": dict(call.args)})
    return {"role": "assistant", "content": content}
