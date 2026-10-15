"""nova_config.effective_config: which value a run actually uses.

The entry's data, then options, then config.json on top, with only real
config.json values counting, so a blank panel field leaves a key the entry
carries alone. After the staged AI setup has split the self hosted
endpoints, a blank endpoint in config.json is a deliberate clear and wins.
"""
from types import SimpleNamespace

import pytest


@pytest.fixture
def nc(load):
    return load("nova_config")


def _entry(data=None, options=None):
    return SimpleNamespace(data=data, options=options)


def _panel(nc, monkeypatch, values):
    monkeypatch.setattr(nc, "get_all", lambda: dict(values))


def test_config_json_beats_both_entry_layers(nc, monkeypatch):
    _panel(nc, monkeypatch, {"llm_provider": "anthropic", "model": "m-panel"})
    got = nc.effective_config(_entry(
        data={"llm_provider": "groq", "groq_api_key": "G"},
        options={"llm_provider": "openai", "model": "m-options"}))
    assert (got["llm_provider"], got["model"]) == ("anthropic", "m-panel")
    assert got["groq_api_key"] == "G"


@pytest.mark.parametrize("blank", ["", None])
def test_a_blank_config_json_value_keeps_the_entry_value(nc, monkeypatch, blank):
    _panel(nc, monkeypatch, {"api_key": blank, "ollama_url": blank})
    got = nc.effective_config(_entry(data={"api_key": "SECRET",
                                           "ollama_url": "http://box:11434"}))
    assert got["api_key"] == "SECRET"
    assert got["ollama_url"] == "http://box:11434"


def test_after_the_split_a_blank_endpoint_clears_the_old_one(nc, monkeypatch):
    _panel(nc, monkeypatch, {"self_hosted_endpoints_migrated": True,
                             "ollama_base_url": "http://now:11434",
                             "custom_base_url": ""})
    got = nc.effective_config(_entry(options={
        "ollama_base_url": "http://then:11434",
        "custom_base_url": "https://then.example/v1"}))
    assert got["ollama_base_url"] == "http://now:11434"
    assert got["custom_base_url"] == ""


def test_before_the_split_a_blank_endpoint_is_just_blank(nc, monkeypatch):
    _panel(nc, monkeypatch, {"custom_base_url": ""})
    got = nc.effective_config(_entry(data={"custom_base_url": "https://kept.example"}))
    assert got["custom_base_url"] == "https://kept.example"


def test_runtime_get_also_treats_a_split_blank_endpoint_as_cleared(nc, monkeypatch):
    stored = {"self_hosted_endpoints_migrated": True, "custom_base_url": ""}
    monkeypatch.setattr(nc, "get", lambda key, default=None: stored.get(key, default))
    _panel(nc, monkeypatch, stored)
    entry = _entry(data={"custom_base_url": "https://then.example/v1"})
    assert nc.runtime_get(None, entry, "custom_base_url", "dflt") == ""


def test_options_beat_data_when_config_json_is_silent(nc, monkeypatch):
    _panel(nc, monkeypatch, {})
    got = nc.effective_config(_entry(data={"k": "data"}, options={"k": "options"}))
    assert got["k"] == "options"


def test_keys_from_each_source_all_arrive(nc, monkeypatch):
    _panel(nc, monkeypatch, {"model": "m-panel"})
    got = nc.effective_config(_entry(data={"honorific": "madam"}))
    assert got["honorific"] == "madam" and got["model"] == "m-panel"


def test_without_an_entry_only_real_config_json_values_come_back(nc, monkeypatch):
    _panel(nc, monkeypatch, {"kept": "yes", "dropped": ""})
    assert nc.effective_config(None) == {"kept": "yes"}


def test_an_entry_with_no_data_or_options_is_fine(nc, monkeypatch):
    _panel(nc, monkeypatch, {"kept": "yes"})
    assert nc.effective_config(_entry()) == {"kept": "yes"}
