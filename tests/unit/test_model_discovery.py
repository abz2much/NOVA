"""Security and parsing coverage for ``nova/list_models``."""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from urllib.parse import urlparse

import pytest


SRC = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "websocket.py"


def _load_model_discovery_functions():
    """Load only the pure model discovery helpers from websocket.py."""
    wanted = {
        "_CLOUD_MODEL_ENDPOINTS",
        "_PROVIDER_CREDENTIAL_KEYS",
        "_SAFE_MODEL_DISCOVERY_ERROR",
        "_resolve_model_discovery_request",
        "_parse_model_list",
        "_log_model_discovery_failure",
    }
    tree = ast.parse(SRC.read_text())
    nodes = []
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if names & wanted:
                nodes.append(node)
                found.update(names & wanted)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            nodes.append(node)
            found.add(node.name)

    missing = wanted - found
    assert not missing, f"model discovery helpers missing: {sorted(missing)}"

    namespace = {
        "logging": logging,
        "urlparse": urlparse,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SRC), "exec"), namespace)
    return namespace


@pytest.fixture
def discovery():
    return _load_model_discovery_functions()


@pytest.mark.parametrize(
    ("provider", "endpoint", "credential_key", "header_name", "header_value"),
    [
        ("groq", "https://api.groq.com/openai/v1/models", "groq_api_key",
         "Authorization", "Bearer groq-secret"),
        ("openai", "https://api.openai.com/v1/models", "openai_api_key",
         "Authorization", "Bearer openai-secret"),
        ("anthropic", "https://api.anthropic.com/v1/models", "anthropic_api_key",
         "x-api-key", "anthropic-secret"),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/models",
         "gemini_api_key", "x-goog-api-key", "gemini-secret"),
    ],
)
def test_cloud_providers_use_fixed_endpoints_and_named_credentials(
    discovery, provider, endpoint, credential_key, header_name, header_value,
):
    resolve = discovery["_resolve_model_discovery_request"]
    config = {
        "llm_provider": "custom",
        "llm_base_url": "https://attacker.invalid/v1?key=stolen",
        credential_key: header_value.removeprefix("Bearer "),
    }

    url, headers = resolve(config, provider)

    assert url == endpoint
    assert headers[header_name] == header_value
    assert "attacker.invalid" not in url


@pytest.mark.parametrize("provider", ["groq", "openai", "anthropic", "gemini"])
def test_shared_key_is_used_only_for_matching_saved_cloud_provider(discovery, provider):
    resolve = discovery["_resolve_model_discovery_request"]

    matching_url, matching_headers = resolve(
        {"llm_provider": provider, "api_key": "matching-primary"}, provider,
    )
    _, unrelated_headers = resolve(
        {"llm_provider": "groq", "api_key": "groq-primary"}, provider,
    )

    assert matching_url.startswith("https://")
    assert "matching-primary" in " ".join(matching_headers.values())
    if provider != "groq":
        assert "groq-primary" not in " ".join(unrelated_headers.values())


@pytest.mark.parametrize(
    ("provider", "saved_url", "expected_url"),
    [
        ("ollama", "http://ollama.lan:11434", "http://ollama.lan:11434/api/tags"),
        ("ollama", "http://10.0.0.8:11434/v1", "http://10.0.0.8:11434/v1/models"),
        ("custom", "https://models.example.test/v1", "https://models.example.test/v1/models"),
    ],
)
def test_local_and_custom_use_only_saved_server_side_url_without_credentials(
    discovery, provider, saved_url, expected_url,
):
    resolve = discovery["_resolve_model_discovery_request"]
    config = {
        "llm_provider": provider,
        "llm_base_url": saved_url,
        "api_key": "shared-cloud-secret",
        "base_url": "https://attacker.invalid/v1",
        "request_base_url": "https://attacker.invalid/v1",
    }

    url, headers = resolve(config, provider)

    assert url == expected_url
    assert headers == {}
    assert "attacker.invalid" not in url


def test_local_ollama_discovery_needs_no_cloud_key(discovery):
    resolve = discovery["_resolve_model_discovery_request"]

    url, headers = resolve(
        {"llm_provider": "ollama", "llm_base_url": "http://192.168.1.20:11434"},
        "ollama",
    )

    assert url == "http://192.168.1.20:11434/api/tags"
    assert headers == {}


@pytest.mark.parametrize(
    ("provider", "url", "payload", "expected"),
    [
        ("groq", "https://api.groq.com/openai/v1/models",
         {"data": [{"id": "z"}, {"id": "a"}, {"id": "a"}]}, ["a", "z"]),
        ("openai", "https://api.openai.com/v1/models",
         {"data": [{"id": "gpt-b"}, {"id": "gpt-a"}]}, ["gpt-a", "gpt-b"]),
        ("anthropic", "https://api.anthropic.com/v1/models",
         {"data": [{"id": "claude-b"}, {"id": "claude-a"}]}, ["claude-a", "claude-b"]),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/models",
         {"models": [
             {"name": "models/gemini-chat", "supportedGenerationMethods": ["generateContent"]},
             {"name": "models/embed-only", "supportedGenerationMethods": ["embedContent"]},
         ]}, ["gemini-chat"]),
        ("ollama", "http://ollama.lan:11434/api/tags",
         {"models": [{"name": "qwen:latest"}, {"name": "llama:latest"}]},
         ["llama:latest", "qwen:latest"]),
        ("ollama", "http://ollama.lan:11434/v1/models",
         {"data": [{"id": "qwen"}, {"id": "llama"}]}, ["llama", "qwen"]),
        ("custom", "https://models.example.test/v1/models",
         {"data": [{"id": "private-b"}, {"id": "private-a"}]},
         ["private-a", "private-b"]),
    ],
)
def test_existing_model_list_parsing_is_preserved(
    discovery, provider, url, payload, expected,
):
    parse = discovery["_parse_model_list"]
    assert parse(provider, url, payload) == expected


def test_failure_log_contains_only_sanitised_metadata(discovery, caplog):
    log_failure = discovery["_log_model_discovery_failure"]

    class UpstreamFailure(RuntimeError):
        status = 401

    with caplog.at_level(logging.INFO, logger="nova.model_discovery.test"):
        log_failure(
            "custom\nAuthorization: Bearer provider-secret",
            "https://user:password@models.example.test/v1/models?key=query-secret",
            UpstreamFailure("body-secret Authorization: Bearer header-secret"),
            logger=logging.getLogger("nova.model_discovery.test"),
        )

    text = caplog.text
    assert "provider=unknown" in text
    assert "host=models.example.test" in text
    assert "status=401" in text
    assert "error=UpstreamFailure" in text
    for secret in (
        "password", "query-secret", "body-secret", "header-secret",
        "provider-secret", "Bearer",
    ):
        assert secret not in text
