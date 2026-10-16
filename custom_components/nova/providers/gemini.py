"""Native Gemini adapter (Google's Interactions API).

Google documents the Interactions API as Beta and recommends generateContent
for stable production use, so its details can change. Everything that depends
on its wire format is in gemini_wire.py, which is the only file that would
need to follow a change.

Nova keeps its own history, so each call is one stateless request: ``store``
is false and the whole conversation is sent as steps, so nothing is kept on
Google's side for later turns. The wire format lives in gemini_wire.py.

The transport is the standard library, like native Ollama: no SDK and no
manifest requirement. The call runs in Home Assistant's executor (see
activity.py). Redirects are never followed: the key only ever goes to Google.
A busy Google (429 or 5xx) or a dropped connection is retried twice, briefly.
"""
from __future__ import annotations

import json
import logging
import random
import socket
import time
import urllib.request
from dataclasses import replace
from typing import Any, ClassVar, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from . import gemini_wire as wire
from .base import LLMProvider
from .capabilities import GEMINI_POLICY, ProviderCapabilities, gemini_vision
from .errors import ProviderError, ProviderErrorKind, _safe_code, kind_for_http_status
from .models import ChatRequest, ChatResponse, Usage

_LOGGER = logging.getLogger(__name__)

# One request is a few seconds. A request past this is a hang, not a slow
# success, and a hang is never retried.
GEMINI_TIMEOUT = 60.0
# A busy Google (429, 500, 502, 503) or a dropped connection is retried twice
# after a short wait. A bad request, a bad key and a missing model are not.
# 504 and timeouts are hangs, so they are not retried either.
RETRY_STATUSES = frozenset({429, 500, 502, 503})
RETRY_DELAYS = (1.0, 2.0)
RETRY_AFTER_CAP = 5.0       # a longer Retry-After is not waited for
RETRY_TOTAL_BUDGET = 30.0   # seconds across all tries; no retry starts past it
GEMINI_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_ERROR_BODY_BYTES = 4096


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _pause(seconds: float) -> None:
    """The single wait seam for retries (tests replace it)."""
    time.sleep(seconds)


def _urlopen(request: Request, timeout: float):  # type: ignore[no-untyped-def]
    """The single network seam for native Gemini calls."""
    return _OPENER.open(request, timeout=timeout)


def _total_usage(first: Usage, second: Usage) -> Usage:
    def add(a: Optional[int], b: Optional[int]) -> Optional[int]:
        return None if a is None and b is None else (a or 0) + (b or 0)
    return Usage(add(first.input_tokens, second.input_tokens),
                 add(first.output_tokens, second.output_tokens))


def _is_transient(exc: ProviderError) -> bool:
    if getattr(exc, "status", None) in RETRY_STATUSES:
        return True
    return exc.kind is ProviderErrorKind.CONNECTION_FAILED


class GeminiHTTPError(ProviderError):
    """An HTTP failure from Google. ``hint`` is the lower case error text,
    kept only so the adapter can decide on a retry; it is never part of the
    message."""

    hint: str = ""
    retry_after: Optional[float] = None


