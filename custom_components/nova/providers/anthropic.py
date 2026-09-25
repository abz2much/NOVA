"""Anthropic adapter (Messages API through the anthropic SDK)."""
from __future__ import annotations

import json
from typing import Any, ClassVar, Optional

from .base import LLMProvider
from .capabilities import SDK_CLIENT_POLICY, ProviderCapabilities, anthropic_vision
from .errors import ProviderConfigurationError, ProviderErrorKind, error_text
from .models import ChatRequest, ChatResponse, ToolCall, Usage
from .reasoning import visible_text


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
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = Anthropic(**kwargs)

    # ── Request translation ─────────────────────────────────────────────────

    @classmethod
    def _to_anthropic_messages(cls, messages: list[dict]) -> tuple[str, list[dict]]:
        """Split system text from the conversation and translate the
        OpenAI-shaped history Nova builds.

        Anthropic has no "tool" role or top-level tool_calls field: tool use
        and results are content blocks on ordinary user/assistant turns, and
        it uses a different image block format than OpenAI's image_url."""
        system = ""
        chat_msgs: list[dict] = []
        for m in messages:
            role = m["role"]
            if role == "system":
                sys_c = m["content"]
                if isinstance(sys_c, list):
                    sys_c = " ".join(
                        p.get("text", "") for p in sys_c
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                system = sys_c if not system else system + "\n\n" + sys_c
            elif role == "tool":
                # A tool_result block, sent back as a *user* turn. Anthropic
                # requires every tool_result from one assistant turn's
                # (possibly parallel) tool_use calls to land in a single user
                # message, so merge onto the previous one when it's already an
                # all-tool_result user turn.
                block = {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m.get("content") or "",
                }
                prev = chat_msgs[-1] if chat_msgs else None
                if (prev and prev["role"] == "user"
                        and isinstance(prev["content"], list) and prev["content"]
                        and all(b.get("type") == "tool_result" for b in prev["content"])):
                    prev["content"].append(block)
                else:
                    chat_msgs.append({"role": "user", "content": [block]})
            elif role == "assistant" and m.get("tool_calls"):
                # Assistant turn with tool calls -> a text block (if any)
                # plus one tool_use block per call.
                content = []
                text = m.get("content") or ""
                if text:
                    content.append({"type": "text", "text": text})
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {}) or {}
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args) if args else {}
                        except (TypeError, ValueError):
                            args = {}
                    content.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args or {},
                    })
                chat_msgs.append({"role": "assistant", "content": content})
            else:
                chat_msgs.append({
                    "role": role,
                    "content": cls._to_anthropic_content(m["content"]),
                })
        return system, chat_msgs

    @staticmethod
    def _to_anthropic_content(content):
        """Convert OpenAI-style message content to Anthropic's format. The
        camera pipeline sends images as {"type":"image_url",...}; Anthropic
        wants {"type":"image","source":{"type":"base64",...}}. Plain strings
        and text blocks pass through unchanged."""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return content
        out = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "image_url":
                url = (part.get("image_url") or {}).get("url", "") or ""
                if url.startswith("data:"):
                    try:
                        header, b64 = url.split(",", 1)
                        media_type = header.split(":", 1)[1].split(";", 1)[0] or "image/jpeg"
                    except Exception:
                        media_type, b64 = "image/jpeg", ""
                    out.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    })
                elif url:
                    out.append({"type": "image", "source": {"type": "url", "url": url}})
            elif ptype == "text":
                out.append({"type": "text", "text": part.get("text", "")})
            else:
                out.append(part)
        return out

    def _request_kwargs(self, request: ChatRequest) -> dict[str, Any]:
        system, chat_msgs = self._to_anthropic_messages(
            [m.to_openai() for m in request.messages])
        kwargs: dict[str, Any] = {
            "model": request.model or self.model,
            "system": system,
            "messages": chat_msgs,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in request.tools
            ]
        return kwargs

    # ── Execution ───────────────────────────────────────────────────────────

    def complete(self, request: ChatRequest) -> ChatResponse:
        kwargs = self._request_kwargs(request)
        try:
            resp = self._client.messages.create(**kwargs)
        except Exception as exc:
            # Newer Claude generations reject `temperature` outright with a
            # 400 instead of ignoring it, rolled out model by model, so retry
            # once without it rather than hardcoding model ids.
            msg = error_text(exc).lower()
            if "temperature" in kwargs and "temperature" in msg and "deprecated" in msg:
                kwargs.pop("temperature")
                resp = self._client.messages.create(**kwargs)
            else:
                raise
        text_parts = []
        calls = []
        # Only text and tool_use blocks are read: thinking and
        # redacted_thinking blocks are hidden reasoning and never surface.
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=str(block.id), name=str(block.name), args=args))
        u = getattr(resp, "usage", None)
        text = visible_text("".join(text_parts))
        tool_calls = tuple(calls)
        return ChatResponse(
            text=text,
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=getattr(u, "input_tokens", None) if u is not None else None,
                output_tokens=getattr(u, "output_tokens", None) if u is not None else None,
            ),
            provider=self.name,
            model=str(kwargs["model"]),
            raw=anthropic_raw(text, tool_calls),
        )

    def capabilities(self, model: Optional[str] = None) -> ProviderCapabilities:
        return ProviderCapabilities(vision=anthropic_vision(model or self.model))


def anthropic_raw(text: str, tool_calls: tuple[ToolCall, ...]) -> dict:
    """The sanitized ``raw`` compatibility value: an Anthropic-shaped message
    rebuilt from normalized data only (no thinking blocks, ids or usage)."""
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    for call in tool_calls:
        content.append({"type": "tool_use", "id": call.id, "name": call.name,
                        "input": dict(call.args)})
    return {"role": "assistant", "content": content}
