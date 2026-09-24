"""nova_config cache lifecycle tests."""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def config(load):
    return load("nova_config")


def test_reload_reads_external_disk_change(config, tmp_path, monkeypatch):
    path = tmp_path / "nova" / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"model": "first"}))
    monkeypatch.setattr(config, "CONFIG_PATH", path)

    assert config.load()["model"] == "first"
    path.write_text(json.dumps({"model": "second"}))
    assert config.get("model") == "first"
    assert config.reload()["model"] == "second"


def test_configure_invalidates_cache_on_integration_reload(config, tmp_path, monkeypatch):
    path = tmp_path / "nova" / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"model": "before"}))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    config.load()
    path.write_text(json.dumps({"model": "after"}))

    class _Config:
        @staticmethod
        def path(*parts):
            return str(tmp_path.joinpath(*parts))

    class _Hass:
        config = _Config()

    config.configure(_Hass())
    assert config.get("model") == "after"
