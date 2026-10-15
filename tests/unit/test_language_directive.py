"""The language block in the agent prompt follows Home Assistant's language.

Any language other than English gets a "## Language" block naming it; an
English install, or one with no language set, gets nothing at all.
"""
from types import SimpleNamespace

import pytest


@pytest.fixture
def directive(load):
    return load("agent")._language_directive


def _hass(code):
    return SimpleNamespace(config=SimpleNamespace(language=code))


@pytest.mark.parametrize("code, name", [("fr", "French"), ("de-DE", "German")])
def test_a_known_language_is_named(directive, code, name):
    block = directive(_hass(code))
    assert "## Language" in block and name in block


def test_a_code_with_no_name_is_used_as_it_is(directive):
    block = directive(_hass("xx"))
    assert "## Language" in block and "xx" in block


@pytest.mark.parametrize("hass", [
    _hass("en"), _hass("en-GB"), _hass(None), SimpleNamespace(),
])
def test_english_or_unset_adds_nothing(directive, hass):
    assert directive(hass) == ""
