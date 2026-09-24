"""
Nova — LLM provider abstraction.

The agent doesn't know or care which LLM backend it's talking to. This
module gives every backend the same interface so swapping providers is a
configuration change rather than a code rewrite.

Supported backends today:
  - groq        (default — fast, free tier, OpenAI-compatible API)
  - openai      (OpenAI direct, or any OpenAI-compatible endpoint)
  - ollama      (local, self-hosted via Ollama's native chat API)
  - anthropic   (Claude API)
  - custom      (any OpenAI-compatible endpoint with a base_url)

Adding a new backend: subclass LLMProvider and register it in PROVIDERS.
Everything else — conversation, camera, briefings, sentinel — uses the
uniform interface and doesn't need changes.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
import time as _time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_LOGGER = logging.getLogger(__name__)


# ─── Standard response shape ─────────────────────────────────────────────────
#
# Every backend returns this structure:
#   {
#     "text": "...",                      # response text (may be empty if tools called)
#     "tool_calls": [                     # list of requested tool calls (or empty)
#       {"id": "call_xyz", "name": "...", "args": {...}},
#     ],
#     "raw": <provider-specific object>,  # for debugging / feeding back
#     "usage": {"input_tokens": int|None, "output_tokens": int|None},  # Phase 5:
#       whatever the provider itself reported — never estimated. None per
#       field when the server didn't report it (e.g. some self-hosted
#       OpenAI-compatible endpoints omit usage entirely).
#   }


def _openai_style_usage(resp) -> dict:
    """Token usage from an OpenAI-compatible chat completion response (Groq,
    OpenAI, and custom OpenAI-compatible endpoints share this shape).
    None per field when the response has no usage object, or the field
    itself is absent — never estimated."""
    u = getattr(resp, "usage", None)
    return {
        "input_tokens": getattr(u, "prompt_tokens", None) if u is not None else None,
        "output_tokens": getattr(u, "completion_tokens", None) if u is not None else None,
    }


class LLMProvider(ABC):
    """Abstract base for all LLM backends."""

    name: str = "abstract"

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        model_override: Optional[str] = None,
    ) -> dict:
        """Run a synchronous chat completion. Returns standardised dict.

        model_override lets a caller request a different model for this single
        call (e.g. a vision-capable model for image analysis) without needing
        to create a new provider instance.
        """
        ...

    def supports_vision(self) -> bool:
        """Whether this backend + model can take image inputs."""
        return False

    def supports_tools(self) -> bool:
        """Whether this backend supports function/tool calling."""
        return True


# ─── Groq (default) ──────────────────────────────────────────────────────────

class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        super().__init__(api_key, model, base_url)
        try:
            from groq import Groq
        except ImportError as exc:
            raise RuntimeError(
                "groq package not installed — `pip install groq`"
            ) from exc
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = Groq(**kwargs)

    def chat(self, messages, tools=None, max_tokens=512, temperature=0.7, model_override=None):
        kwargs: dict[str, Any] = {
            "model": model_override or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        tool_calls = []
        # Check message.tool_calls directly — Groq sometimes sets
        # finish_reason="stop" even when tool_calls are present.
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "id":   tc.id,
                    "name": tc.function.name,
                    "args": json.loads(tc.function.arguments or "{}"),
                })
        return {
            "text": (choice.message.content or "").strip(),
            "tool_calls": tool_calls,
            "raw": choice.message,
            "usage": _openai_style_usage(resp),
        }

    def supports_vision(self) -> bool:
        # Groq vision-capable models all contain 'vision' in the name
        return "vision" in self.model.lower()


# ─── OpenAI / OpenAI-compatible ──────────────────────────────────────────────

class OpenAIProvider(LLMProvider):
    """OpenAI, Azure OpenAI, or any OpenAI-compatible endpoint (Ollama, vLLM, etc)."""
    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        super().__init__(api_key, model, base_url)
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "openai package not installed — `pip install openai`"
            ) from exc
        kwargs = {"api_key": api_key or "not-required"}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def chat(self, messages, tools=None, max_tokens=512, temperature=0.7, model_override=None):
        kwargs: dict[str, Any] = {
            "model": model_override or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        extra = self._extra_body()
        if extra:
            kwargs["extra_body"] = extra
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        tool_calls = []
        # Check message.tool_calls directly — some providers set
        # finish_reason="stop" even when tool_calls are present.
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "id":   tc.id,
                    "name": tc.function.name,
                    "args": json.loads(tc.function.arguments or "{}"),
                })
        return {
            "text": (choice.message.content or "").strip(),
            "tool_calls": tool_calls,
            "raw": choice.message,
            "usage": _openai_style_usage(resp),
        }

    def _extra_body(self) -> dict:
        """Provider-specific request extras. Empty for vanilla OpenAI."""
        return {}

    def supports_vision(self) -> bool:
        # Modern OpenAI models all support vision; Ollama varies by model;
        # most Gemini models are multimodal; LLaVA supports vision locally
        vision_models = (
            "gpt-4", "gpt-5", "llava", "vision",
            "gemini-2", "gemini-3", "gemini-flash", "gemini-pro",
        )
        return any(v in self.model.lower() for v in vision_models)


# ─── Ollama (local, self-hosted) ─────────────────────────────────────────────
# Tuning for local inference: keep the model resident so we don't pay the
# load-into-VRAM cost on every call; raise the context window well above
# Ollama's 2048 default (Nova's system prompt + knowledge + history need the
# room, and a too-small window silently truncates); and allow a generous
# timeout since the first token can lag while a cold model loads.
OLLAMA_KEEP_ALIVE = "30m"
OLLAMA_NUM_CTX = 8192
OLLAMA_TIMEOUT = 120.0  # seconds


class OllamaProvider(LLMProvider):
    """Ollama's native ``/api/chat`` transport.

    Ollama's OpenAI-compatible endpoint does not reliably forward native
    controls such as ``think``. The native endpoint also avoids an OpenAI SDK
    dependency for a fully local installation.
    """
    name = "ollama"

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        native_base = self._normalize_url(base_url)
        if not native_base:
            raise ValueError("Ollama endpoint is not configured")
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

    @staticmethod
    def _content_to_native(content: Any) -> tuple[str, list[str]]:
        """Convert OpenAI-style text/image blocks to Ollama message fields."""
        if isinstance(content, str):
            return content, []
        if not isinstance(content, list):
            return str(content or ""), []

        text_parts: list[str] = []
        images: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_parts.append(str(part.get("text") or ""))
                continue
            if part.get("type") != "image_url":
                continue
            url = str((part.get("image_url") or {}).get("url") or "")
            if not url.startswith("data:") or "," not in url:
                raise ValueError("Ollama images must be inline data URLs")
            payload = url.split(",", 1)[1]
            try:
                base64.b64decode(payload, validate=True)
            except Exception as exc:
                raise ValueError("Ollama image contains invalid base64 data") from exc
            images.append(payload)
        return "\n".join(p for p in text_parts if p), images

    @classmethod
    def _messages_to_native(cls, messages: list[dict]) -> list[dict]:
        """Translate Nova's OpenAI-shaped history to Ollama chat messages."""
        out: list[dict] = []
        tool_names: dict[str, str] = {}
        for message in messages:
            role = str(message.get("role") or "user")
            content, images = cls._content_to_native(message.get("content", ""))
            native: dict[str, Any] = {"role": role, "content": content}
            if images:
                native["images"] = images

            calls = message.get("tool_calls") or []
            if calls:
                native_calls = []
                for call in calls:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    arguments = function.get("arguments") or {}
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError as exc:
                            raise ValueError("tool call arguments are not valid JSON") from exc
                    if not isinstance(arguments, dict):
                        raise ValueError("tool call arguments must be a JSON object")
                    call_id = str(call.get("id") or "")
                    if call_id and name:
                        tool_names[call_id] = name
                    native_calls.append({
                        "function": {"name": name, "arguments": arguments},
                    })
                native["tool_calls"] = native_calls

            if role == "tool":
                tool_name = tool_names.get(str(message.get("tool_call_id") or ""))
                if tool_name:
                    native["tool_name"] = tool_name
            out.append(native)
        return out

    @staticmethod
    def _num_ctx() -> int:
        num_ctx = OLLAMA_NUM_CTX
        try:
            from . import nova_config
            num_ctx = int(nova_config.get("ollama_num_ctx", OLLAMA_NUM_CTX) or OLLAMA_NUM_CTX)
            if num_ctx < 512:
                num_ctx = OLLAMA_NUM_CTX
        except Exception:
            num_ctx = OLLAMA_NUM_CTX
        return num_ctx

    def chat(self, messages, tools=None, max_tokens=512, temperature=0.7,
             model_override=None):
        payload: dict[str, Any] = {
            "model": model_override or self.model,
            "messages": self._messages_to_native(messages),
            "stream": False,
            "think": False,
            "keep_alive": OLLAMA_KEEP_ALIVE,
            "options": {
                "num_ctx": self._num_ctx(),
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        if tools:
            payload["tools"] = tools

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=OLLAMA_TIMEOUT) as response:
                raw_bytes = response.read()
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                detail = str(json.loads(detail).get("error") or detail)
            except Exception:
                detail = "request failed"
            raise RuntimeError(f"Ollama HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Ollama connection failed: {exc}") from exc

        try:
            data = json.loads(raw_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("Ollama returned an invalid JSON response") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Ollama returned an invalid response")
        if data.get("error"):
            raise RuntimeError(f"Ollama error: {str(data['error'])[:300]}")

        message = data.get("message") or {}
        if not isinstance(message, dict):
            raise RuntimeError("Ollama returned an invalid message")
        tool_calls = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, dict):
                raise RuntimeError("Ollama returned an invalid tool call")
            function = item.get("function") or {}
            if not isinstance(function, dict) or not function.get("name"):
                raise RuntimeError("Ollama returned a tool call without a name")
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Ollama returned invalid tool arguments") from exc
            if not isinstance(arguments, dict):
                raise RuntimeError("Ollama returned non-object tool arguments")
            tool_calls.append({
                "id": f"call_ollama_{uuid.uuid4().hex}",
                "name": str(function.get("name") or ""),
                "args": arguments,
            })

        return {
            "text": str(message.get("content") or "").strip(),
            "tool_calls": tool_calls,
            "raw": data,
            "usage": {
                "input_tokens": data.get("prompt_eval_count"),
                "output_tokens": data.get("eval_count"),
            },
        }


# ─── Anthropic ───────────────────────────────────────────────────────────────

class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        super().__init__(api_key, model, base_url)
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise RuntimeError(
                "anthropic package not installed — `pip install anthropic`"
            ) from exc
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = Anthropic(**kwargs)

    def chat(self, messages, tools=None, max_tokens=512, temperature=0.7, model_override=None):
        # Anthropic's API splits system vs user/assistant, uses a different
        # image block format than the OpenAI-style image_url our callers
        # send, and has no "tool" role or top-level tool_calls field — tool
        # use/results are content blocks on ordinary user/assistant turns.
        # Our callers (agent.py, conversation.py) build history in the
        # OpenAI shape — {"role": "assistant", "tool_calls": [...]} and
        # {"role": "tool", "tool_call_id": ..., "content": ...} — so that
        # shape has to be translated here rather than passed through, or a
        # tool-calling turn 400s on the very next request to Anthropic.
        system = ""
        chat_msgs = []
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
                # (possibly parallel) tool_use calls to land in a single
                # user message, so merge onto the previous one when it's
                # already an all-tool_result user turn rather than adding a
                # second consecutive user message.
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
                # OpenAI-style assistant turn with tool calls -> a text
                # block (if any) plus one tool_use block per call.
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
                        "type":  "tool_use",
                        "id":    tc.get("id", ""),
                        "name":  fn.get("name", ""),
                        "input": args or {},
                    })
                chat_msgs.append({"role": "assistant", "content": content})
            else:
                chat_msgs.append({
                    "role": role,
                    "content": self._to_anthropic_content(m["content"]),
                })

        kwargs: dict[str, Any] = {
            "model": model_override or self.model,
            "system": system,
            "messages": chat_msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            # Anthropic uses a slightly different tool schema
            kwargs["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"]["description"],
                    "input_schema": t["function"]["parameters"],
                }
                for t in tools
            ]

        try:
            resp = self._client.messages.create(**kwargs)
        except Exception as exc:
            # Newer Claude generations (Opus 4.7+, Sonnet 5, ...) reject
            # `temperature` outright with a 400 instead of ignoring it, and
            # Anthropic has been rolling this out model-by-model — so retry
            # once without it rather than hardcoding which model IDs do this.
            msg = str(exc).lower()
            if "temperature" in kwargs and "temperature" in msg and "deprecated" in msg:
                kwargs.pop("temperature")
                resp = self._client.messages.create(**kwargs)
            else:
                raise

        text_parts = []
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id":   block.id,
                    "name": block.name,
                    "args": block.input,
                })
        u = getattr(resp, "usage", None)
        return {
            "text": "".join(text_parts).strip(),
            "tool_calls": tool_calls,
            "raw": resp,
            "usage": {
                "input_tokens": getattr(u, "input_tokens", None) if u is not None else None,
                "output_tokens": getattr(u, "output_tokens", None) if u is not None else None,
            },
        }

    def supports_vision(self) -> bool:
        return True  # all modern Claude models

    @staticmethod
    def _to_anthropic_content(content):
        """
        Convert OpenAI-style message content to Anthropic's format. Our camera
        pipeline sends images as {"type":"image_url","image_url":{"url":...}};
        Anthropic wants {"type":"image","source":{"type":"base64",...}}.
        Plain strings and text blocks pass through unchanged.
        """
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


