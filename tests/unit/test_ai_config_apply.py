"""Pure validation coverage for the staged AI configuration transaction."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest


SRC = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "websocket.py"


@pytest.fixture
def ai_config(load):
    llm = load("llm_provider")
    wanted = {
        "_AI_ROLE_FIELDS", "_AI_APPLY_KEYS", "_AI_PROVIDERS",
        "_AI_CLOUD_PROVIDERS", "_prepare_ai_config_updates",
        "_validate_ai_candidate",
    }
    tree = ast.parse(SRC.read_text())
    nodes = []
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if names & wanted:
                nodes.append(node)
                found.update(names & wanted)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted:
            nodes.append(node)
            found.add(node.name)
    assert found == wanted
    namespace = {
        "Any": Any,
        "resolve_provider_endpoint": llm.resolve_provider_endpoint,
        "__package__": "jc",
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SRC), "exec"), namespace)
    return namespace


def _all_roles(provider="groq", model="model-1"):
    return {
        "llm_provider": provider, "model": model,
        "classifier_provider": provider, "classifier_model": model,
        "reasoning_provider": provider, "reasoning_model": model,
        "vision_provider": provider, "vision_model": model,
        "camera_reasoning_provider": provider,
        "camera_reasoning_model": model,
    }


def test_prepare_normalises_bare_ollama_endpoint(ai_config):
    prepare = ai_config["_prepare_ai_config_updates"]
    assert prepare({"ollama_base_url": "ollama.lan"}) == {
        "ollama_base_url": "http://ollama.lan:11434",
    }


def test_prepare_rejects_unknown_keys_and_bad_context(ai_config):
    prepare = ai_config["_prepare_ai_config_updates"]
    with pytest.raises(ValueError):
        prepare({"api_key": "must-not-be-accepted"})
    with pytest.raises(ValueError):
        prepare({"ollama_num_ctx": 128})


def test_candidate_requires_each_cloud_providers_own_credential(ai_config):
    validate = ai_config["_validate_ai_candidate"]
    candidate = _all_roles("openai")
    candidate["groq_api_key"] = "unrelated-key"
    assert validate(candidate) == ["Add the openai credential before applying"]


def test_candidate_accepts_local_roles_without_a_cloud_key(ai_config):
    validate = ai_config["_validate_ai_candidate"]
    candidate = _all_roles("ollama", "local-tools:latest")
    candidate.update({
        "ollama_base_url": "http://ollama.lan:11434",
        "self_hosted_endpoints_migrated": True,
    })
    assert validate(candidate) == []


def test_candidate_requires_the_matching_self_hosted_endpoint(ai_config):
    validate = ai_config["_validate_ai_candidate"]
    candidate = _all_roles("custom", "private-model")
    candidate["self_hosted_endpoints_migrated"] = True
    assert validate(candidate) == ["Set the custom endpoint before applying"]
