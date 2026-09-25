"""Nova's provider architecture.

* models.py — typed request, response, message, image, tool, usage, model and
  discovery contracts;
* errors.py — the normalized error taxonomy;
* capabilities.py — capability and concurrency policy;
* base.py — the adapter contract;
* groq.py, openai_compatible.py, ollama.py, anthropic.py — isolated adapters;
* registry.py — one descriptor per provider;
* routing.py — credential and endpoint isolation, and the factory;
* discovery.py — model discovery behind the descriptors;
* destinations.py — the destination and redirect policy;
* manager.py — runtime-owned client lifecycle;
* activity.py — the single activity-aware execution boundary.

custom_components.nova.llm_provider remains the compatibility façade.
"""
from __future__ import annotations

from .activity import ROLES, execute_chat
from .base import LLMProvider
from .errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderErrorKind,
    ProviderRequestError,
    error_text,
)
from .manager import ProviderManager, provider_scope, runtime_manager
from .models import (
    Capability,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ImagePart,
    ModelInfo,
    TextPart,
    ToolCall,
    ToolSpec,
    Usage,
)
from .registry import DESCRIPTORS, PROVIDER_IDS, ProviderDescriptor, descriptor
from .routing import ProviderSpec, build_provider, resolve_spec, tier_spec

__all__ = [
    "Capability", "ChatMessage", "ChatRequest", "ChatResponse", "DESCRIPTORS",
    "ImagePart", "LLMProvider", "ModelInfo", "PROVIDER_IDS", "ProviderConfigurationError",
    "ProviderDescriptor", "ProviderError", "ProviderErrorKind", "ProviderManager",
    "ProviderRequestError", "ProviderSpec", "ROLES", "TextPart", "ToolCall", "ToolSpec",
    "Usage", "build_provider", "descriptor", "error_text", "execute_chat",
    "provider_scope", "resolve_spec", "runtime_manager", "tier_spec",
]
