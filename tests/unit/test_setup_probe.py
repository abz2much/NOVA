"""Model tests for the first run setup screens (setup_probe.py)."""
from __future__ import annotations

import sys
import types

import pytest

if "aiohttp" not in sys.modules:
    _aiohttp = types.ModuleType("aiohttp")
    _aiohttp.ClientTimeout = lambda **kw: None
    _aiohttp.ClientSession = object
    sys.modules["aiohttp"] = _aiohttp


def test_rejects_images_matches_groq_text_only_error(load):
    errors = load("providers.errors")
    assert errors.rejects_images(Exception("messages[1].content must be a string")) is True
    assert errors.rejects_images(
        errors.ProviderError(errors.ProviderErrorKind.UNSUPPORTED_CAPABILITY, "groq")) is True
    assert errors.rejects_images(
        errors.ProviderError(errors.ProviderErrorKind.RATE_LIMITED, "groq")) is False


def test_default_vision_model_is_shared(load):
    const = load("const")
    assert const.DEFAULT_VISION_MODEL == "qwen/qwen3.6-27b"


class _HTTPError(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status_code = status


def test_openai_picture_refusal_is_unsupported_capability(load):
    errors = load("providers.errors")
    exc = _HTTPError(400, "Invalid content type. image_url is only supported by certain models.")
    assert errors.normalize_error(exc, "openai").kind is errors.ProviderErrorKind.UNSUPPORTED_CAPABILITY


def test_a_refused_key_is_authentication_failed(load):
    # Proves the chain behind "a key in the wrong field is refused": a 401
    # from the provider becomes authentication_failed (setup_roles maps that
    # to invalid_auth, tested in test_setup_roles.py).
    errors = load("providers.errors")
    assert errors.normalize_error(_HTTPError(401, "Incorrect API key provided"), "openai").kind \
        is errors.ProviderErrorKind.AUTHENTICATION_FAILED
    # Google answers a bad key with 400, not 401.
    for text in ("API key not valid. Please pass a valid API key.", "API_KEY_INVALID",
                 "Invalid API key"):
        assert errors.normalize_error(_HTTPError(400, text), "gemini").kind \
            is errors.ProviderErrorKind.AUTHENTICATION_FAILED, text
    # A 400 that is not about the key is unchanged.
    assert errors.normalize_error(_HTTPError(400, "bad field"), "gemini").kind \
        is errors.ProviderErrorKind.INVALID_REQUEST
