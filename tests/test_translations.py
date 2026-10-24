"""Shape and content of Nova's translation files (v7.36.0).

Two families are checked:

* translations/*.json hold the config flow, options, Repairs notices and
  so on. Every language must have exactly the keys en.json has, so Home
  Assistant never shows a raw key. Service and entity text is English only
  for now and is left out of the comparison; Home Assistant falls back to
  en.json there.
* frontend/i18n/*.json map the panel's English strings to a translation.

Paths are built from this file's location, so the checks behave the same
whichever directory pytest is started from.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

COMPONENT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "nova"
FLOW_DIR = COMPONENT / "translations"
PANEL_DIR = COMPONENT / "frontend" / "i18n"
ENGLISH_ONLY_SECTIONS = ("services", "entity")
PLACEHOLDER = re.compile(r"\{(\w+)\}")

FLOW_FILES = sorted(FLOW_DIR.glob("*.json"))
PANEL_FILES = sorted(PANEL_DIR.glob("*.json"))


def _read(path: pathlib.Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _without_english_only(data: dict) -> dict:
    return {k: v for k, v in data.items() if k not in ENGLISH_ONLY_SECTIONS}


def _leaves(node, trail=()):
    """(path, value) for every non-dict value, depth first."""
    if isinstance(node, dict):
        for key, child in node.items():
            yield from _leaves(child, trail + (key,))
    else:
        yield trail, node


def _skeleton(node):
    """The nested key structure with every leaf replaced by None."""
    if isinstance(node, dict):
        return {key: _skeleton(child) for key, child in node.items()}
    return None


def test_enough_files_are_present():
    assert len(FLOW_FILES) >= 7, "expected en + at least 6 translations"
    assert len(PANEL_FILES) >= 12, "expected the seeded panel language files"
    assert (FLOW_DIR / "en.json").exists()


# ── config flow translations ─────────────────────────────────────────────

def test_en_json_has_the_service_section_the_others_may_skip():
    # The comparison below drops "services"; make sure it is really there in
    # English, so dropping it is not hiding a missing section.
    assert "services" in _read(FLOW_DIR / "en.json")


@pytest.mark.parametrize("path", FLOW_FILES, ids=lambda p: p.name)
def test_flow_file_mirrors_en_json(path):
    english = _skeleton(_without_english_only(_read(FLOW_DIR / "en.json")))
    assert _skeleton(_without_english_only(_read(path))) == english, (
        f"{path.name} structure differs from en.json")


@pytest.mark.parametrize("path", FLOW_FILES, ids=lambda p: p.name)
def test_flow_file_has_no_blank_strings(path):
    blank = [trail for trail, value in _leaves(_read(path))
             if isinstance(value, str) and not value.strip()]
    assert blank == [], f"empty string in {path.name}: {blank}"


@pytest.mark.parametrize("path", FLOW_FILES, ids=lambda p: p.name)
def test_flow_file_keeps_the_english_placeholders(path):
    # Home Assistant fills {detail}, {step} and the like by name; a
    # translated or dropped placeholder shows up raw in the UI.
    english = dict(_leaves(_without_english_only(_read(FLOW_DIR / "en.json"))))
    changed = []
    for trail, value in _leaves(_without_english_only(_read(path))):
        source = english.get(trail)
        if isinstance(value, str) and isinstance(source, str):
            if sorted(PLACEHOLDER.findall(value)) != sorted(PLACEHOLDER.findall(source)):
                changed.append("/".join(trail))
    assert changed == [], f"{path.name}: {changed}"


# ── panel translations ───────────────────────────────────────────────────

@pytest.mark.parametrize("path", PANEL_FILES, ids=lambda p: p.name)
def test_panel_file_is_a_non_empty_map_of_strings(path):
    data = _read(path)
    assert isinstance(data, dict) and data, f"{path.name} empty or not an object"
    for english, translated in data.items():
        assert isinstance(translated, str) and translated.strip(), (
            f"empty value for {english!r} in {path.name}")