# ─── Registry ────────────────────────────────────────────────────────────────

PROVIDERS = {
    "groq":      GroqProvider,
    "openai":    OpenAIProvider,
    "ollama":    OllamaProvider,    # Ollama's native /api/chat API
    "gemini":    OpenAIProvider,    # Gemini exposes an OpenAI-compatible API
    "custom":    OpenAIProvider,    # Any OpenAI-compatible endpoint
    "anthropic": AnthropicProvider,
}


_CLOUD_PROVIDERS = {"groq", "gemini", "openai", "anthropic"}
# Ollama tag syntax: name[:size-tag], e.g. gemma4:26b, llama3.3:70b-instruct.
# No cloud provider uses colon-tagged model ids, which makes this a safe tell.
_OLLAMA_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*$", re.I)


def _is_local_url(base_url: Optional[str]) -> bool:
    """Whether a configured base_url unambiguously points at a local/private
    host — loopback, an RFC1918 private address, or an mDNS .local name.
    Used only to disambiguate 'custom' providers off their OWN already-
    configured base_url; never a guess beyond what that config already says."""
    try:
        host = (urlparse(base_url or "").hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False


def execution_location(provider: "LLMProvider") -> str:
    """Where a provider's calls actually execute, for Phase 5's activity
    aggregate — determined at runtime, never guessed beyond what's already
    configured:
      - a known hosted (cloud) provider -> "cloud"
      - ollama                          -> "local"
      - anything else ("custom")        -> "unknown", unless its own
        configured base_url is unambiguously local (see _is_local_url)
    """
    name = getattr(provider, "name", "")
    if name == "ollama":
        return "local"
    if name in _CLOUD_PROVIDERS:
        return "cloud"
    if _is_local_url(getattr(provider, "base_url", None)):
        return "local"
    return "unknown"


# A default model per cloud provider, used when first-run setup detects a
# provider from the pasted key but the model field is still on its
# placeholder value — so e.g. an Anthropic key never gets silently sent
# an OpenAI/Groq-shaped model id and fails with a confusing 404.
DEFAULT_MODELS = {
    "groq":      "openai/gpt-oss-120b",
    "anthropic": "claude-sonnet-5",
    "openai":    "gpt-5-mini",
    "gemini":    "gemini-3.6-flash",
}


_SELF_HOSTED_PROVIDERS = frozenset({"ollama", "custom"})
_PROVIDER_ENDPOINT_FIELDS = {
    "ollama": "ollama_base_url",
    "custom": "custom_base_url",
}


def normalize_provider_endpoint(value: str, provider: str) -> str:
    """Validate and normalise a self-hosted provider endpoint.

    Accepts a full HTTP(S) URL or a bare host/IP. Bare Ollama endpoints get
    Ollama's default port. Credentials, query strings, and fragments are not
    accepted in endpoint URLs; authentication belongs in the provider's
    dedicated credential field.
    """
    provider = str(provider or "").strip().lower()
    if provider not in _SELF_HOSTED_PROVIDERS:
        raise ValueError(f"provider '{provider}' does not use a self-hosted endpoint")

    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > 2048:
        raise ValueError("endpoint is too long")

    had_scheme = "://" in raw
    if not had_scheme:
        # urlparse treats an unbracketed IPv6 address as several URL fields.
        # Bracket it before adding the scheme and Ollama's default port.
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            address = None
        if address is not None and address.version == 6:
            raw = f"http://[{raw}]"
        else:
            raw = f"http://{raw}"

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("endpoint must use http or https")
    if not parsed.hostname:
        raise ValueError("endpoint must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("endpoint credentials must be stored separately")
    if parsed.query or parsed.fragment or parsed.params:
        raise ValueError("endpoint must not include a query string or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("endpoint has an invalid port") from exc

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    if port is None and provider == "ollama" and not had_scheme:
        port = 11434
    authority = host if port is None else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{authority}{path}"


def resolve_provider_endpoint(
    config: dict,
    provider: str,
    tier: Optional[str] = None,
) -> Optional[str]:
    """Return the endpoint belonging to ``provider``.

    Provider-specific fields prevent an Ollama URL from being reused for a
    custom OpenAI-compatible provider (and vice versa). ``llm_base_url`` stays
    as a compatibility fallback for existing installations until the settings
    UI has saved the dedicated field.
    """
    provider = str(provider or "").strip().lower()
    if provider not in _SELF_HOSTED_PROVIDERS:
        return None

    candidates = []
    if tier:
        candidates.append(config.get(f"{tier}_base_url"))
    candidates.append(config.get(_PROVIDER_ENDPOINT_FIELDS[provider]))
    if not config.get("self_hosted_endpoints_migrated"):
        candidates.append(config.get("llm_base_url"))
    for candidate in candidates:
        if candidate not in (None, ""):
            return normalize_provider_endpoint(str(candidate), provider)
    return None


def detect_provider_from_key(api_key: str) -> str:
    """
    Guess the cloud provider from an API key's own shape, so first-run
    setup doesn't have to ask which service a pasted key belongs to.

    Key prefixes are stable, documented conventions for each vendor:
      - Anthropic: 'sk-ant-'
      - Groq:      'gsk_'
      - Google AI Studio (Gemini): 'AIza'
      - OpenAI:    'sk-' (checked after Anthropic, which also starts 'sk-')

    Falls back to 'groq' for anything unrecognized — matches the prior
    hardcoded default, so an unknown key shape still gets a clear
    'invalid_auth' from test_connection rather than a silent misroute.
    """
    k = (api_key or "").strip()
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith("gsk_"):
        return "groq"
    if k.startswith("AIza"):
        return "gemini"
    if k.startswith("sk-"):
        return "openai"
    return "groq"


def normalize_routing(provider_name: str, model: str,
                       base_url: Optional[str]) -> tuple[str, Optional[str], Optional[str]]:
    """
    (provider_name, base_url, correction_note|None). v6.47.1: a colon-tagged
    model (Ollama syntax, e.g. 'gemma4:26b') configured against a cloud
    provider is a settings mismatch that produces confusing 404s from the
    cloud API ('models/gemma4:26b is not found') — the model plainly lives
    on the local Ollama server. Route it there and say so, instead of
    faithfully forwarding a local model name to Google.
    """
    p = (provider_name or "").lower().strip()
    m = (model or "").strip()
    if p in _CLOUD_PROVIDERS and _OLLAMA_TAG_RE.match(m):
        note = (f"model '{m}' uses Ollama tag syntax but provider was "
                f"'{p}' — routing to ollama"
                + ("" if base_url else " (default base URL)"))
        return "ollama", base_url, note
    return p, base_url, None


def create_provider(
    provider_name: str,
    api_key: str,
    model: str,
    base_url: Optional[str] = None,
) -> LLMProvider:
    """
    Factory for provider instances.

    provider_name: 'groq' | 'openai' | 'gemini' | 'ollama' | 'anthropic' | 'custom'
    For 'ollama', set base_url to e.g. 'http://gpu-server:11434'.
    For 'gemini', base_url defaults to Google's OpenAI-compat endpoint.
    For 'custom', set base_url to whatever OpenAI-compatible endpoint you want.
    """
    provider_name = provider_name.lower().strip()
    provider_name, base_url, note = normalize_routing(provider_name, model, base_url)
    if note:
        _LOGGER.warning("LLM routing corrected: %s", note)
    cls = PROVIDERS.get(provider_name)
    if cls is None:
        raise ValueError(f"Unknown LLM provider '{provider_name}'")

    # Default base URLs for provider-specific cases
    if provider_name == "ollama" and not base_url:
        raise ValueError("Ollama endpoint is not configured")
    elif provider_name == "gemini" and not base_url:
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"

    return cls(api_key=api_key, model=model, base_url=base_url)


def list_providers() -> list[str]:
    """For UI dropdowns."""
    return list(PROVIDERS.keys())


# ─── Phase 2 — per-provider credential resolution (v7.107.0) ─────────────────

def resolve_provider_credential(config: dict, provider: str) -> str:
    """The credential `provider` should use, and only that provider's own.

    Every provider has a dedicated field (const.PROVIDER_API_KEY_FIELDS). Once
    an installation has migrated (ha_secrets.split_shared_credential), that
    field is always populated for whichever provider owns it and this is the
    only thing read.

    A narrow, self-expiring fallback covers an installation still waiting on
    that migration (or where it failed — write/verify failure leaves the
    legacy value in place by design): the shared legacy `api_key` is used
    ONLY when `provider` is one of the four fixed cloud providers AND it is
    also the installation's saved primary provider (`llm_provider`) — the
    sole case where that shared value is actually known to belong to this
    provider. Ollama and custom never receive it: Nova cannot prove a
    self-hosted endpoint was ever the shared key's intended destination
    (same reasoning Phase 1's model discovery already applies).
    """
    from .const import PROVIDER_API_KEY_FIELDS, CREDENTIAL_LEGACY_FALLBACK_PROVIDERS

    provider = str(provider or "").strip().lower()
    field = PROVIDER_API_KEY_FIELDS.get(provider)
    if not field:
        return ""
    val = str(config.get(field) or "")
    if val:
        return val
    if provider not in CREDENTIAL_LEGACY_FALLBACK_PROVIDERS:
        return ""
    saved_provider = str(config.get("llm_provider") or "groq").strip().lower()
    if provider != saved_provider:
        return ""
    return str(config.get("api_key") or "")


# ─── v5.2 Tiered provider selection (observer mode) ──────────────────────────
#
# Observer mode uses three distinct LLM tiers:
#   - classifier: called often, must be cheap + fast (Flash-Lite)
#   - reasoning:  called when classifier flags something (Flash)
#   - review:     called periodically for deeper reasoning (Pro)
#
# Each tier can have its own provider and model. If tier-specific keys aren't
# set, falls back to defaults defined in const.py.

def create_tier_provider(
    config: dict,
    tier: str,
) -> LLMProvider:
    """
    Build a provider for a specific observer tier.

    tier must be one of: 'classifier', 'reasoning', 'review', 'conversation'.
    Each provider resolves its own dedicated credential (see
    resolve_provider_credential) — a tier pointed at a different provider
    than the Main Agent never reuses the Main Agent's key.
    """
    from .const import (
        CONF_MODEL,
        DEFAULT_CLASSIFIER_PROVIDER, DEFAULT_CLASSIFIER_MODEL,
        DEFAULT_REASONING_PROVIDER, DEFAULT_REASONING_MODEL,
        DEFAULT_REVIEW_PROVIDER, DEFAULT_REVIEW_MODEL,
    )

    tier_defaults = {
        "classifier":   (DEFAULT_CLASSIFIER_PROVIDER, DEFAULT_CLASSIFIER_MODEL),
        "reasoning":    (DEFAULT_REASONING_PROVIDER, DEFAULT_REASONING_MODEL),
        "review":       (DEFAULT_REVIEW_PROVIDER, DEFAULT_REVIEW_MODEL),
        "conversation": (
            config.get("llm_provider", "groq"),
            config.get(CONF_MODEL, "openai/gpt-oss-120b"),
        ),
    }

    if tier not in tier_defaults:
        raise ValueError(f"Unknown tier: {tier}")

    default_provider, default_model = tier_defaults[tier]

    provider_name = config.get(f"{tier}_provider", default_provider)
    model         = config.get(f"{tier}_model", default_model)

    api_key = resolve_provider_credential(config, provider_name)

    base_url = resolve_provider_endpoint(config, provider_name, tier)

    _LOGGER.debug("Creating %s tier provider: %s / %s", tier, provider_name, model)

    return create_provider(
        provider_name=provider_name,
        api_key=api_key,
        model=model,
        base_url=base_url,
    )


# ─── Activity-aware chat wrapper (Phase 5) ───────────────────────────────────
#
# Absorbs the hass.async_add_executor_job(lambda: provider.chat(...)) boilerplate
# every call site already needs (provider.chat is a blocking, synchronous call),
# and records bounded activity metadata about the call — never its content.
# Adopting it is a net simplification at a call site, not an added step.

DATA_CATEGORIES = {"text", "vision"}  # fixed vocabulary — never inferred from content


async def chat_with_activity(
    hass,
    provider: "LLMProvider",
    messages: list[dict],
    *,
    role: str,
    data_category: str,
    tools: Optional[list[dict]] = None,
    max_tokens: int = 512,
    temperature: float = 0.7,
    model_override: Optional[str] = None,
) -> dict:
    """Run provider.chat() off the event loop and record bounded activity
    metadata (provider/model/role/location/category, success, token counts,
    latency) — never prompts, responses, tool arguments, images, entity
    states, credentials, or the raw provider object.

    Recording is best-effort and structurally cannot change what the caller
    sees: on success the same standardised dict comes back untouched; on
    failure the same exception is re-raised untouched — activity recording
    happens in a `finally` and can never itself alter or swallow it.
    """
    start = _time.monotonic()
    result = None
    success = False
    try:
        result = await hass.async_add_executor_job(
            lambda: provider.chat(messages, tools=tools, max_tokens=max_tokens,
                                  temperature=temperature, model_override=model_override))
        success = True
        return result
    finally:
        latency_ms = int((_time.monotonic() - start) * 1000)
        usage = (result or {}).get("usage") or {}
        try:
            from . import provider_activity
            await hass.async_add_executor_job(
                provider_activity.record,
                provider.name,
                model_override or getattr(provider, "model", ""),
                role,
                execution_location(provider),
                data_category if data_category in DATA_CATEGORIES else "text",
                success,
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                latency_ms,
            )
        except Exception:
            pass  # activity logging must never affect the actual call


def _classify_conn_error(exc) -> str:
    """Map a provider/client exception to a config-flow error key."""
    msg = str(exc).lower()
    if any(t in msg for t in ("auth", "api key", "api_key", "401", "403",
                              "unauthorized", "invalid key", "permission")):
        return "invalid_auth"
    if any(t in msg for t in ("connect", "timeout", "timed out", "refused",
                              "resolve", "unreachable", "getaddrinfo",
                              "name or service", "connection", "network")):
        return "cannot_connect"
    return "unknown"


async def test_connection(hass, provider, api_key, model, base_url):
    """Verify the LLM is reachable and the credentials work with a tiny chat
    call, so setup can fail fast on a bad URL or key instead of installing into
    a broken state. Returns None on success, else a config-flow error key
    ('cannot_connect' | 'invalid_auth' | 'unknown'). Runs the blocking client in
    the executor; never raises.
    """
    try:
        # Constructing a client can itself do blocking I/O (e.g. the
        # Anthropic SDK reads local credential files and loads the CA
        # bundle in __init__) — run it off the event loop like the ping
        # below, so HA's loop guard doesn't raise mid-setup.
        client = await hass.async_add_executor_job(
            create_provider, provider, api_key, model, base_url or None)
    except Exception as exc:
        # Without this the config flow only ever shows the generic
        # "cannot_connect"/"invalid_auth" key in the UI — the real reason
        # (bad key, DNS, TLS, wrong model) is otherwise lost.
        _LOGGER.error(
            "Nova: failed to build '%s' LLM client (model=%s): %s",
            provider, model, exc,
        )
        return _classify_conn_error(exc)
    if client is None:
        return "cannot_connect"

    def _ping():
        result = client.chat(
            [{"role": "user", "content": "Reply with the word pong."}],
            tools=None,
            max_tokens=32,
            temperature=0.0,
        )
        if not str(result.get("text") or "").strip() and not result.get("tool_calls"):
            raise RuntimeError("provider returned an empty response")
        return result
    try:
        await hass.async_add_executor_job(_ping)
        return None
    except Exception as exc:
        _LOGGER.error(
            "Nova: '%s' LLM connection test failed (model=%s): %s",
            provider, model, exc,
        )
        return _classify_conn_error(exc)
