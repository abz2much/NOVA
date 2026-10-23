"""Renaming a camera inside Nova only (v6.48.0).

display_name decides what a camera is called (Nova's own name, then the Home
Assistant friendly name, then the entity id) and merge_camera_name builds the
saved map, where a blank name means "go back to the Home Assistant name".
The panel and ws_rename_camera both rely on these two.
"""
import sys
import types

import pytest

EID = "camera.x"
HA = "HA Name"


@pytest.fixture
def cam(load, monkeypatch):
    # aiohttp is imported at the top of camera.py and may be missing here
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    return load("camera")


# ── display_name ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("friendly, names, shown", [
    (HA, {EID: "Bedroom 2"}, "Bedroom 2"),       # Nova's name wins
    (HA, {EID: "  Porch  "}, "Porch"),           # and is trimmed
    (HA, {EID: "   "}, HA),                       # blank falls back
    (HA, {}, HA),
    (HA, None, HA),
    (HA, {EID: 42}, HA),                          # non-text entry ignored
    (HA, {EID: None}, HA),
    (HA, {"camera.other": "Garage"}, HA),         # another camera's entry
    (None, {}, EID),                              # last resort: the id
    ("", {}, EID),
    (None, {EID: "Shed"}, "Shed"),
])
def test_display_name_table(cam, friendly, names, shown):
    assert cam.display_name(EID, friendly, names) == shown


# ── merge_camera_name ───────────────────────────────────────────────────────

def test_new_name_is_added_trimmed(cam):
    assert cam.merge_camera_name({}, EID, "  Front Porch ") == {EID: "Front Porch"}


def test_starting_from_no_map(cam):
    assert cam.merge_camera_name(None, EID, "Hall") == {EID: "Hall"}
    assert cam.merge_camera_name(None, EID, None) == {}


@pytest.mark.parametrize("blank", ["", "   ", None, 7])
def test_blank_name_reverts_only_that_camera(cam, blank):
    before = {EID: "Front Porch", "camera.y": "Garage"}
    assert cam.merge_camera_name(before, EID, blank) == {"camera.y": "Garage"}
    assert before == {EID: "Front Porch", "camera.y": "Garage"}


def test_rename_replaces_and_leaves_the_caller_map_alone(cam):
    before = {EID: "Old"}
    after = cam.merge_camera_name(before, EID, "New")
    assert after == {EID: "New"}
    assert before == {EID: "Old"}
    assert after is not before


def test_reverting_a_camera_with_no_entry_changes_nothing(cam):
    assert cam.merge_camera_name({"camera.y": "G"}, EID, "") == {"camera.y": "G"}


def test_merged_map_reads_back_through_display_name(cam):
    names = cam.merge_camera_name({}, EID, " Workshop ")
    assert cam.display_name(EID, HA, names) == "Workshop"
    names = cam.merge_camera_name(names, EID, "")
    assert cam.display_name(EID, HA, names) == HA