class GeminiProvider(LLMProvider):
    """Google Gemini through the Interactions API."""

    name: ClassVar[str] = "gemini"
    concurrency = GEMINI_POLICY
    requires_credential = True

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        if base_url and (urlparse(str(base_url)).hostname or "") != wire.GEMINI_HOST:
            # The native API only exists on Google's host. Never send the key
            # anywhere else.
            _LOGGER.warning("Gemini ignores a base URL that is not Google's")
        super().__init__(api_key, model, wire.GEMINI_API_BASE_URL)

    # ── Execution ───────────────────────────────────────────────────────────

    def complete(self, request: ChatRequest) -> ChatResponse:
        model = wire.bare_model(request.model or self.model)
        history = [m.to_openai() for m in request.messages]
        flatten, thinking = False, True
        for attempt in range(3):
            body = wire.build_request(request, model, flatten_tools=flatten, thinking=thinking)
            try:
                data = self._post(body)
                break
            except GeminiHTTPError as exc:
                if exc.status != 400 or attempt == 2:
                    raise
                if thinking and "thinking" in exc.hint and wire.thinking_level_for(model):
                    thinking = False       # a model that takes no thinking level
                    continue
                if (not flatten and wire.has_tool_turns(history) and not exc.detail
                        and exc.kind is ProviderErrorKind.INVALID_REQUEST):
                    # Any other refusal of a request that carries tool turns
                    # (a signature Gemini will not take, a reply shape a model
                    # does not accept): once more with the tool turns as plain
                    # text. A too large or blocked prompt is not retried.
                    flatten = True
                    continue
                raise
        response = wire.parse_interaction(data, model=model)
        if wire.is_incomplete(data) and not response.text and not response.tool_calls:
            # The budget ran out on thinking before any answer was written.
            # One retry with a larger budget; both calls are billed, so both
            # are counted.
            bigger = replace(request, max_tokens=max(request.max_tokens * 4, 2048))
            retry = wire.parse_interaction(
                self._post(wire.build_request(
                    bigger, model, flatten_tools=flatten, thinking=thinking)),
                model=model)
            response = replace(retry, usage=_total_usage(response.usage, retry.usage))
        return response

    def _post(self, body: dict[str, Any]) -> Any:
        """One request with up to two short retries for a busy Google or a
        dropped connection."""
        started = time.monotonic()
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                return self._post_once(body)
            except ProviderError as exc:
                if attempt == len(RETRY_DELAYS) or not _is_transient(exc):
                    raise
                delay = RETRY_DELAYS[attempt]
                after = getattr(exc, "retry_after", None)
                if after is not None:
                    delay = min(after, RETRY_AFTER_CAP)
                delay += random.uniform(0, 0.25)
                if time.monotonic() - started + delay > RETRY_TOTAL_BUDGET:
                    raise
                _LOGGER.info("Gemini busy (%s), retrying in %.1f seconds",
                             getattr(exc, "status", None) or exc.kind.value, delay)
                _pause(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _post_once(self, body: dict[str, Any]) -> Any:
        http_request = Request(
            f"{self.base_url}/interactions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            method="POST",
        )
        try:
            with _urlopen(http_request, timeout=GEMINI_TIMEOUT) as response:
                raw_bytes = response.read(GEMINI_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise self._http_error(exc) from exc
        except URLError as exc:
            kind = (ProviderErrorKind.TIMEOUT
                    if isinstance(exc.reason, (TimeoutError, socket.timeout))
                    else ProviderErrorKind.CONNECTION_FAILED)
            raise ProviderError(kind, self.name) from exc
        if len(raw_bytes) > GEMINI_MAX_RESPONSE_BYTES:
            raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, self.name,
                                detail="Gemini returned an oversized response")
        try:
            return json.loads(raw_bytes)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, self.name,
                                detail="Gemini returned an invalid JSON response") from exc

    def _http_error(self, exc: HTTPError) -> GeminiHTTPError:
        """Classify an HTTP failure. The body is read (bounded) only to tell a
        bad key, a missing model, a safety block or an oversized prompt apart;
        none of its text reaches a message."""
        try:
            text = exc.read(_ERROR_BODY_BYTES).decode("utf-8", errors="replace")
        except Exception:
            text = ""
        code: Optional[str] = None
        try:
            error = json.loads(text).get("error")
        except (ValueError, AttributeError):
            error = None
        if isinstance(error, dict):
            code = _safe_code(error.get("status"))
            text = str(error.get("message") or text)
        status = exc.code
        detail: Optional[str] = None
        kind = kind_for_http_status(status, text)
        if status in (400, 413) and wire.is_too_large(text):
            kind, detail = ProviderErrorKind.INVALID_REQUEST, wire.TOO_LARGE_DETAIL
        elif status == 400 and kind is ProviderErrorKind.INVALID_REQUEST and wire.has_safety_words(text):
            detail = wire.SAFETY_DETAIL
        error_out = GeminiHTTPError(kind, self.name, status=status, code=code, detail=detail)
        error_out.hint = text.lower()
        try:
            after = float(str(exc.headers.get("Retry-After") or "").strip())
            error_out.retry_after = after if 0 <= after < 3600 else None
        except (AttributeError, ValueError, TypeError):
            pass
        return error_out

    def capabilities(self, model: Optional[str] = None) -> ProviderCapabilities:
        return ProviderCapabilities(vision=gemini_vision(model or self.model))
