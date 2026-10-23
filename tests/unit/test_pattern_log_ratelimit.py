"""The base gap between two logged changes of one entity (the flood cap).

`pattern_log_min_interval` sets it, 60 seconds when unset, and 0 turns the
cap off. A value that does not read as a number falls back to the default.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def core(load):
    return load("cognitive_core")


@pytest.mark.parametrize("config,seconds", [
    pytest.param({}, 60.0, id="unset"),
    pytest.param(None, 60.0, id="no-config"),
    pytest.param({"pattern_log_min_interval": 30}, 30.0, id="configured"),
    pytest.param({"pattern_log_min_interval": "45"}, 45.0, id="numeric-text"),
    pytest.param({"pattern_log_min_interval": 0}, 0.0, id="disabled"),
    pytest.param({"pattern_log_min_interval": -20}, 0.0, id="negative-clamped"),
    pytest.param({"pattern_log_min_interval": "bad"}, 60.0, id="unreadable"),
    pytest.param({"pattern_log_min_interval": None}, 60.0, id="null"),
])
def test_the_base_interval(core, monkeypatch, config, seconds):
    monkeypatch.setattr(core._CORE, "config", config)
    got = core._pattern_log_interval()
    assert got == seconds
    assert isinstance(got, float)
