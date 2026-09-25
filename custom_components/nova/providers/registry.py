"""The provider descriptor registry.

One descriptor per provider id says everything the rest of the package needs
to know about it: its adapter, where it executes, which credential and
endpoint fields belong to it, and how its models are discovered. Order is the
public provider order (list_providers()).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional

from .anthropic import AnthropicProvider
from .base import LLMProvider
from .capabilities import OLLAMA_POLICY, SDK_CLIENT_POLICY, ConcurrencyPolicy
from .groq import GroqProvider
from .ollama import OllamaProvider
from .openai_compatible import (
    GEMINI_OPENAI_BASE_URL,
    CustomProvider,
    GeminiProvider,
    OpenAIProvider,
)

LocationPolicy = Literal["cloud", "local", "endpoint"]
AuthStyle = Literal["bearer", "x-api-key", "x-goog-api-key"]
ModelListShape = Literal["openai", "gemini", "ollama"]
Pagination = Optional[Literal["pageToken", "after_id"]]


@dataclass(frozen=True, slots=True)
class DiscoverySpec:
    """How a provider lists its models.

    Cloud providers have one fixed URL. Self-hosted providers derive the URL
    from their saved endpoint (``path`` appended), and only ever send their
    own dedicated credential."""

    auth: AuthStyle
    shape: ModelListShape
    fixed_url: Optional[str] = None
    path: Optional[str] = None
    pagination: Pagination = None
    extra_headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    id: str
    adapter: type[LLMProvider]
    location: LocationPolicy
    credential_field: str
    discovery: DiscoverySpec
    concurrency: ConcurrencyPolicy
    endpoint_field: Optional[str] = None
    default_base_url: Optional[str] = None
    default_model: Optional[str] = None
    # Whether the legacy shared `api_key` may stand in for this provider's
    # own credential while an installation has not migrated yet. Only fixed
    # cloud destinations qualify.
    legacy_shared_key: bool = False

    @property
    def self_hosted(self) -> bool:
        return self.endpoint_field is not None


_DESCRIPTORS: tuple[ProviderDescriptor, ...] = (
    ProviderDescriptor(
        id="groq", adapter=GroqProvider, location="cloud",
        credential_field="groq_api_key", concurrency=SDK_CLIENT_POLICY,
        default_model="openai/gpt-oss-120b", legacy_shared_key=True,
        discovery=DiscoverySpec(
            auth="bearer", shape="openai",
            fixed_url="https://api.groq.com/openai/v1/models"),
    ),
    ProviderDescriptor(
        id="openai", adapter=OpenAIProvider, location="cloud",
        credential_field="openai_api_key", concurrency=SDK_CLIENT_POLICY,
        default_model="gpt-5-mini", legacy_shared_key=True,
        discovery=DiscoverySpec(
            auth="bearer", shape="openai",
            fixed_url="https://api.openai.com/v1/models"),
    ),
    ProviderDescriptor(
        id="ollama", adapter=OllamaProvider, location="local",
        credential_field="ollama_api_key", concurrency=OLLAMA_POLICY,
        endpoint_field="ollama_base_url",
        discovery=DiscoverySpec(auth="bearer", shape="ollama", path="/api/tags"),
    ),
    ProviderDescriptor(
        id="gemini", adapter=GeminiProvider, location="cloud",
        credential_field="gemini_api_key", concurrency=SDK_CLIENT_POLICY,
        default_base_url=GEMINI_OPENAI_BASE_URL,
        default_model="gemini-3.6-flash", legacy_shared_key=True,
        discovery=DiscoverySpec(
            auth="x-goog-api-key", shape="gemini",
            fixed_url="https://generativelanguage.googleapis.com/v1beta/models",
            pagination="pageToken"),
    ),
    ProviderDescriptor(
        id="custom", adapter=CustomProvider, location="endpoint",
        credential_field="custom_api_key", concurrency=SDK_CLIENT_POLICY,
        endpoint_field="custom_base_url",
        discovery=DiscoverySpec(auth="bearer", shape="openai", path="/models"),
    ),
    ProviderDescriptor(
        id="anthropic", adapter=AnthropicProvider, location="cloud",
        credential_field="anthropic_api_key", concurrency=SDK_CLIENT_POLICY,
        default_model="claude-sonnet-5", legacy_shared_key=True,
        discovery=DiscoverySpec(
            auth="x-api-key", shape="openai",
            fixed_url="https://api.anthropic.com/v1/models",
            pagination="after_id",
            extra_headers={"anthropic-version": "2023-06-01"}),
    ),
)

DESCRIPTORS: Mapping[str, ProviderDescriptor] = {d.id: d for d in _DESCRIPTORS}
PROVIDER_IDS: tuple[str, ...] = tuple(d.id for d in _DESCRIPTORS)
CLOUD_PROVIDERS = frozenset(d.id for d in _DESCRIPTORS if d.location == "cloud")
SELF_HOSTED_PROVIDERS = frozenset(d.id for d in _DESCRIPTORS if d.self_hosted)
# provider id -> adapter class, in public order (the legacy PROVIDERS map).
ADAPTERS: Mapping[str, type[LLMProvider]] = {d.id: d.adapter for d in _DESCRIPTORS}


def descriptor(provider: str) -> Optional[ProviderDescriptor]:
    return DESCRIPTORS.get(str(provider or "").strip().lower())


def list_providers() -> list[str]:
    """For UI dropdowns."""
    return list(PROVIDER_IDS)
