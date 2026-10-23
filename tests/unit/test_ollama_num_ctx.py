"""Ollama's context window comes from the ``ollama_num_ctx`` setting, with
8192 as the default (v7.46.0). Values that cannot be a real window fall back
to the default rather than reaching the server."""
from __future__ import annotations

import sys
import types

import pytest

DEFAULT_WINDOW = 8192


@pytest.fixture
def llm(load):
    return load("llm_provider")


@pytest.fixture
def set_window(monkeypatch):
    """Install a stand-in settings module answering ``ollama_num_ctx``.

    Returns the list of keys asked for, so a test can see what was read."""
    asked: list[str] = []

    def _install(configured):
        settings = types.ModuleType("jc.nova_config")

        def get(key, default=None):
            asked.append(key)
            return configured if key == "ollama_num_ctx" else default

        settings.get = get
        monkeypatch.setitem(sys.modules, "jc.nova_config", settings)
        return asked

    return _install


def _window(llm) -> int:
    # _num_ctx is a static method reading settings only, so no client is built.
    return llm.OllamaProvider._num_ctx()


def test_default_constant_is_8192(llm):
    assert llm.OLLAMA_NUM_CTX == DEFAULT_WINDOW


@pytest.mark.parametrize("configured, expected", [
    pytest.param(None, DEFAULT_WINDOW, id="unset"),
    pytest.param(0, DEFAULT_WINDOW, id="zero"),
    pytest.param("", DEFAULT_WINDOW, id="blank"),
    pytest.param(16, DEFAULT_WINDOW, id="far-too-small"),
    pytest.param(511, DEFAULT_WINDOW, id="just-below-floor"),
    pytest.param(-4096, DEFAULT_WINDOW, id="negative"),
    pytest.param("not-a-number", DEFAULT_WINDOW, id="garbage-text"),
    pytest.param([4096], DEFAULT_WINDOW, id="wrong-type"),
    pytest.param(512, 512, id="floor-is-accepted"),
    pytest.param(32768, 32768, id="large"),
    pytest.param("16384", 16384, id="numeric-text"),
])
def test_window_from_setting(llm, set_window, configured, expected):
    asked = set_window(configured)
    assert _window(llm) == expected
    assert "ollama_num_ctx" in asked


def test_instance_call_matches_static_call(llm, set_window):
    set_window(32768)
    bare = llm.OllamaProvider.__new__(llm.OllamaProvider)
    assert bare._num_ctx() == 32768


def test_settings_that_raise_fall_back(llm, monkeypatch):
    broken = types.ModuleType("jc.nova_config")

    def get(key, default=None):
        raise RuntimeError("settings store unreadable")

    broken.get = get
    monkeypatch.setitem(sys.modules, "jc.nova_config", broken)
    assert _window(llm) == DEFAULT_WINDOW
