"""Provider availability rules (Phase 3, v7.108.0).

A provider is only ever "available" by its own evidence: a cloud provider by
its own credential, custom by its own saved endpoint, ollama always (it has
a working default). Never inferred from another provider's state.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "websocket.py"


@pytest.fixture
def compute():
    tree = ast.parse(SRC.read_text())
    node = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_compute_provider_availability"
    )
    namespace: dict = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SRC), "exec"), namespace)
    return namespace["_compute_provider_availability"]


def test_cloud_providers_available_only_with_their_own_credential(compute):
    status = {"groq": True, "openai": False, "anthropic": False, "gemini": False}
    available = compute(status, base_url_set=False)
    assert available["groq"] is True
    assert available["openai"] is False
    assert available["anthropic"] is False
    assert available["gemini"] is False


def test_one_providers_credential_never_marks_another_available(compute):
    """The exact shape of the requirement: never infer availability from
    another provider's key."""
    status = {"groq": True, "openai": True, "anthropic": False, "gemini": False}
    available = compute(status, base_url_set=False)
    assert available["anthropic"] is False
    assert available["gemini"] is False


def test_custom_available_only_when_endpoint_saved(compute):
    assert compute({}, base_url_set=False)["custom"] is False
    assert compute({}, base_url_set=True)["custom"] is True


def test_ollama_always_available(compute):
    assert compute({}, base_url_set=False)["ollama"] is True
    assert compute({}, base_url_set=True)["ollama"] is True


def test_credential_status_never_influences_custom_or_ollama(compute):
    """A cloud credential existing must never make custom/ollama look
    configured — they have their own, unrelated availability evidence."""
    status = {"groq": True, "openai": True, "anthropic": True, "gemini": True}
    available = compute(status, base_url_set=False)
    assert available["custom"] is False
    assert available["ollama"] is True  # true regardless, not because of any key


def test_returns_exactly_the_six_provider_families(compute):
    available = compute({}, base_url_set=False)
    assert set(available) == {"groq", "openai", "anthropic", "gemini", "custom", "ollama"}
