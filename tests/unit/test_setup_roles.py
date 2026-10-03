"""Pure role logic for first run setup (setup_roles.py)."""
from __future__ import annotations

import pytest


@pytest.fixture
def sr(load):
    return load("setup_roles")


CLOUD_DEFAULTS = {"groq": "openai/gpt-oss-120b", "openai": "gpt-5-mini",
                  "gemini": "gemini-3.6-flash", "anthropic": "claude-sonnet-5"}


def test_role_keys_match_nova_settings(sr):
    assert sr.ROLE_KEYS == {
        "conversation": ("llm_provider", "model"),
        "classifier": ("classifier_provider", "classifier_model"),
        "reasoning": ("reasoning_provider", "reasoning_model"),
        "camera_reasoning": ("camera_reasoning_provider", "camera_reasoning_model"),
        "vision": ("vision_provider", "vision_model"),
    }


def test_provider_order_per_role(sr):
    every = ["ollama", "groq", "gemini", "openai", "anthropic"]
    assert sr.provider_options("conversation", every) == [
        "anthropic", "openai", "gemini", "groq", "ollama"]
    for role in ("classifier", "reasoning", "camera_reasoning"):
        assert sr.provider_options(role, every) == [
            "groq", "gemini", "openai", "anthropic", "ollama"]
    assert sr.provider_options("vision", every) == [
        "gemini", "openai", "anthropic", "groq", "ollama", "not_now"]


def test_only_passed_providers_are_offered(sr):
    assert sr.provider_options("conversation", ["groq"]) == ["groq"]
    assert sr.provider_options("vision", ["groq"]) == ["groq", "not_now"]


def test_default_provider_is_first_passed(sr):
    assert sr.default_provider("conversation", ["groq", "anthropic"], None) == "anthropic"
    assert sr.default_provider("classifier", ["groq", "anthropic"], None) == "groq"
    assert sr.default_provider("vision", ["groq", "anthropic"], None) == "anthropic"


def test_not_now_filled_in_for_ollama_without_picture_model(sr):
    assert sr.default_provider("vision", ["ollama"], False) == "not_now"
    # Unknown (old server): Ollama is offered, the model field stays empty.
    assert sr.default_provider("vision", ["ollama"], None) == "ollama"
    assert sr.default_provider("vision", ["ollama"], True) == "ollama"


def test_ollama_has_reports_unknown_when_no_abilities(sr):
    assert sr.ollama_has([{"id": "a", "capabilities": []}], "vision") is None
    assert sr.ollama_has([{"id": "a", "capabilities": ["completion"]}], "vision") is False
    assert sr.ollama_has([{"id": "a", "capabilities": ["completion", "vision"]}], "vision") is True


def test_cloud_default_models(sr):
    assert sr.default_model("conversation", "openai", CLOUD_DEFAULTS, []) == "gpt-5-mini"
    assert sr.default_model("vision", "groq", CLOUD_DEFAULTS, []) == "qwen/qwen3.6-27b"
    assert sr.default_model("vision", "gemini", CLOUD_DEFAULTS, []) == "gemini-3.6-flash"


def test_ollama_default_models_by_ability_never_guessing(sr):
    details = [
        {"id": "nomic-embed-text", "capabilities": ["embedding"]},
        {"id": "llama3.2", "capabilities": ["completion", "tools"]},
        {"id": "llava", "capabilities": ["completion", "vision"]},
    ]
    assert sr.default_model("conversation", "ollama", CLOUD_DEFAULTS, details) == "llama3.2"
    assert sr.default_model("classifier", "ollama", CLOUD_DEFAULTS, details) == "llama3.2"
    assert sr.default_model("vision", "ollama", CLOUD_DEFAULTS, details) == "llava"
    only_embed = [{"id": "nomic-embed-text", "capabilities": ["embedding"]}]
    assert sr.default_model("classifier", "ollama", CLOUD_DEFAULTS, only_embed) == ""
    old_server = [{"id": "a", "capabilities": []}, {"id": "b", "capabilities": []}]
    assert sr.default_model("conversation", "ollama", CLOUD_DEFAULTS, old_server) == ""


def test_error_key_per_kind(sr):
    assert sr.error_key("unsupported_capability", True) == "model_no_pictures"
    assert sr.error_key("model_not_found", False) == "model_not_found"
    assert sr.error_key("access_denied", False) == "model_access_denied"
    assert sr.error_key("authentication_failed", False) == "invalid_auth"
    for kind in ("rate_limited", "timeout", "connection_failed", "provider_unavailable"):
        assert sr.error_key(kind, True) == "test_incomplete"
    assert sr.error_key("malformed_response", False) == "test_failed"
    assert sr.error_key("something_new", False) == "test_failed"


def test_picture_message_only_for_the_picture_test(sr):
    # A text role never sent a picture, so it must not say it could not read one.
    assert sr.error_key("unsupported_capability", False) == "test_failed"


def test_screen_one_tells_refused_from_could_not_complete(sr):
    for typed in (True, False):
        for kind in ("authentication_failed", "missing_credential"):
            assert sr.key_error(kind, typed) == "invalid_auth"
        for kind in ("rate_limited", "timeout", "connection_failed", "provider_unavailable",
                     "malformed_response", "something_new"):
            assert sr.key_error(kind, typed) == "cannot_connect"


def test_a_key_that_only_lacks_the_default_model_works(sr):
    # The provider accepted the key but not the default model. The key is
    # good; screen 3 picks a model.
    assert sr.key_error("model_not_found", True) is None
    assert sr.key_error("model_not_found", False) is None
    # Access denied passes for a typed key (the user can see and change it on
    # screen 3) ...
    assert sr.key_error("access_denied", True) is None


def test_a_saved_key_denied_access_fails_instead_of_trapping(sr):
    # ... but not for a saved key: a blank field keeps the saved key, so a key
    # blocked everywhere would fail every model on screen 3 with no way back.
    assert sr.key_error("access_denied", False) == "invalid_auth"
