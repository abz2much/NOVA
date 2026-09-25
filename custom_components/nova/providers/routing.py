"""Central routing: which provider, model, credential and endpoint a role
uses, and building an adapter for that choice.

Isolation rules:
* a provider only ever receives its own dedicated credential (with the
  narrow, self-expiring legacy shared-key fallback for fixed cloud
  destinations that are the saved primary provider);
* self-hosted providers only use their own saved endpoint field;
* a custom provider never falls back to another provider's endpoint;
* when a model id is re-routed to Ollama, credential and endpoint are
  resolved again for Ollama, so another provider's credential never follows
  the model.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

from .base import LLMProvider
from .errors import ProviderConfigurationError, ProviderErrorKind
from .registry import CLOUD_PROVIDERS, DESCRIPTORS, SELF_HOSTED_PROVIDERS, descriptor

_LOGGER = logging.getLogger(__name__)

# Ollama tag syntax: name[:size-tag], e.g. gemma4:26b, llama3.3:70b-instruct.
# No cloud provider uses colon-tagged model ids, which makes this a safe tell.
_OLLAMA_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*$", re.I)

# A default model per cloud provider, used when first-run setup detects a
# provider from the pasted key but the model field is still on its
# placeholder value.
DEFAULT_MODELS = {
    pid: d.default_model for pid, d in DESCRIPTORS.items()
    if d.location == "cloud" and d.default_model
}

# Model defaults per role (role -> (provider key, model key)).
ROLE_FIELDS = {
    "conversation": ("llm_provider", "model"),
    "classifier": ("classifier_provider", "classifier_model"),
    "reasoning": ("reasoning_provider", "reasoning_model"),
    "review": ("review_provider", "review_model"),
    "vision": ("vision_provider", "vision_model"),
    "camera_reasoning": ("camera_reasoning_provider", "camera_reasoning_model"),
}
TIERS = ("classifier", "reasoning", "review", "conversation")


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """One resolved provider choice. The credential is never shown in repr
    and only enters the fingerprint as a digest."""

    provider: str
    model: str
    api_key: str = field(default="", repr=False)
    base_url: Optional[str] = None

    def fingerprint(self) -> str:
        key_digest = hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()
        material = "\0".join((self.provider, self.model, self.base_url or "", key_digest))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ── Endpoints ───────────────────────────────────────────────────────────────

def normalize_provider_endpoint(value: str, provider: str) -> str:
    """Validate and normalise a self-hosted provider endpoint.

    Accepts a full HTTP(S) URL or a bare host/IP. Bare Ollama endpoints get
    Ollama's default port. Credentials, query strings, and fragments are not
    accepted in endpoint URLs; authentication belongs in the provider's
    dedicated credential field.
    """
    provider = str(provider or "").strip().lower()
    if provider not in SELF_HOSTED_PROVIDERS:
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
    desc = descriptor(provider)
    if desc is None or desc.endpoint_field is None:
        return None

    candidates = []
    if tier:
        candidates.append(config.get(f"{tier}_base_url"))
    candidates.append(config.get(desc.endpoint_field))
    if not config.get("self_hosted_endpoints_migrated"):
        candidates.append(config.get("llm_base_url"))
    for candidate in candidates:
        if candidate not in (None, ""):
            return normalize_provider_endpoint(str(candidate), provider)
    return None


# ── Credentials ─────────────────────────────────────────────────────────────

def resolve_provider_credential(config: dict, provider: str) -> str:
    """The credential `provider` should use, and only that provider's own.

    Every provider has a dedicated field (const.PROVIDER_API_KEY_FIELDS). Once
    an installation has migrated (ha_secrets.split_shared_credential), that
    field is always populated for whichever provider owns it and this is the
    only thing read.

    A narrow, self-expiring fallback covers an installation still waiting on
    that migration: the shared legacy `api_key` is used ONLY when `provider`
    is one of the four fixed cloud providers AND it is also the
    installation's saved primary provider (`llm_provider`). Ollama and custom
    never receive it.
    """
    from ..const import CREDENTIAL_LEGACY_FALLBACK_PROVIDERS, PROVIDER_API_KEY_FIELDS

    provider = str(provider or "").strip().lower()
    field_name = PROVIDER_API_KEY_FIELDS.get(provider)
    if not field_name:
        return ""
    val = str(config.get(field_name) or "")
    if val:
        return val
    if provider not in CREDENTIAL_LEGACY_FALLBACK_PROVIDERS:
        return ""
    saved_provider = str(config.get("llm_provider") or "groq").strip().lower()
    if provider != saved_provider:
        return ""
    return str(config.get("api_key") or "")


def detect_provider_from_key(api_key: str) -> str:
    """Guess the cloud provider from an API key's own shape.

    Key prefixes are stable, documented conventions for each vendor:
      - Anthropic: 'sk-ant-'
      - Groq:      'gsk_'
      - Google AI Studio (Gemini): 'AIza'
      - OpenAI:    'sk-' (checked after Anthropic, which also starts 'sk-')

    Falls back to 'groq' for anything unrecognized, so an unknown key shape
    still gets a clear 'invalid_auth' from test_connection.
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


# ── Routing ─────────────────────────────────────────────────────────────────

