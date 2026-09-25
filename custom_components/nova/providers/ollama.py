"""Native Ollama adapter (``/api/chat``).

Ollama's OpenAI-compatible endpoint does not reliably forward native controls
such as ``think``. The native endpoint also avoids an OpenAI SDK dependency
for a fully local installation, so this adapter is not an OpenAI-compatible
one.
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Any, ClassVar, Optional
from urllib.error import HTTPError
from urllib.request import Request

from .base import LLMProvider
from .capabilities import OLLAMA_POLICY, ProviderCapabilities, ollama_vision
from .destinations import build_safe_opener, check_url
from .errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderErrorKind,
    ProviderRequestError,
    normalize_error,
)
from .models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ImagePart,
    TextPart,
    ToolCall,
    Usage,
)
from .reasoning import visible_text

# Tuning for local inference: keep the model resident so we don't pay the
# load-into-VRAM cost on every call; raise the context window well above
# Ollama's 2048 default (Nova's system prompt + knowledge + history need the
# room, and a too-small window silently truncates); and allow a generous
# timeout since the first token can lag while a cold model loads.
OLLAMA_KEEP_ALIVE = "30m"
OLLAMA_NUM_CTX = 8192
OLLAMA_TIMEOUT = 120.0  # seconds
# Largest chat response body read from a local server.
OLLAMA_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# Stateless: urllib handlers only. Every redirect target is checked and
# credentials never cross an origin change (destinations.SafeRedirectHandler).
_OPENER = build_safe_opener(resolve=True)


def _urlopen(request: Request, timeout: float):
    """The single network seam for native Ollama calls."""
    return _OPENER.open(request, timeout=timeout)


class OllamaProvider(LLMProvider):
    """Ollama's native ``/api/chat`` transport."""

    name: ClassVar[str] = "ollama"
    concurrency = OLLAMA_POLICY
    requires_credential = False

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        native_base = self._normalize_url(base_url)
        if not native_base:
            raise ProviderConfigurationError(
                ProviderErrorKind.INVALID_ENDPOINT, self.name,
                detail="Ollama endpoint is not configured")
        check_url(native_base)
        super().__init__(api_key, model, native_base)

    @staticmethod
    def _normalize_url(base_url: Optional[str]) -> Optional[str]:
        """Return the native API root, accepting a legacy trailing ``/v1``."""
        base = str(base_url or "").strip().rstrip("/")
        if not base:
            return None
        if base.endswith("/v1"):
            base = base[:-3].rstrip("/")
        return base

    # ── Request translation ─────────────────────────────────────────────────

    @staticmethod
    def _content_to_native(message: ChatMessage) -> tuple[str, list[str]]:
        """Convert text/image parts to Ollama message fields."""
        if isinstance(message.content, str):
            return message.content, []
        text_parts: list[str] = []
        images: list[str] = []
        for part in message.content:
            if isinstance(part, TextPart):
                text_parts.append(part.text)
            elif isinstance(part, ImagePart):
                if not part.is_inline or "," not in part.url:
                    raise ValueError("Ollama images must be inline data URLs")
                payload = part.url.split(",", 1)[1]
                try:
                    base64.b64decode(payload, validate=True)
                except Exception as exc:
                    raise ValueError("Ollama image contains invalid base64 data") from exc
                images.append(payload)
        return "\n".join(p for p in text_parts if p), images

    @classmethod
    def _messages_to_native(cls, messages) -> list[dict]:
        """Translate history (typed, or OpenAI-shaped dicts) to Ollama chat
        messages."""
        out: list[dict] = []
        tool_names: dict[str, str] = {}
        for message in messages:
            if not isinstance(message, ChatMessage):
                message = ChatMessage.from_openai(message)
            content, images = cls._content_to_native(message)
            native: dict[str, Any] = {"role": message.role, "content": content}
            if images:
                native["images"] = images
            if message.tool_calls:
                native_calls = []
                for call in message.tool_calls:
                    if call.arguments_error == "invalid_json":
                        raise ValueError("tool call arguments are not valid JSON")
                    if call.arguments_error == "not_object":
                        raise ValueError("tool call arguments must be a JSON object")
                    if call.id and call.name:
                        tool_names[call.id] = call.name
                    native_calls.append({
                        "function": {"name": call.name, "arguments": dict(call.args)},
                    })
                native["tool_calls"] = native_calls
            if message.role == "tool":
                tool_name = tool_names.get(str(message.tool_call_id or ""))
                if tool_name:
                    native["tool_name"] = tool_name
            out.append(native)
        return out

    @staticmethod
    def _num_ctx() -> int:
        num_ctx = OLLAMA_NUM_CTX
        try:
            from .. import nova_config
            num_ctx = int(nova_config.get("ollama_num_ctx", OLLAMA_NUM_CTX) or OLLAMA_NUM_CTX)
            if num_ctx < 512:
                num_ctx = OLLAMA_NUM_CTX
        except Exception:
            num_ctx = OLLAMA_NUM_CTX
        return num_ctx

    @staticmethod
    def _visible_text(content: Any) -> str:
        return visible_text(content)

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": self._messages_to_native(request.messages),
            "stream": False,
            "think": False,
            "keep_alive": OLLAMA_KEEP_ALIVE,
            "options": {
                "num_ctx": self._num_ctx(),
                "num_predict": request.max_tokens,
                "temperature": request.temperature,
            },
        }
        if request.tools:
            payload["tools"] = [t.to_openai() for t in request.tools]
        return payload

    # ── Execution ───────────────────────────────────────────────────────────

    def complete(self, request: ChatRequest) -> ChatResponse:
        try:
            payload = self._payload(request)
        except ValueError as exc:
            # Nova's own fixed translation messages (never provider text).
            raise ProviderRequestError(
                ProviderErrorKind.INVALID_REQUEST, self.name, detail=str(exc)) from exc
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        http_request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with _urlopen(http_request, timeout=OLLAMA_TIMEOUT) as response:
                raw_bytes = response.read(OLLAMA_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise self._http_error(exc) from exc
        if len(raw_bytes) > OLLAMA_MAX_RESPONSE_BYTES:
            raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, self.name,
                                detail="Ollama returned an oversized response")
        return self._parse(raw_bytes, str(payload["model"]))

    def _http_error(self, exc: HTTPError) -> ProviderError:
        """Classify an HTTP failure. The body is read (bounded) only to tell
        a missing model from a missing endpoint; it never enters a message."""
        error = normalize_error(exc, self.name)
        try:
            body = exc.read(300).decode("utf-8", errors="replace").lower()
        except Exception:
            body = ""
        if exc.code == 404 and "model" in body:
            error = ProviderError(ProviderErrorKind.MODEL_NOT_FOUND, self.name, status=404)
        return error

    def _parse(self, raw_bytes: bytes, model: str) -> ChatResponse:
        def malformed(detail: str) -> ProviderError:
            return ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, self.name, detail=detail)

        try:
            data = json.loads(raw_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise malformed("Ollama returned an invalid JSON response") from exc
        if not isinstance(data, dict):
            raise malformed("Ollama returned an invalid response")
        if data.get("error"):
            raise ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, self.name,
                                detail="Ollama reported an error")
        message = data.get("message") or {}
        if not isinstance(message, dict):
            raise malformed("Ollama returned an invalid message")
        calls = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, dict):
                raise malformed("Ollama returned an invalid tool call")
            function = item.get("function") or {}
            if not isinstance(function, dict) or not function.get("name"):
                raise malformed("Ollama returned a tool call without a name")
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise malformed("Ollama returned invalid tool arguments") from exc
            if not isinstance(arguments, dict):
                raise malformed("Ollama returned non-object tool arguments")
            calls.append(ToolCall(
                id=f"call_ollama_{uuid.uuid4().hex}",
                name=str(function.get("name") or ""),
                args=arguments,
            ))
        # Only the visible content is read: Ollama's separate ``thinking``
        # field is ignored and a leaked envelope in content is removed.
        text = visible_text(message.get("content"))
        tool_calls = tuple(calls)
        return ChatResponse(
            text=text,
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=_count(data.get("prompt_eval_count")),
                output_tokens=_count(data.get("eval_count")),
            ),
            provider=self.name,
            model=model,
            raw=ollama_raw(text, tool_calls),
        )

    def capabilities(self, model: Optional[str] = None) -> ProviderCapabilities:
        return ProviderCapabilities(vision=ollama_vision(model or self.model))


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def ollama_raw(text: str, tool_calls: tuple[ToolCall, ...]) -> dict:
    """The sanitized ``raw`` compatibility value: Ollama's native message
    shape rebuilt from normalized data only (no thinking, timings or
    context)."""
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = [
            {"function": {"name": c.name, "arguments": dict(c.args)}} for c in tool_calls]
    return {"message": message}
