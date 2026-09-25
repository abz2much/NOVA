"""Groq adapter (Groq's own SDK, OpenAI-shaped chat completions)."""
from __future__ import annotations

from typing import ClassVar, Optional

from .capabilities import SDK_CLIENT_POLICY, ProviderCapabilities, groq_vision
from .errors import ProviderConfigurationError, ProviderErrorKind
from .openai_wire import build_openai_kwargs, parse_openai_completion
from .base import LLMProvider
from .models import ChatRequest, ChatResponse


class GroqProvider(LLMProvider):
    name: ClassVar[str] = "groq"
    concurrency = SDK_CLIENT_POLICY
    requires_credential = True

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        super().__init__(api_key, model, base_url)
        try:
            from groq import Groq
        except ImportError as exc:
            raise ProviderConfigurationError(
                ProviderErrorKind.PROVIDER_UNAVAILABLE, self.name,
                detail="the groq package is not installed") from exc
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = Groq(**kwargs)

    def complete(self, request: ChatRequest) -> ChatResponse:
        model = request.model or self.model
        resp = self._client.chat.completions.create(
            **build_openai_kwargs(request, model))
        return parse_openai_completion(resp, provider=self.name, model=model)

    def capabilities(self, model: Optional[str] = None) -> ProviderCapabilities:
        return ProviderCapabilities(vision=groq_vision(model or self.model))
