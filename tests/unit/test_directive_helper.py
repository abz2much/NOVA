"""The system prompt every LLM call starts from: the prime directive first,
then the persona, then the task's own instructions, with the honorific filled
in. conftest replaces jc.directive_helper with a stub for the other tests, so
the real file is loaded here under its own name.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

SOURCE = (Path(__file__).resolve().parents[2] / "custom_components" / "nova"
          / "directive_helper.py")


@pytest.fixture
def dh(load):
    const = load("const")
    spec = importlib.util.spec_from_file_location("jc.directive_helper_real", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.const = const
    return module


@pytest.fixture
def runtime(monkeypatch):
    """A nova_config whose runtime_get serves the given settings and records
    each lookup."""
    seen = []
    settings = {}

    def runtime_get(hass, entry, key, default=None):
        seen.append((hass, entry, key, default))
        return settings.get(key, default)

    stub = types.ModuleType("jc.nova_config")
    stub.runtime_get = runtime_get
    monkeypatch.setitem(sys.modules, "jc.nova_config", stub)
    return types.SimpleNamespace(seen=seen, settings=settings)


# ── resolve_directive ────────────────────────────────────────────────────────

def test_with_no_entry_the_default_directive_applies(dh):
    assert dh.resolve_directive(None) == dh.const.get_directive(
        dh.const.DEFAULT_DIRECTIVE_PRESET, "")
    assert dh.resolve_directive(None, object()) == dh.resolve_directive(None)


def test_the_preset_and_the_custom_text_are_read_from_the_live_settings(dh, runtime):
    hass, entry = object(), object()
    runtime.settings[dh.const.CONF_DIRECTIVE_PRESET] = "strict"
    runtime.settings[dh.const.CONF_DIRECTIVE] = "Always whisper."
    out = dh.resolve_directive(entry, hass)
    assert runtime.seen == [
        (hass, entry, dh.const.CONF_DIRECTIVE_PRESET, dh.const.DEFAULT_DIRECTIVE_PRESET),
        (hass, entry, dh.const.CONF_DIRECTIVE, ""),
    ]
    assert out == dh.const.get_directive("strict", "Always whisper.")


def test_an_unset_entry_gets_the_default_preset_and_no_custom_text(dh, runtime):
    entry = object()
    assert dh.resolve_directive(entry) == dh.const.get_directive(
        dh.const.DEFAULT_DIRECTIVE_PRESET, "")
    assert [call[0] for call in runtime.seen] == [None, None]


def test_the_custom_directive_wins_over_the_preset(dh, runtime):
    runtime.settings[dh.const.CONF_DIRECTIVE] = "PRIME DIRECTIVE: be brief."
    assert dh.resolve_directive(object(), object()) == "PRIME DIRECTIVE: be brief."


# ── get_entry ────────────────────────────────────────────────────────────────

class _Entries:
    def __init__(self, entries=None, boom=False):
        self.asked = []
        self._entries, self._boom = entries or [], boom

    def async_entries(self, domain):
        self.asked.append(domain)
        if self._boom:
            raise RuntimeError("not ready")
        return self._entries


def _hass(entries):
    return types.SimpleNamespace(config_entries=entries)


def test_the_first_nova_entry_is_the_one_used(dh):
    entries = _Entries(["first", "second"])
    assert dh.get_entry(_hass(entries)) == "first"
    assert entries.asked == [dh.const.DOMAIN]


def test_no_entry_gives_none(dh):
    assert dh.get_entry(_hass(_Entries([]))) is None


def test_a_lookup_failure_gives_none(dh):
    assert dh.get_entry(_hass(_Entries(boom=True))) is None
    assert dh.get_entry(object()) is None


# ── fill_honorific ───────────────────────────────────────────────────────────

def test_the_honorific_replaces_every_placeholder(dh):
    assert dh.fill_honorific("Yes, {honorific}. Right away, {honorific}.", "madam") == \
        "Yes, madam. Right away, madam."
    assert dh.fill_honorific("no placeholder", "madam") == "no placeholder"


def test_with_no_honorific_the_addressing_line_is_rewritten(dh):
    line = "Address your owner as {honorific} — naturally, never robotically"
    assert dh.fill_honorific(line, "") == (
        "Address the room in general — no honorific, since no one specific "
        "is home to address right now")


def test_with_no_honorific_trailing_placeholders_are_dropped_cleanly(dh):
    assert dh.fill_honorific("Right away, {honorific}.", "") == "Right away."
    assert dh.fill_honorific("Done, {honorific}! Yes, {honorific}.", "") == "Done! Yes."
    assert dh.fill_honorific("{honorific} is here", "") == " is here"


def test_the_persona_has_no_placeholder_left_either_way(dh):
    persona = dh.const.NOVA_PERSONA
    assert "{honorific}" in persona
    named = dh.fill_honorific(persona, "sir")
    assert "{honorific}" not in named
    assert "Address your owner as sir — naturally, never robotically" in named
    blank = dh.fill_honorific(persona, "")
    assert "{honorific}" not in blank
    assert "Address the room in general — no honorific, since no one specific " \
        "is home to address right now" in blank
    assert "Right away, ." not in blank and "Address your owner as  " not in blank


# ── build_system_prompt ──────────────────────────────────────────────────────

@pytest.fixture
def prompt_env(dh, monkeypatch):
    calls = []
    monkeypatch.setattr(dh, "get_entry", lambda hass: calls.append(("entry", hass)) or "E")
    monkeypatch.setattr(
        dh, "resolve_directive",
        lambda entry, hass=None: calls.append(("directive", entry, hass)) or "DIRECTIVE")
    monkeypatch.setattr(dh, "NOVA_PERSONA", "PERSONA for {honorific}", raising=True)
    return calls


def test_the_directive_comes_first_then_the_persona(dh, prompt_env):
    hass = object()
    assert dh.build_system_prompt(hass, "sir") == \
        "DIRECTIVE\n\n---\n\nPERSONA for sir"
    assert prompt_env == [("entry", hass), ("directive", "E", hass)]


def test_task_context_goes_last_with_the_edges_trimmed(dh, prompt_env):
    out = dh.build_system_prompt(object(), "sir", "  Describe the camera.\n ")
    assert out == "DIRECTIVE\n\n---\n\nPERSONA for sir\n\n---\n\nDescribe the camera."


def test_an_empty_task_context_adds_no_section(dh, prompt_env):
    assert dh.build_system_prompt(object(), "sir", "") == "DIRECTIVE\n\n---\n\nPERSONA for sir"


def test_a_blank_task_context_still_adds_an_empty_section(dh, prompt_env):
    assert dh.build_system_prompt(object(), "sir", "   ") == \
        "DIRECTIVE\n\n---\n\nPERSONA for sir\n\n---\n\n"


def test_a_placeholder_in_the_task_context_is_filled_too(dh, prompt_env):
    out = dh.build_system_prompt(object(), "madam", "Greet {honorific}.")
    assert out.endswith("\n\n---\n\nGreet madam.")


def test_no_honorific_leaves_no_blank_in_the_prompt(dh, prompt_env):
    out = dh.build_system_prompt(object(), "", "Say hello, {honorific}.")
    assert "{honorific}" not in out and out.endswith("Say hello.")


def test_the_real_prompt_starts_with_the_directive_and_holds_the_persona(dh, runtime):
    hass = types.SimpleNamespace(config_entries=_Entries([]))
    out = dh.build_system_prompt(hass, "sir", "Task text.")
    directive = dh.resolve_directive(None)
    assert out.startswith(directive + "\n\n---\n\n")
    assert dh.fill_honorific(dh.const.NOVA_PERSONA, "sir") in out
    assert out.endswith("\n\n---\n\nTask text.")
