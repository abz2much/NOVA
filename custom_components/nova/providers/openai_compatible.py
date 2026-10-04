"""OpenAI-compatible adapters: OpenAI, Gemini and custom endpoints.

The three share the OpenAI SDK and wire format but are distinct providers:
each reports its own name, so Provider Activity and execution location see
the provider that actually handled the call. A custom endpoint never falls
back to OpenAI's default endpoint.
"""
from __future__ import annotations

from typing import Any, ClassVar, Optional

from .base import LLMProvider
from .capabilities import SDK_CLIENT_POLICY, ProviderCapabilities, openai_compatible_vision
from .destinations import check_url
from .errors import ProviderConfigurationError, ProviderErrorKind
from .models import ChatRequest, ChatResponse
from .openai_wire import build_openai_kwargs, parse_openai_completion

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _import_openai():
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ProviderConfigurationError(
            ProviderErrorKind.PROVIDER_UNAVAILABLE, "openai",
            detail="the openai package is not installed") from exc
    return OpenAI


class OpenAICompatibleProvider(LLMProvider):
    """Base for adapters that speak OpenAI chat completions through the
    OpenAI SDK."""

    name: ClassVar[str] = "openai"
    concurrency = SDK_CLIENT_POLICY
    # Whether tool call ``extra_content`` in the history is sent back. Only
    # Gemini needs it (its thought signature); everyone else would reject it.
    keeps_extra_content: ClassVar[bool] = False

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        super().__init__(api_key, model, base_url)
        OpenAI = _import_openai()
        kwargs: dict[str, Any] = {"api_key": api_key or "not-required"}
        if base_url:
            kwargs["base_url"] = base_url
        kwargs.update(self._client_options())
        self._client = OpenAI(**kwargs)

    def _client_options(self) -> dict[str, Any]:
        """Extra OpenAI(...) constructor options. None for fixed cloud
        destinations."""
        return {}

    def _extra_body(self) -> dict:
        """Provider-specific request extras. Empty for vanilla OpenAI."""
        return {}

    def _adjust_kwargs(self, kwargs: dict[str, Any], model: str) -> dict[str, Any]:
        """Last change to the request before it is sent. None for most
        OpenAI compatible APIs."""
        return kwargs

    def complete(self, request: ChatRequest) -> ChatResponse:
        model = request.model or self.model
        kwargs = build_openai_kwargs(
            request, model, keep_extra_content=self.keeps_extra_content)
        kwargs = self._adjust_kwargs(kwargs, model)
        extra = self._extra_body()
        if extra:
            kwargs["extra_body"] = extra
        resp = self._client.chat.completions.create(**kwargs)
        return parse_openai_completion(resp, provider=self.name, model=model)

    def capabilities(self, model: Optional[str] = None) -> ProviderCapabilities:
        return ProviderCapabilities(vision=openai_compatible_vision(model or self.model))


# OpenAI's reasoning models: GPT-5 and the o series.
_OPENAI_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
# Reasoning models spend hidden reasoning tokens out of max_completion_tokens,
# so a small budget (40 to 120 tokens for scenes, the classifier and the
# package monitor) can run out before any text is written and the reply comes
# back empty. Room for the reasoning is added on top of what the caller asked
# for, and the effort is set low: these are short tasks.
_REASONING_TOKEN_ROOM = 4096
_REASONING_EFFORT = "low"
# Models that reject reasoning_effort: the chat snapshots and the first o1
# previews.
_NO_EFFORT_PREFIXES = ("o1-mini", "o1-preview")


def _openai_base_model(model: str) -> str:
    """The base model name, lower case: a fine-tuned "ft:gpt-5-mini:org::id"
    gives "gpt-5-mini"."""
    name = str(model or "").lower()
    if name.startswith("ft:"):
        name = name[3:].split(":", 1)[0]
    return name


class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI's own API."""

    name: ClassVar[str] = "openai"
    requires_credential = True

    def _adjust_kwargs(self, kwargs: dict[str, Any], model: str) -> dict[str, Any]:
        """OpenAI's reasoning models reject max_tokens and any temperature
        but the default: send max_completion_tokens, with room for the
        reasoning, and leave temperature out."""
        base = _openai_base_model(model)
        if base.startswith(_OPENAI_REASONING_PREFIXES):
            kwargs = dict(kwargs)
            requested = kwargs.pop("max_tokens", None)
            if requested is not None:
                kwargs["max_completion_tokens"] = int(requested) + _REASONING_TOKEN_ROOM
            kwargs.pop("temperature", None)
            if "-chat" not in base and not base.startswith(_NO_EFFORT_PREFIXES):
                kwargs.setdefault("reasoning_effort", _REASONING_EFFORT)
        return kwargs


class GeminiProvider(OpenAICompatibleProvider):
    """Google Gemini through its OpenAI-compatible endpoint."""

    name: ClassVar[str] = "gemini"
    requires_credential = True
    # Gemini 3 rejects a follow up tool call request unless the thought
    # signature from its previous call is sent back unchanged.
    keeps_extra_content = True

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        super().__init__(api_key, model, base_url or GEMINI_OPENAI_BASE_URL)


class CustomProvider(OpenAICompatibleProvider):
    """An administrator-configured OpenAI-compatible endpoint.

    It requires its own saved endpoint (it never falls back to OpenAI's), and
    its HTTP client checks every request, redirects included, against the
    destination policy. httpx itself drops the Authorization header when a
    redirect changes origin."""

    name: ClassVar[str] = "custom"
    requires_credential = False

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        if not str(base_url or "").strip():
            raise ProviderConfigurationError(
                ProviderErrorKind.INVALID_ENDPOINT, self.name,
                detail="the custom endpoint is not configured")
        check_url(str(base_url))
        super().__init__(api_key, model, base_url)

    def _client_options(self) -> dict[str, Any]:
        def _check_request(request) -> None:
            check_url(str(request.url), resolve=True)

        hooks = {"request": [_check_request]}
        try:
            from openai import DefaultHttpxClient
            http_client = DefaultHttpxClient(event_hooks=hooks, max_redirects=3)
        except ImportError:
            import httpx
            http_client = httpx.Client(event_hooks=hooks, follow_redirects=True,
                                       max_redirects=3)
        return {"http_client": http_client}
