"""Settings the panel saves have to be accepted, and shown again, by the backend.

nova/update_config refuses any key outside PANEL_WRITABLE_KEYS with
`invalid_key`, which the user sees as a failed save (the Residence tab's HOME
STYLE control once did exactly that). A key that saves but is missing from
get_panel_data's `config` block has the opposite problem: the control snaps
back to its default on the next render.

Neither side is imported: websocket.py pulls in Home Assistant at module
level and the panel is JavaScript, so both are read as source.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from ws_sources import panel_config_keys, panel_writable_keys, ws_function, ws_text

PANEL_SOURCE = (pathlib.Path(__file__).resolve().parents[2]
                / "custom_components" / "nova" / "frontend" / "nova-panel.js")

# How the panel names a setting it is about to persist.
_SAVE_PATTERNS = (
    re.compile(r"_saveConfig\(\s*['\"]([a-z0-9_]+)['\"]"),   # direct calls
    re.compile(r"data-cfg-key=['\"]([a-z0-9_]+)['\"]"),     # generic handlers
)
# How the panel reads a saved value back when it renders a control.
_READ_BACK = re.compile(r"d\.config\?\.([a-z0-9_]+)")


def _panel_js() -> str:
    return PANEL_SOURCE.read_text(encoding="utf-8")


def keys_the_panel_saves() -> set[str]:
    js = _panel_js()
    return {key for pattern in _SAVE_PATTERNS for key in pattern.findall(js)}


def keys_the_panel_reads_back() -> set[str]:
    return set(_READ_BACK.findall(_panel_js()))


# Groups of keys with the sides each must (or must not) appear on.
# "writable": in PANEL_WRITABLE_KEYS. "surfaced": in get_panel_data's config.
EXPECTED = [
    pytest.param(("home_stories", "has_basement", "garage_bays", "garage_mode",
                  "exit_doors", "basement_mode", "door_mapping"),
                 {"writable"}, id="home-layout-card-8.30"),
    pytest.param(("garage_mode", "has_garage", "exit_doors", "exit_door_status",
                  "exit_door_candidates", "basement_mode", "home_features"),
                 {"surfaced"}, id="garage-and-exit-doors-8.26"),
    pytest.param(("banter_level", "search_backend", "searxng_url",
                  "calendar_tight_gap_min", "recognition_source"),
                 {"surfaced"}, id="character-and-research-read-back"),
    pytest.param(("security_alarm_entity", "lockdown_auto_on_arm",
                  "intrusion_requires_confinement", "lockdown_exempt_locks"),
                 {"writable", "surfaced"}, id="security-alarm"),
    pytest.param(("notify_services",), {"writable"}, id="several-notify-targets"),
    pytest.param(("onboarding_dismissed",), {"writable"}, id="onboarding-dismiss"),
]

# The removed 3D house (8.30.0) used these. Values already in config.json stay
# there and are ignored, but the backend must neither accept nor report them.
RETIRED_3D_HOUSE = ("residence_style", "floor_plan_sqft", "dormers_front",
                    "dormers_rear", "chimney_side", "home_bedrooms", "home_bathrooms")


@pytest.mark.parametrize("keys, sides", EXPECTED)
def test_known_keys_sit_where_the_panel_needs_them(keys, sides):
    on_side = {"writable": panel_writable_keys(), "surfaced": panel_config_keys()}
    for side in sides:
        absent = [k for k in keys if k not in on_side[side]]
        assert absent == [], f"not {side}: {absent}"


@pytest.mark.parametrize("key", RETIRED_3D_HOUSE)
def test_3d_house_keys_are_gone_from_both_sides(key):
    assert key not in panel_writable_keys()
    assert key not in panel_config_keys()


def test_the_allowlist_and_the_panel_extraction_are_both_populated():
    assert len(panel_writable_keys()) > 0
    assert keys_the_panel_saves(), "found no keys the panel saves; has the JS changed shape?"


def test_whatever_the_panel_saves_the_backend_accepts():
    rejected = sorted(keys_the_panel_saves() - panel_writable_keys())
    assert rejected == [], (
        f"nova/update_config would answer invalid_key for: {rejected}")


def test_saved_and_read_back_keys_come_back_in_panel_data():
    # Covers the whole class, not just the keys named in EXPECTED: any field
    # that is both saved and read back must be returned, or it resets.
    round_trip = keys_the_panel_saves() & keys_the_panel_reads_back()
    lost = sorted(round_trip - panel_config_keys())
    assert lost == [], f"saved and read back but never returned, so they reset: {lost}"


# ── first-run onboarding card (v6.70.0) ──────────────────────────────────

def test_panel_data_carries_the_onboarding_block():
    source = ws_text()
    assert '"onboarding": _get_onboarding_state(' in source
    assert "def _get_onboarding_state(" in source


def _onboarding_step_ids() -> set[str]:
    """The `"id"` value of every step dict built inside _get_onboarding_state."""
    ids = set()
    for node in ast.walk(ws_function("_get_onboarding_state")):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "id"
                        and isinstance(v, ast.Constant)):
                    ids.add(v.value)
    return ids


@pytest.mark.parametrize("step", ["notify", "cameras", "voice", "banter"])
def test_onboarding_checklist_names_the_key_setup_steps(step):
    assert f'"{step}"' in ws_text()
    # Stronger: the id belongs to a step of the checklist itself, not just
    # any string that happens to match somewhere in the websocket layer.
    assert step in _onboarding_step_ids()
