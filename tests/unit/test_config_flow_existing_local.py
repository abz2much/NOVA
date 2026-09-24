"""Existing local-only installs are detected during config flow."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path


SRC = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "config_flow.py"


def _find_config_function():
    tree = ast.parse(SRC.read_text())
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_find_config"
    )
    namespace = {"json": json, "os": os, "CONF_API_KEY": "api_key"}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SRC), "exec"), namespace)
    return namespace["_find_config"]


def test_finds_ollama_only_config_without_cloud_key(tmp_path):
    path = tmp_path / "config.json"
    data = {
        "llm_provider": "ollama",
        "ollama_base_url": "http://gpu.local:11434",
        "model": "local-model",
    }
    path.write_text(json.dumps(data))
    assert _find_config_function()(str(path)) == data


def test_finds_legacy_local_only_config(tmp_path):
    path = tmp_path / "config.json"
    data = {
        "llm_provider": "ollama",
        "llm_base_url": "http://gpu.local:11434",
    }
    path.write_text(json.dumps(data))
    assert _find_config_function()(str(path)) == data
