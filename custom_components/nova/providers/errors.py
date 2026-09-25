"""Normalized provider errors.

Every failure that leaves the provider package is a ProviderError with a
closed ``kind``. The original exception is kept as ``__cause__`` (exception
chaining) for internal diagnosis, but a ProviderError's own message and repr
are built only from safe fields: the provider id, the kind, an HTTP status and
an allow-listed provider error code. Secrets, headers, request bodies and
response bodies never appear in them.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import re
import socket
from enum import Enum
from typing import Optional


class ProviderErrorKind(str, Enum):
    MISSING_CREDENTIAL = "missing_credential"
    AUTHENTICATION_FAILED = "authentication_failed"
    ACCESS_DENIED = "access_denied"
    INVALID_ENDPOINT = "invalid_endpoint"
    MODEL_NOT_FOUND = "model_not_found"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    CONNECTION_FAILED = "connection_failed"
    MALFORMED_RESPONSE = "malformed_response"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


# A fixed, human-readable phrase per kind. The phrases keep the vocabulary
# older callers matched on ("timeout", "connection", "rate limit", "model ...
# not found", "request too large") without ever echoing provider text.
_PHRASES = {
    ProviderErrorKind.MISSING_CREDENTIAL: "no credential is configured",
    ProviderErrorKind.AUTHENTICATION_FAILED: "authentication failed (invalid api key)",
    ProviderErrorKind.ACCESS_DENIED: "access denied (permission)",
    ProviderErrorKind.INVALID_ENDPOINT: "the endpoint is invalid or not configured",
    ProviderErrorKind.MODEL_NOT_FOUND: "model not found",
    ProviderErrorKind.INVALID_REQUEST: "the provider rejected the request",
    ProviderErrorKind.UNSUPPORTED_CAPABILITY: "the model does not support this input",
    ProviderErrorKind.RATE_LIMITED: "rate limit exceeded (too many requests)",
    ProviderErrorKind.TIMEOUT: "request timed out (timeout)",
    ProviderErrorKind.CONNECTION_FAILED: "connection failed",
    ProviderErrorKind.MALFORMED_RESPONSE: "the provider returned a malformed response",
    ProviderErrorKind.PROVIDER_UNAVAILABLE: "the provider is unavailable",
    ProviderErrorKind.CANCELLED: "the request was cancelled",
    ProviderErrorKind.UNKNOWN: "the provider call failed",
}

RETRYABLE_KINDS = frozenset({
    ProviderErrorKind.RATE_LIMITED,
    ProviderErrorKind.TIMEOUT,
    ProviderErrorKind.CONNECTION_FAILED,
    ProviderErrorKind.PROVIDER_UNAVAILABLE,
})

_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_SAFE_PROVIDER = re.compile(r"^[a-z0-9_\-]{1,32}$")


def _safe_code(value) -> Optional[str]:
    if isinstance(value, str) and _SAFE_CODE.match(value):
        return value
    return None


class ProviderError(RuntimeError):
    """A normalized provider failure. See the module docstring."""

    def __init__(
        self,
        kind: ProviderErrorKind,
        provider: str = "",
        *,
        status: Optional[int] = None,
        code: Optional[str] = None,
        detail: Optional[str] = None,
    ):
        self.kind = ProviderErrorKind(kind)
        provider = str(provider or "").strip().lower()
        self.provider = provider if _SAFE_PROVIDER.match(provider) else "provider"
        self.status = status if isinstance(status, int) and not isinstance(status, bool) else None
        self.code = _safe_code(code)
        # `detail` is only ever a fixed phrase chosen by Nova's own code
        # (never provider or user text).
        self.detail = detail
        super().__init__(self._message())

    def _message(self) -> str:
        phrase = self.detail or _PHRASES[self.kind]
        extras = [self.kind.value]
        if self.status is not None:
            extras.append(f"HTTP {self.status}")
        if self.code:
            extras.append(f"code {self.code}")
        return f"{self.provider}: {phrase} ({', '.join(extras)})"

    def __repr__(self) -> str:
        return f"ProviderError({self._message()!r})"

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    def diagnostic_text(self) -> str:
        """In-process text for classification heuristics: this error's own
        safe message plus its chained original. Never log or return it."""
        cause = self.__cause__
        return f"{self}\n{cause}" if cause is not None else str(self)


class ProviderConfigurationError(ProviderError, ValueError):
    """A provider that cannot be built from its configuration (missing
    endpoint, unknown provider). A ValueError too, as before."""


class ProviderRequestError(ProviderError, ValueError):
    """A request Nova itself cannot translate for the provider (for example
    a remote image URL for native Ollama). A ValueError too, as before."""


def error_text(exc: BaseException) -> str:
    """Text a legacy heuristic may match on: a ProviderError's safe message
    plus its chained original, or the exception's own text."""
    if isinstance(exc, ProviderError):
        return exc.diagnostic_text()
    return str(exc)


