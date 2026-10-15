"""nova_config.effective_config_with_runtime, used when a subsystem starts.

A panel install has empty entry data and options, so merging only those
loses every config.json setting. This view keeps the entry, config.json and
the panel's live values, live ones on top and blanks ignored at each layer.
"""
from types import SimpleNamespace

import pytest


@pytest.fixture
def nc(load, monkeypatch):
    monkeypatch.setattr(load("ha_secrets"), "overlay_credentials", lambda d: d)
    return load("nova_config")


def test_every_layer_arrives_with_live_values_on_top(nc, monkeypatch):
    monkeypatch.setattr(nc, "get_all", lambda: {
        "llm_provider": "anthropic", "model": "m-file", "file_blank": ""})
    entry = SimpleNamespace(data={"honorific": "madam"}, options={"model": "m-opt"})
    got = nc.effective_config_with_runtime(
        entry, {"model": "m-live", "live_blank": "", "live_none": None})
    assert got == {"honorific": "madam", "llm_provider": "anthropic",
                   "model": "m-live"}


@pytest.mark.parametrize("live", [None, {}])
def test_no_live_values_is_the_plain_effective_config(nc, monkeypatch, live):
    monkeypatch.setattr(nc, "get_all", lambda: {"x": "1"})
    entry = SimpleNamespace(data={}, options={"y": "2"})
    assert nc.effective_config_with_runtime(entry, live) == nc.effective_config(entry)
    assert nc.effective_config_with_runtime(entry, live) == {"x": "1", "y": "2"}