def normalize_routing(provider_name: str, model: str,
                      base_url: Optional[str]) -> tuple[str, Optional[str], Optional[str]]:
    """(provider_name, base_url, correction_note|None). A colon-tagged model
    (Ollama syntax, e.g. 'gemma4:26b') configured against a cloud provider is
    a settings mismatch that produces confusing 404s from the cloud API. Route
    it to Ollama and say so, instead of forwarding a local model name to a
    cloud provider."""
    p = (provider_name or "").lower().strip()
    m = (model or "").strip()
    if p in CLOUD_PROVIDERS and _OLLAMA_TAG_RE.match(m):
        note = (f"model '{m}' uses Ollama tag syntax but provider was "
                f"'{p}' — routing to ollama"
                + ("" if base_url else " (default base URL)"))
        return "ollama", base_url, note
    return p, base_url, None


def resolve_spec(
    config: dict,
    provider: str,
    model: str,
    *,
    tier: Optional[str] = None,
) -> ProviderSpec:
    """Resolve credential and endpoint for (provider, model) from one
    effective configuration, after routing correction."""
    routed, _, note = normalize_routing(provider, model, None)
    if note:
        _LOGGER.warning("LLM routing corrected: %s", note)
    return ProviderSpec(
        provider=routed,
        model=model,
        api_key=resolve_provider_credential(config, routed),
        base_url=resolve_provider_endpoint(config, routed, tier),
    )


def tier_spec(config: dict, tier: str) -> ProviderSpec:
    """The ProviderSpec for an observer tier or the conversation role."""
    from ..const import (
        CONF_MODEL,
        DEFAULT_CLASSIFIER_MODEL,
        DEFAULT_CLASSIFIER_PROVIDER,
        DEFAULT_REASONING_MODEL,
        DEFAULT_REASONING_PROVIDER,
        DEFAULT_REVIEW_MODEL,
        DEFAULT_REVIEW_PROVIDER,
    )

    tier_defaults = {
        "classifier": (DEFAULT_CLASSIFIER_PROVIDER, DEFAULT_CLASSIFIER_MODEL),
        "reasoning": (DEFAULT_REASONING_PROVIDER, DEFAULT_REASONING_MODEL),
        "review": (DEFAULT_REVIEW_PROVIDER, DEFAULT_REVIEW_MODEL),
        "conversation": (
            config.get("llm_provider", "groq"),
            config.get(CONF_MODEL, "openai/gpt-oss-120b"),
        ),
    }
    if tier not in tier_defaults:
        raise ValueError(f"Unknown tier: {tier}")
    default_provider, default_model = tier_defaults[tier]
    provider_name = config.get(f"{tier}_provider", default_provider)
    model = config.get(f"{tier}_model", default_model)
    return resolve_spec(config, provider_name, model, tier=tier)


def build_provider(spec: ProviderSpec) -> LLMProvider:
    """Construct the adapter for a resolved spec. Blocking (SDK clients load
    TLS material): run it in the executor."""
    desc = descriptor(spec.provider)
    if desc is None:
        raise ProviderConfigurationError(
            ProviderErrorKind.INVALID_REQUEST, "provider",
            detail="Unknown LLM provider")
    base_url = spec.base_url or desc.default_base_url
    return desc.adapter(api_key=spec.api_key, model=spec.model, base_url=base_url)


def create_provider(
    provider_name: str,
    api_key: str,
    model: str,
    base_url: Optional[str] = None,
) -> LLMProvider:
    """Factory for provider instances (legacy signature).

    provider_name: 'groq' | 'openai' | 'gemini' | 'ollama' | 'anthropic' | 'custom'
    'ollama' and 'custom' require their own endpoint in base_url; 'gemini'
    defaults to Google's OpenAI-compatible endpoint. When a model is
    re-routed to Ollama the given credential is not sent there: it belongs
    to the provider it was resolved for. The endpoint, if any, is kept."""
    requested = str(provider_name or "").lower().strip()
    routed, base_url, note = normalize_routing(requested, model, base_url)
    if note:
        _LOGGER.warning("LLM routing corrected: %s", note)
        api_key = ""
    return build_provider(ProviderSpec(
        provider=routed, model=model, api_key=api_key or "", base_url=base_url))


def create_tier_provider(config: dict, tier: str) -> LLMProvider:
    """Build a provider for a specific observer tier.

    tier must be one of: 'classifier', 'reasoning', 'review', 'conversation'.
    Each provider resolves its own dedicated credential and endpoint."""
    spec = tier_spec(config, tier)
    _LOGGER.debug("Creating %s tier provider: %s / %s", tier, spec.provider, spec.model)
    return build_provider(spec)


# ── Execution location ──────────────────────────────────────────────────────

def is_local_url(base_url: Optional[str]) -> bool:
    """Whether a configured base_url unambiguously points at a local/private
    host — loopback, an RFC1918 private address, or an mDNS .local name."""
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


def execution_location(provider: Any) -> str:
    """Where a provider's calls actually execute, determined from its
    descriptor and configured endpoint, never guessed beyond that:
      - a hosted (cloud) provider -> "cloud"
      - ollama                    -> "local"
      - custom (or unknown)       -> "local" when its own configured
        base_url is unambiguously local, else "unknown"
    """
    name = getattr(provider, "name", "")
    desc = DESCRIPTORS.get(name)
    if desc is not None and desc.location == "cloud":
        return "cloud"
    if desc is not None and desc.location == "local":
        return "local"
    if is_local_url(getattr(provider, "base_url", None)):
        return "local"
    return "unknown"
