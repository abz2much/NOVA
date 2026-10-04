"""Short, safe error text for the model and the panel.

An exception's own text can carry file paths, host names, library detail or
provider response bodies. What leaves the server is built from the exception
type only, plus three kinds of text that are safe by construction:

- a ProviderError's message (providers/errors.py builds it from the provider
  id, a fixed phrase, the HTTP status and an allow-listed code only);
- a NovaValidationError's message (Nova's own fixed validation text, shown
  in the settings screen);
- with ``keep_ha_text``, a HomeAssistantError's message. Home Assistant's own
  conversation chat log returns that text to the model as ``error_text``, so a
  tool error from Home Assistant reads the same through Nova.

The full exception stays in the Home Assistant log.
"""
from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

_MAX_LEN = 240


class NovaValidationError(ValueError):
    """A rejected request, in Nova's own words. Raise it only with fixed text
    Nova wrote (never a path, a URL or text from a library); its message is
    returned to the caller as it is. A ValueError, so existing handlers still
    catch it."""


def _is_ha_error(exc: BaseException) -> bool:
    try:
        from homeassistant.exceptions import HomeAssistantError
    except ImportError:
        return False
    return isinstance(exc, HomeAssistantError)


def safe_error_message(exc: BaseException, *, where: str = "",
                       log: bool = False, keep_ha_text: bool = False) -> str:
    """The text to return for `exc`. With ``log``, the full exception and its
    traceback go to the Home Assistant log first (for callers that do not log
    it themselves). A NovaValidationError is an expected rejection, not a
    failure, so it is never logged."""
    if log and not isinstance(exc, NovaValidationError):
        _LOGGER.warning("Nova: %s failed", where or "request", exc_info=exc)
    from .providers.errors import ProviderError
    name = type(exc).__name__
    if isinstance(exc, NovaValidationError):
        return str(exc)[:_MAX_LEN]
    if isinstance(exc, ProviderError) or (keep_ha_text and _is_ha_error(exc)):
        text = str(exc).strip()
        if text:
            return f"{name}: {text}"[:_MAX_LEN]
    return f"{name} (details are in the Home Assistant log)"