def _status_of(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def _code_of(exc: BaseException) -> Optional[str]:
    """An allow-listed machine code from an SDK error body, e.g.
    'tool_use_failed' or 'context_length_exceeded'. Never free text."""
    for attr in ("code", "type"):
        code = _safe_code(getattr(exc, attr, None))
        if code:
            return code
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        for key in ("code", "type"):
            code = _safe_code(error.get(key))
            if code:
                return code
    return None


_TOO_LARGE = ("request too large", "too large for model", "context length",
              "maximum context", "prompt is too long", "input is too long")
_VISION_REJECTED = ("must be a string", "does not support image", "image input is not supported")


def _kind_for_status(status: int, text: str) -> ProviderErrorKind:
    if status == 401:
        return ProviderErrorKind.AUTHENTICATION_FAILED
    if status == 403:
        return ProviderErrorKind.ACCESS_DENIED
    if status == 404:
        if "model" in text:
            return ProviderErrorKind.MODEL_NOT_FOUND
        return ProviderErrorKind.INVALID_ENDPOINT
    if status in (408, 504):
        return ProviderErrorKind.TIMEOUT
    if status == 429:
        return ProviderErrorKind.RATE_LIMITED
    if status >= 500:
        return ProviderErrorKind.PROVIDER_UNAVAILABLE
    if status in (400, 422) and any(k in text for k in _VISION_REJECTED):
        return ProviderErrorKind.UNSUPPORTED_CAPABILITY
    if 300 <= status < 400:
        return ProviderErrorKind.INVALID_ENDPOINT
    if 400 <= status < 500:
        return ProviderErrorKind.INVALID_REQUEST
    return ProviderErrorKind.UNKNOWN


def normalize_error(exc: BaseException, provider: str = "") -> ProviderError:
    """Map any exception from a provider call to a ProviderError chained to
    it. asyncio.CancelledError is never normalized: callers must let task
    cancellation propagate untouched."""
    if isinstance(exc, asyncio.CancelledError):
        raise TypeError("asyncio.CancelledError must propagate, not be normalized")
    if isinstance(exc, ProviderError):
        return exc
    text = str(exc).lower()
    name = type(exc).__name__
    status = _status_of(exc)
    code = _code_of(exc)

    if isinstance(exc, concurrent.futures.CancelledError):
        kind = ProviderErrorKind.CANCELLED
    elif "Timeout" in name or isinstance(exc, (TimeoutError, socket.timeout)):
        kind = ProviderErrorKind.TIMEOUT
    elif status is not None:
        kind = _kind_for_status(status, text)
    elif name in ("APIConnectionError", "ConnectError", "URLError") or isinstance(
            exc, (ConnectionError, socket.gaierror)):
        kind = ProviderErrorKind.CONNECTION_FAILED
    elif any(k in text for k in _TOO_LARGE):
        kind = ProviderErrorKind.INVALID_REQUEST
    elif any(k in text for k in ("401", "unauthorized", "invalid api key",
                                 "invalid_api_key", "api key", "api_key", "auth")):
        kind = ProviderErrorKind.AUTHENTICATION_FAILED
    elif any(k in text for k in ("403", "permission", "forbidden")):
        kind = ProviderErrorKind.ACCESS_DENIED
    elif "429" in text or "rate limit" in text or "too many requests" in text:
        kind = ProviderErrorKind.RATE_LIMITED
    elif any(k in text for k in ("timed out", "timeout")):
        kind = ProviderErrorKind.TIMEOUT
    elif any(k in text for k in ("connect", "refused", "unreachable", "getaddrinfo",
                                 "name or service", "resolve", "network")):
        kind = ProviderErrorKind.CONNECTION_FAILED
    elif any(k in text for k in ("503", "502", "500", "unavailable", "overloaded")):
        kind = ProviderErrorKind.PROVIDER_UNAVAILABLE
    elif ("not_found" in text or "404" in text) and "model" in text:
        kind = ProviderErrorKind.MODEL_NOT_FOUND
    elif isinstance(exc, (ValueError, KeyError, TypeError)) or name == "JSONDecodeError":
        kind = ProviderErrorKind.MALFORMED_RESPONSE
    elif isinstance(exc, OSError):
        kind = ProviderErrorKind.CONNECTION_FAILED
    else:
        kind = ProviderErrorKind.UNKNOWN
    error = ProviderError(kind, provider, status=status, code=code)
    error.__cause__ = exc
    return error


# Config-flow error keys (strings.json config.error.*) — unchanged contract.
_CONFIG_FLOW_KEYS = {
    ProviderErrorKind.MISSING_CREDENTIAL: "invalid_auth",
    ProviderErrorKind.AUTHENTICATION_FAILED: "invalid_auth",
    ProviderErrorKind.ACCESS_DENIED: "invalid_auth",
    ProviderErrorKind.TIMEOUT: "cannot_connect",
    ProviderErrorKind.CONNECTION_FAILED: "cannot_connect",
    ProviderErrorKind.INVALID_ENDPOINT: "cannot_connect",
    ProviderErrorKind.PROVIDER_UNAVAILABLE: "cannot_connect",
}


def _legacy_config_flow_key(exc: BaseException) -> str:
    msg = str(exc).lower()
    if any(t in msg for t in ("auth", "api key", "api_key", "401", "403",
                              "unauthorized", "invalid key", "permission")):
        return "invalid_auth"
    if any(t in msg for t in ("connect", "timeout", "timed out", "refused",
                              "resolve", "unreachable", "getaddrinfo",
                              "name or service", "connection", "network")):
        return "cannot_connect"
    return "unknown"


def config_flow_error_key(exc: BaseException) -> str:
    """'invalid_auth' | 'cannot_connect' | 'unknown' for a failure.

    The original exception is classified exactly as before; the normalized
    kind only decides a case that classification left as 'unknown'."""
    if isinstance(exc, ProviderError):
        if exc.__cause__ is not None:
            legacy = _legacy_config_flow_key(exc.__cause__)
            if legacy != "unknown":
                return legacy
        return _CONFIG_FLOW_KEYS.get(exc.kind, "unknown")
    return _legacy_config_flow_key(exc)
