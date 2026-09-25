"""The shared adapter contract.

An adapter turns a typed ChatRequest into one provider's wire request and the
provider's reply into a typed ChatResponse. ``complete()`` is synchronous and
blocking: only the activity boundary (activity.py) runs it, in Home
Assistant's executor.

``chat()`` is the legacy dictionary interface kept for the llm_provider
compatibility façade and for code that already holds a provider object.
Production code outside this package calls neither directly; it goes
through activity.execute_chat().
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Optional

from .capabilities import SDK_CLIENT_POLICY, ConcurrencyPolicy, ProviderCapabilities
from .errors import ProviderError, ProviderErrorKind, normalize_error
from .models import ChatRequest, ChatResponse


class LLMProvider(ABC):
    """Abstract base for all LLM adapters."""

    name: ClassVar[str] = "abstract"
    concurrency: ClassVar[ConcurrencyPolicy] = SDK_CLIENT_POLICY
    # Cloud adapters never send a request without their own credential.
    requires_credential: ClassVar[bool] = False

    # Class-level defaults keep instances built without __init__ (test
    # doubles made with object.__new__) usable.
    _client: Any = None
    _closed: bool = False
    _close_lock: Optional[threading.Lock] = None

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self._closed = False
        self._close_lock = threading.Lock()

    def __repr__(self) -> str:
        # Never include the credential.
        return f"<{type(self).__name__} provider={self.name} model={self.model!r}>"

    # ── Request execution ───────────────────────────────────────────────────

    @abstractmethod
    def complete(self, request: ChatRequest) -> ChatResponse:
        """Run one blocking chat completion and return the typed result."""

    def run(self, request: ChatRequest) -> ChatResponse:
        """complete() with the shared guards: a closed client and a missing
        credential fail before any network I/O, and every failure leaves as
        a ProviderError chained to its original."""
        if self._closed:
            raise ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, self.name,
                                detail="the provider client was closed")
        if self.requires_credential and not self.api_key:
            raise ProviderError(ProviderErrorKind.MISSING_CREDENTIAL, self.name)
        try:
            return self.complete(request)
        except ProviderError:
            raise
        except Exception as exc:
            raise normalize_error(exc, self.name) from exc

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        model_override: Optional[str] = None,
    ) -> dict:
        """Legacy dictionary interface: {text, tool_calls, raw, usage}.

        model_override lets a caller request a different model for this
        single call (e.g. a vision-capable model for image analysis)."""
        request = ChatRequest.from_legacy(
            messages, tools, max_tokens, temperature, model_override)
        return self.run(request).to_legacy()

    # ── Capabilities ────────────────────────────────────────────────────────

    def capabilities(self, model: Optional[str] = None) -> ProviderCapabilities:
        return ProviderCapabilities(vision=False)

    def supports_vision(self) -> bool:
        """Whether this backend + model can take image inputs."""
        return self.capabilities().vision

    def supports_tools(self) -> bool:
        """Whether this backend supports function/tool calling."""
        return self.capabilities().tools

    # ── Lifecycle ───────────────────────────────────────────────────────────

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> bool:
        """Close the underlying SDK client exactly once. Returns True only
        for the call that actually closed it. Blocking: call it from the
        executor. Adapters without a closable client just mark themselves
        closed."""
        lock = self._close_lock or threading.Lock()
        with lock:
            if self._closed:
                return False
            self._closed = True
        client = self._client
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()
        return True
