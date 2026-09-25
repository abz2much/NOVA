"""
Nova — LLM provider compatibility façade.

The provider architecture lives in the ``providers`` package: typed models,
normalized errors, one descriptor registry, isolated adapters (Groq,
OpenAI-compatible, native Ollama, Anthropic), central routing, model
discovery, the runtime-owned ProviderManager and the single activity-aware
execution boundary.

This module keeps every name other code and older integrations import from
``custom_components.nova.llm_provider``, with the same signatures and the same
response dictionary:

  {
    "text": "...",                      # visible response text only
    "tool_calls": [{"id": ..., "name": ..., "args": {...}}],
    "raw": <sanitized compatibility value>,
    "usage": {"input_tokens": int|None, "output_tokens": int|None},
  }

``raw`` is rebuilt by each adapter from normalized data only: it never holds
the SDK response object, hidden reasoning, credentials, headers, request
bodies or images. Nova's own code never reads it.
"""
from __future__ import annotations

import logging
from typing import Optional

from .providers.activity import execute_chat
from .providers.anthropic import AnthropicProvider
from .providers.base import LLMProvider
from .providers.destinations import check_url
from .providers.errors import ProviderError, config_flow_error_key
from .providers.groq import GroqProvider
from .providers.manager import async_close_client
from .providers.models import DATA_CATEGORIES
from .providers.ollama import (
    OLLAMA_KEEP_ALIVE,
    OLLAMA_NUM_CTX,
    OLLAMA_TIMEOUT,
    OllamaProvider,
)
from .providers.openai_compatible import (
    CustomProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
)
from .providers.openai_wire import openai_style_usage
from .providers.registry import (
    ADAPTERS,
    CLOUD_PROVIDERS,
    SELF_HOSTED_PROVIDERS,
    list_providers,
)
from .providers.routing import (
    _OLLAMA_TAG_RE,
    DEFAULT_MODELS,
    create_provider,
    create_tier_provider,
    detect_provider_from_key,
    execution_location,
    is_local_url,
    normalize_provider_endpoint,
    normalize_routing,
    resolve_provider_credential,
    resolve_provider_endpoint,
)

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "AnthropicProvider", "CustomProvider", "DATA_CATEGORIES", "DEFAULT_MODELS",
    "GeminiProvider", "GroqProvider", "LLMProvider", "OLLAMA_KEEP_ALIVE",
    "OLLAMA_NUM_CTX", "OLLAMA_TIMEOUT", "OllamaProvider", "OpenAICompatibleProvider",
    "OpenAIProvider", "PROVIDERS", "chat_with_activity", "create_provider",
    "create_tier_provider", "detect_provider_from_key", "execution_location",
    "list_providers", "normalize_provider_endpoint", "normalize_routing",
    "resolve_provider_credential", "resolve_provider_endpoint", "test_connection",
]

# provider id -> adapter class, in public order. Gemini and custom have
# their own adapters (and names) instead of reusing OpenAIProvider.
PROVIDERS = dict(ADAPTERS)

_CLOUD_PROVIDERS = set(CLOUD_PROVIDERS)
_SELF_HOSTED_PROVIDERS = frozenset(SELF_HOSTED_PROVIDERS)
_PROVIDER_ENDPOINT_FIELDS = {
    "ollama": "ollama_base_url",
    "custom": "custom_base_url",
}
_is_local_url = is_local_url
_OLLAMA_TAG_RE = _OLLAMA_TAG_RE


def _openai_style_usage(resp) -> dict:
    """Token usage dictionary from an OpenAI-compatible completion."""
    return openai_style_usage(resp).to_dict()


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
    """Legacy dictionary form of providers.activity.execute_chat(): the same
    boundary (executor, concurrency policy, Provider Activity recording),
    returning the standard response dictionary. Failures raise ProviderError
    chained to the original exception."""
    response = await execute_chat(
        hass, provider, messages,
        role=role, data_category=data_category, tools=tools,
        max_tokens=max_tokens, temperature=temperature,
        model_override=model_override,
    )
    return response.to_legacy()


def _classify_conn_error(exc) -> str:
    """Map a provider/client exception to a config-flow error key."""
    return config_flow_error_key(exc)


async def test_connection(hass, provider, api_key, model, base_url):
    """Verify the LLM is reachable and the credentials work with a tiny chat
    call, so setup can fail fast on a bad URL or key instead of installing
    into a broken state. Returns None on success, else a config-flow error
    key ('cannot_connect' | 'invalid_auth' | 'unknown'). Never raises.

    A self-hosted endpoint is checked against the destination policy
    (link-local and metadata destinations are refused) before anything is
    sent to it. The client built for the check is closed afterwards."""
    provider_id = str(provider or "").strip().lower()
    if provider_id in _SELF_HOSTED_PROVIDERS and base_url:
        try:
            await hass.async_add_executor_job(
                lambda: check_url(str(base_url), resolve=True))
        except ProviderError as exc:
            _LOGGER.error("Nova: '%s' endpoint refused: %s", provider_id, exc)
            return "cannot_connect"
    try:
        # Constructing a client can itself do blocking I/O (the SDKs load
        # the CA bundle), so run it off the event loop.
        client = await hass.async_add_executor_job(
            create_provider, provider, api_key, model, base_url or None)
    except Exception as exc:
        _LOGGER.error(
            "Nova: failed to build '%s' LLM client (model=%s): %s",
            provider, model, exc,
        )
        return _classify_conn_error(exc)
    if client is None:
        return "cannot_connect"
    try:
        result = await execute_chat(
            hass, client,
            [{"role": "user", "content": "Reply with the word pong."}],
            role="connection_test", data_category="text",
            tools=None, max_tokens=32, temperature=0.0,
        )
        if not result.text.strip() and not result.tool_calls:
            _LOGGER.error("Nova: '%s' LLM connection test returned an empty "
                          "response (model=%s)", provider, model)
            return "unknown"
        return None
    except Exception as exc:
        _LOGGER.error(
            "Nova: '%s' LLM connection test failed (model=%s): %s",
            provider, model, exc,
        )
        return _classify_conn_error(exc)
    finally:
        await async_close_client(hass, client)
