"""Role logic for first run setup: which roles exist, which provider and
model each starts with, and which message each test failure shows.

Pure: no Home Assistant imports, no I/O. The config flow calls it. See the
vault note "Nova Multi Provider First Run" for the reasons behind each rule.
"""
from __future__ import annotations

from .const import DEFAULT_VISION_MODEL

CLOUD: tuple[str, ...] = ("groq", "anthropic", "openai", "gemini")
ROLES: tuple[str, ...] = ("conversation", "classifier", "reasoning",
                          "camera_reasoning", "vision")
ROLE_KEYS: dict[str, tuple[str, str]] = {
    "conversation": ("llm_provider", "model"),
    "classifier": ("classifier_provider", "classifier_model"),
    "reasoning": ("reasoning_provider", "reasoning_model"),
    "camera_reasoning": ("camera_reasoning_provider", "camera_reasoning_model"),
    "vision": ("vision_provider", "vision_model"),
}
NOT_NOW = "not_now"
PROVIDER_LABELS: dict[str, str] = {
    "groq": "Groq", "anthropic": "Anthropic", "openai": "OpenAI",
    "gemini": "Gemini", "ollama": "Ollama",
}

_BACKGROUND = ("groq", "gemini", "openai", "anthropic", "ollama")
_ORDER: dict[str, tuple[str, ...]] = {
    "conversation": ("anthropic", "openai", "gemini", "groq", "ollama"),
    "classifier": _BACKGROUND,
    "reasoning": _BACKGROUND,
    "camera_reasoning": _BACKGROUND,
    "vision": ("gemini", "openai", "anthropic", "groq", "ollama"),
}
# The Ollama ability each role needs, as Ollama reports it.
_ABILITY = {"conversation": "tools", "vision": "vision"}

_ERROR_KEYS: dict[str, str] = {
    "model_not_found": "model_not_found",
    "access_denied": "model_access_denied",
    "authentication_failed": "invalid_auth",
    "missing_credential": "invalid_auth",
    "rate_limited": "test_incomplete",
    "timeout": "test_incomplete",
    "connection_failed": "test_incomplete",
    "provider_unavailable": "test_incomplete",
}


def provider_options(role: str, passed: list[str]) -> list[str]:
    """Providers that passed screen 1, in this role's order. Vision also
    offers "Not now"."""
    options = [p for p in _ORDER[role] if p in passed]
    if role == "vision":
        options.append(NOT_NOW)
    return options


def ollama_has(details: list[dict], ability: str) -> bool | None:
    """Whether any Ollama model reports `ability`. None when the server
    reports no abilities at all (older Ollama), so nothing can be told."""
    if not any(d.get("capabilities") for d in details):
        return None
    return any(ability in (d.get("capabilities") or ()) for d in details)


def default_provider(role: str, passed: list[str], ollama_vision: bool | None) -> str:
    """The provider filled in for `role`. "Not now" for vision when Ollama
    is the only provider and it reports no model able to read pictures."""
    options = [p for p in _ORDER[role] if p in passed]
    if role == "vision" and options == ["ollama"] and ollama_vision is False:
        return NOT_NOW
    return options[0]


def default_model(role: str, provider: str, cloud_defaults: dict[str, str],
                  details: list[dict]) -> str:
    """The model filled in. Empty on Ollama when no model reports the needed
    ability: lists can hold embedding models, so Nova never guesses."""
    if provider == "ollama":
        ability = _ABILITY.get(role, "completion")
        for d in details:
            if ability in (d.get("capabilities") or ()):
                return str(d.get("id") or "")
        return ""
    if role == "vision" and provider == "groq":
        return DEFAULT_VISION_MODEL
    return cloud_defaults.get(provider, "")


def key_error(kind: str, typed: bool) -> str | None:
    """Screen 1's result for a key tested with the default model: the key was
    refused, the check could not complete, or None when the key works but
    the default model is not available to it (screen 3 then picks a model).

    Access denied passes only for a key typed on screen 1. A saved key that
    is denied fails, so it is left out and noted on screen 2: a blank field
    keeps the saved key, so a key blocked everywhere would otherwise fail
    every model on screen 3, where there is no back button."""
    if kind in ("authentication_failed", "missing_credential"):
        return "invalid_auth"
    if kind == "model_not_found" or (kind == "access_denied" and typed):
        return None
    if kind == "access_denied":
        return "invalid_auth"
    return "cannot_connect"


def error_key(kind: str, picture: bool) -> str:
    """The setup screen error key for a ProviderErrorKind value. The picture
    message is used only for the picture test: a text role never sent one."""
    if kind == "unsupported_capability" and picture:
        return "model_no_pictures"
    return _ERROR_KEYS.get(kind, "test_failed")
