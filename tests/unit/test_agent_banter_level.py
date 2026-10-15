"""The AI reply tone follows the Character setting and the mode (8.49.39).

The agent prompt read banter_level as ``int(value or 1)``, so the Plain
setting (0) came out as the dry default, and it never looked at the active
mode, while the canned lines follow the mode first. Now both decide in the
same order: the mode's tone, then the setting, then dry.
"""
import sys

import pytest


@pytest.fixture
def ctx(load, monkeypatch):
    load("agent")
    mod = sys.modules["jc.agent_runtime.context"]
    settings = {}
    monkeypatch.setattr(load("nova_config"), "get",
                        lambda key, default=None: settings.get(key, default))
    mode = {"banter": None}
    monkeypatch.setattr(load("modes"), "mode_banter_level", lambda: mode["banter"])
    mod._settings, mod._mode = settings, mode
    return mod


def _tone(ctx):
    line = ctx._banter_guidance()
    if "strictly plain" in line:
        return "plain"
    if "lean into the character" in line:
        return "full"
    assert line == ""
    return "dry"


@pytest.mark.parametrize("setting, expected", [
    (0, "plain"), ("0", "plain"), (1, "dry"), (2, "full"), (None, "dry"),
    ("", "dry"), ("junk", "dry"), (7, "full"), (-3, "plain"),
])
def test_the_character_setting_decides_with_no_mode_tone(ctx, setting, expected):
    if setting is not None:
        ctx._settings["banter_level"] = setting
    assert _tone(ctx) == expected


@pytest.mark.parametrize("mode_tone, setting, expected", [
    (0, 2, "plain"),     # movie: plain even with full wit chosen
    (2, 0, "full"),      # party: full wit even with plain chosen
    (1, 2, "dry"),       # lab, guest, focus
])
def test_a_mode_tone_comes_before_the_setting(ctx, mode_tone, setting, expected):
    ctx._mode["banter"] = mode_tone
    ctx._settings["banter_level"] = setting
    assert _tone(ctx) == expected


def test_plain_reaches_the_main_prompt(ctx, fake_hass):
    ctx._settings["banter_level"] = 0
    prompt = ctx._main_prompt("P", "H", "", "", "C", fake_hass)
    assert prompt.endswith("You are Nova." + ctx._PLAIN_REGISTER)


def test_a_broken_modes_module_falls_back_to_the_setting(ctx, load, monkeypatch):
    def boom():
        raise RuntimeError("modes unavailable")
    monkeypatch.setattr(load("modes"), "mode_banter_level", boom)
    ctx._settings["banter_level"] = 0
    assert _tone(ctx) == "plain"
