"""Model discovery behind provider descriptors.

Destinations are chosen by the server, never by the browser:

* cloud providers use the fixed URL in their descriptor, with their own
  credential, and may not be redirected off that origin;
* Ollama and custom use only their saved, administrator-controlled endpoint
  (the endpoint-test command alone passes an administrator's staged
  endpoint) and only their own dedicated credential;
* every redirect is checked against the destination policy, credentials are
  dropped on any origin change, and link-local and cloud metadata
  destinations are refused;
* requests are bounded in time, pages, models and response size.

The model-list cache is a bounded, process-wide TTL cache of public model
ids keyed by (provider, url). It holds no client, credential or hass state.
Every invalidation advances a generation, and a fetch that started before an
invalidation can never repopulate the cache after it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

from .destinations import (
    MAX_REDIRECTS,
    check_url,
    resolve_redirect,
    same_origin,
    strip_credentials,
)
from .errors import ProviderError, ProviderErrorKind
from .models import DiscoveryRequest, DiscoveryResult, ModelInfo
from .registry import DESCRIPTORS, descriptor
from .routing import resolve_provider_credential, resolve_provider_endpoint

_LOGGER = logging.getLogger(__name__)

SAFE_MODEL_DISCOVERY_ERROR = "model_discovery_unavailable"

MAX_DISCOVERY_PAGES = 5
MAX_DISCOVERY_MODELS = 500
MAX_CURSOR_LEN = 2048
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT = 12.0

MODEL_CACHE_TTL = 300.0  # seconds
MODEL_CACHE_MAX_ENTRIES = 32


class ModelDiscoveryHTTPError(RuntimeError):
    """Upstream model endpoint returned a non-success status."""

    def __init__(self, status: int):
        super().__init__("model discovery request failed")
        self.status = status


# ── Request resolution ──────────────────────────────────────────────────────

def _auth_headers(style: str, api_key: str) -> dict[str, str]:
    if not api_key:
        return {}
    if style == "x-api-key":
        return {"x-api-key": api_key}
    if style == "x-goog-api-key":
        return {"x-goog-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def resolve_discovery_request(config: dict, provider: str) -> DiscoveryRequest:
    """A server-selected model endpoint and its safe auth headers.

    Cloud endpoints are fixed. Ollama and custom endpoints come only from
    the saved effective configuration, with only their own optional
    dedicated credential (custom_api_key / ollama_api_key) — never the
    shared primary key and never a browser-supplied one."""
    provider = str(provider or "").strip().lower()
    desc = descriptor(provider)
    if desc is None:
        raise ValueError("unknown model provider")
    spec = desc.discovery
    if spec.fixed_url:
        headers = dict(spec.extra_headers)
        headers.update(_auth_headers(spec.auth, resolve_provider_credential(config, provider)))
        return DiscoveryRequest(provider=provider, url=spec.fixed_url, headers=headers,
                                pagination=spec.pagination, fixed_destination=True)

    base = resolve_provider_endpoint(config, provider)
    if not base:
        raise ValueError("saved model endpoint is not configured")
    base = base.rstrip("/")
    if provider == "ollama" and base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    if base.endswith("/models"):
        url = base
    else:
        url = f"{base}{spec.path}"
    headers = _auth_headers(spec.auth, str(config.get(desc.credential_field) or ""))
    return DiscoveryRequest(provider=provider, url=url, headers=headers,
                            pagination=spec.pagination, fixed_destination=False)


# ── Response parsing ────────────────────────────────────────────────────────

def _shape(provider: str, url: str) -> str:
    desc = DESCRIPTORS.get(provider)
    shape = desc.discovery.shape if desc else "openai"
    if shape == "ollama" and not url.endswith("/api/tags"):
        return "openai"   # an Ollama endpoint saved with an explicit /models path
    return shape


def parse_model_list(provider: str, url: str, data: dict) -> list[str]:
    """Normalise every supported provider response to sorted model IDs."""
    models: list[str] = []
    shape = _shape(provider, url)
    if shape == "gemini":
        for model in data.get("models", []):
            name = model.get("name", "")
            if name.startswith("models/"):
                name = name[len("models/"):]
            methods = model.get("supportedGenerationMethods", [])
            if name and (not methods or "generateContent" in methods):
                models.append(name[:512])
    elif shape == "ollama":
        for model in data.get("models", []):
            name = model.get("name")
            if name:
                models.append(str(name)[:512])
    else:
        for model in data.get("data", []):
            model_id = model.get("id")
            if model_id:
                models.append(str(model_id)[:512])
    return sorted(set(models))


def parse_model_infos(provider: str, url: str, data: dict) -> list[ModelInfo]:
    """Bounded, display-safe model metadata reported by a provider.

    Capability flags are never guessed from a model name. Ollama exposes
    them directly from ``/api/tags``; providers that only return IDs keep an
    empty capability list so the panel can say the capability is unknown."""
    if _shape(provider, url) != "ollama":
        return [ModelInfo(id=model_id) for model_id in parse_model_list(provider, url, data)]
    result: list[ModelInfo] = []
    for item in data.get("models", []):
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("name") or "").strip()
        if not model_id:
            continue
        raw_caps = item.get("capabilities") or []
        capabilities = sorted({
            str(cap).strip().lower()[:64]
            for cap in raw_caps
            if isinstance(cap, str) and str(cap).strip()
        })[:32]
        details = item.get("details")
        if not isinstance(details, dict):
            details = {}
        family = str(details.get("family") or "").strip()
        quantization = str(details.get("quantization_level") or "").strip()
        size = item.get("size")
        context_length = details.get("context_length")
        result.append(ModelInfo(
            id=model_id[:512],
            capabilities=tuple(capabilities),
            family=family[:128] or None,
            quantization=quantization[:64] or None,
            size=size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
            context_length=(context_length if isinstance(context_length, int)
                            and not isinstance(context_length, bool)
                            and context_length > 0 else None),
        ))
    return sorted(result, key=lambda item: item.id)


def parse_model_details(provider: str, url: str, data: dict) -> list[dict]:
    return [info.to_dict() for info in parse_model_infos(provider, url, data)]


def log_discovery_failure(provider: str, url: str, exc: Exception, *, logger=None) -> None:
    """Log bounded failure metadata without URLs, credentials, or bodies."""
    from urllib.parse import urlparse

    logger = logger or _LOGGER
    safe_provider = str(provider or "").strip().lower()
    if safe_provider not in DESCRIPTORS:
        safe_provider = "unknown"
    try:
        hostname = urlparse(url).hostname or "unresolved"
    except ValueError:
        hostname = "unresolved"
    hostname = "".join(
        char for char in hostname if char.isalnum() or char in ".:_-"
    )[:255] or "unresolved"
    raw_status = getattr(exc, "status", None)
    status = raw_status if isinstance(raw_status, int) else "none"
    logger.info(
        "Model discovery failed: provider=%s host=%s status=%s error=%s",
        safe_provider,
        hostname,
        status,
        type(exc).__name__,
    )


# ── Pagination ──────────────────────────────────────────────────────────────
#
# Gemini and Anthropic's model-list endpoints paginate; the rest return a
# flat list. Pagination is driven ONLY by an opaque cursor value the
# provider's previous page returned — never a URL a response might include —
# appended as a query param to the one approved endpoint, and bounded in
# both pages and models.

def next_page_cursor(style: Optional[str], data: dict) -> Optional[str]:
    """The opaque cursor for the next page, or None to stop. Any shape that
    doesn't clearly and safely mean "there is a next page" stops pagination.
    Never returns anything that looks like a URL."""
    if not isinstance(data, dict) or style is None:
        return None
    if style == "pageToken":
        token = data.get("nextPageToken")
    elif style == "after_id":
        if data.get("has_more") is not True:
            return None
        token = data.get("last_id")
    else:
        return None
    if not isinstance(token, str):
        return None
    token = token.strip()
    if not token or len(token) > MAX_CURSOR_LEN or "://" in token:
        return None
    return token


def page_query_params(style: Optional[str], cursor: Optional[str]) -> dict:
    if style == "pageToken":
        params = {"pageSize": "100"}
        if cursor:
            params["pageToken"] = cursor
        return params
    if style == "after_id":
        params = {"limit": "100"}
        if cursor:
            params["after_id"] = cursor
        return params
    return {}


# ── Bounded cache with generations ──────────────────────────────────────────

class ModelListCache:
    """TTL- and size-bounded cache of successful model lists, keyed by
    (provider, url). Only a successful fetch is stored; an auth failure can
    never masquerade as a cached empty success."""

    def __init__(self, ttl: float = MODEL_CACHE_TTL,
                 max_entries: int = MODEL_CACHE_MAX_ENTRIES,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock
        self._entries: dict[tuple[str, str], tuple[float, Any]] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}
        self._generation = 0
        self._provider_generation: dict[str, int] = {}

    def generation(self, provider: str) -> tuple[int, int]:
        """A token that changes whenever ``provider`` (or everything) is
        invalidated. Take it before a fetch starts and pass it to set()."""
        return (self._generation, self._provider_generation.get(provider, 0))

    def get(self, key: tuple[str, str]) -> Any:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if (self._clock() - stored_at) > self.ttl:
            self._entries.pop(key, None)
            return None
        return value

    def set(self, key: tuple[str, str], value: Any, *, generation: tuple[int, int]) -> bool:
        """Store ``value`` unless ``key``'s provider was invalidated after
        ``generation`` was taken, so a fetch that began before a credential
        or endpoint change can never repopulate the cache after it."""
        if generation != self.generation(key[0]):
            return False
        if key not in self._entries and len(self._entries) >= self.max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            self._entries.pop(oldest, None)
        self._entries[key] = (self._clock(), value)
        return True

    def invalidate(self, provider: Optional[str] = None) -> None:
        """Drop cached lists (and in-flight dedup entries) for ``provider``,
        or everything, and advance the generation."""
        if provider is None:
            self._generation += 1
            self._entries.clear()
            self._inflight.clear()
            return
        self._provider_generation[provider] = self._provider_generation.get(provider, 0) + 1
        for key in [k for k in self._entries if k[0] == provider]:
            self._entries.pop(key, None)
        for key in [k for k in self._inflight if k[0] == provider]:
            self._inflight.pop(key, None)

    async def dedupe(self, key: tuple[str, str], fetch: Callable[[], Any]) -> Any:
        """Share one in-flight ``fetch`` among concurrent callers of the same
        key and generation. A caller arriving after an invalidation starts a
        fresh fetch instead of joining the stale one."""
        flight_key = (key[0], key[1], self.generation(key[0]))
        existing = self._inflight.get(flight_key)
        if existing is not None:
            return await asyncio.shield(existing)
        task = asyncio.ensure_future(fetch())
        self._inflight[flight_key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._inflight.get(flight_key) is task:
                self._inflight.pop(flight_key, None)

    def __len__(self) -> int:
        return len(self._entries)


# Process-wide model-list cache (public model ids only; see module docstring).
MODEL_CACHE = ModelListCache()


def invalidate_model_cache(provider: Optional[str] = None) -> None:
    MODEL_CACHE.invalidate(provider)


# ── Fetching ────────────────────────────────────────────────────────────────

async def _read_bounded(resp) -> bytes:
    body = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "endpoint",
                                detail="the model list response is too large")
    return bytes(body)


async def _get_json(session, request: DiscoveryRequest, params: dict,
                    check_destination: Callable[[str], Any]) -> dict:
    """One GET with validated, credential-safe redirects and a bounded
    body."""
    url = request.url
    headers = dict(request.headers)
    for _hop in range(MAX_REDIRECTS + 1):
        if not request.fixed_destination:
            await check_destination(url)
        async with asyncio.timeout(REQUEST_TIMEOUT):
            async with session.get(url, headers=headers, params=params or None,
                                   allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    target = resolve_redirect(url, resp.headers.get("Location"))
                    if not same_origin(url, target):
                        if request.fixed_destination:
                            raise ProviderError(
                                ProviderErrorKind.INVALID_ENDPOINT, request.provider,
                                detail="the provider redirected off its fixed destination")
                        headers = strip_credentials(headers)
                    url = target
                    continue
                if resp.status != 200:
                    raise ModelDiscoveryHTTPError(resp.status)
                body = await _read_bounded(resp)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, request.provider) from exc
        if not isinstance(data, dict):
            raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, request.provider)
        return data
    raise ProviderError(ProviderErrorKind.INVALID_ENDPOINT, request.provider,
                        detail="the endpoint redirected too many times")


def destination_checker(hass) -> Callable[[str], Any]:
    """An async check of a local/custom destination (DNS resolved in the
    executor)."""
    async def _check(url: str) -> None:
        await hass.async_add_executor_job(lambda: check_url(url, resolve=True))
    return _check


async def fetch_models(hass, session, request: DiscoveryRequest) -> DiscoveryResult:
    """Query a provider's models endpoint: model ids, truncation state and
    provider-reported metadata. Paginates only where the provider officially
    supports it, bounded to MAX_DISCOVERY_PAGES requests and
    MAX_DISCOVERY_MODELS models."""
    check = destination_checker(hass)
    all_models: list[str] = []
    all_infos: list[ModelInfo] = []
    cursor: Optional[str] = None
    truncated = False
    for _page in range(MAX_DISCOVERY_PAGES):
        params = page_query_params(request.pagination, cursor)
        data = await _get_json(session, request, params, check)
        all_models.extend(parse_model_list(request.provider, request.url, data))
        all_infos.extend(parse_model_infos(request.provider, request.url, data))
        if len(all_models) >= MAX_DISCOVERY_MODELS:
            truncated = True
            break
        if request.pagination is None:
            break
        cursor = next_page_cursor(request.pagination, data)
        if not cursor:
            break
    else:
        truncated = True  # page budget exhausted with a cursor still pending

    models = sorted(set(all_models))[:MAX_DISCOVERY_MODELS]
    by_id = {info.id: info for info in all_infos if info.id in models}
    details = tuple(by_id.get(model_id, ModelInfo(id=model_id)) for model_id in models)
    return DiscoveryResult(models=tuple(models), truncated=truncated, details=details)
