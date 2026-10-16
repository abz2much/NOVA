"""Native Gemini adapter (Google's Interactions API).

Nova keeps its own history, so each call is one stateless request: ``store``
is false and the whole conversation is sent as steps, so nothing is kept on
Google's side for later turns. The wire format lives in gemini_wire.py.

The transport is the standard library, like native Ollama: no SDK and no
manifest requirement. The call runs in Home Assistant's executor (see
activity.py). Redirects are never followed: the key only ever goes to Google.
"""
from __future__ import annotations

import json
import logging
import socket
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
# success, and there is no retry loop: the caller decides what to do.
GEMINI_TIMEOUT = 60.0
GEMINI_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_ERROR_BODY_BYTES = 4096


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _urlopen(request: Request, timeout: float):  # type: ignore[no-untyped-def]
    """The single network seam for native Gemini calls."""
    return _OPENER.open(request, timeout=timeout)


def _total_usage(first: Usage, second: Usage) -> Usage:
    def add(a: Optional[int], b: Optional[int]) -> Optional[int]:
        return None if a is None and b is None else (a or 0) + (b or 0)
    return Usage(add(first.input_tokens, second.input_tokens),
                 add(first.output_tokens, second.output_tokens))


class GeminiHTTPError(ProviderError):
    """An HTTP failure from Google. ``hint`` is the lower case error text,
    kept only so the adapter can decide on a retry; it is never part of the
    message."""

    hint: str = ""


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
                if (not flatten and wire.has_tool_turns(history)
                        and ("signature" in exc.hint or "thought" in exc.hint)):
                    flatten = True         # history without Gemini's own reasoning
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
        return error_out

    def capabilities(self, model: Optional[str] = None) -> ProviderCapabilities:
        return ProviderCapabilities(vision=gemini_vision(model or self.model))
