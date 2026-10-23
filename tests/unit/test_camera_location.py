"""Pinning a camera indoors or outdoors from the panel (v6.49.0).

Two pure helpers in outdoor.py do the list work: set_entity_location builds
the new (indoor, outdoor) pin lists and location_mode reads a camera's pin
back. The end-to-end cases then save those lists under the same option keys
the WebSocket command writes and check that is_outdoor obeys them ahead of
its own name and area guesses.
"""
import pytest

CAM = "camera.den"


@pytest.fixture
def od(load):
    return load("outdoor")


@pytest.fixture
def pins(load, monkeypatch):
    """Returns save(indoor, outdoor): stores the two lists where is_outdoor
    reads them, as the panel's save would."""
    saved = {}
    monkeypatch.setattr(load("nova_config"), "get",
                        lambda key, default=None: saved.get(key, default))

    def save(indoor, outdoor):
        saved.update(indoor_entities=list(indoor), outdoor_entities=list(outdoor))
    return save


# ── set_entity_location ─────────────────────────────────────────────────────

@pytest.mark.parametrize("start_in, start_out, mode, want_in, want_out", [
    # moving between the lists keeps every other entry where it was
    ([CAM], ["binary_sensor.gate"], "outdoor", [], ["binary_sensor.gate", CAM]),
    ([], [CAM], "indoor", [CAM], []),
    # "auto" just drops the pin
    ([CAM], [], "auto", [], []),
    ([], [CAM], "auto", [], []),
    # glob entries are never touched by a single-camera pin
    (["camera.guest*"], ["camera.yard*"], "outdoor",
     ["camera.guest*"], ["camera.yard*", CAM]),
    # None lists are treated as empty
    (None, None, "indoor", [CAM], []),
    # an unknown mode behaves like auto
    ([CAM], [], "sideways", [], []),
])
def test_pin_table(od, start_in, start_out, mode, want_in, want_out):
    assert od.set_entity_location(start_in, start_out, CAM, mode) == (want_in, want_out)


def test_indoor_then_auto_round_trip(od):
    lists = od.set_entity_location([], [], "camera.backyard", "indoor")
    assert lists == (["camera.backyard"], [])
    assert od.set_entity_location(*lists, "camera.backyard", "auto") == ([], [])


def test_pinning_twice_gives_the_same_lists(od):
    once = od.set_entity_location([], [], "camera.x", "outdoor")
    assert od.set_entity_location(once[0], once[1], "camera.x", "outdoor") == once
    assert once == ([], ["camera.x"])


def test_pin_is_stored_lower_case_and_replaces_any_casing(od):
    # an old entry spelt with capitals is still recognised and replaced
    assert od.set_entity_location(["Camera.Den"], [], " Camera.DEN ", "outdoor") == (
        [], [CAM])


def test_inputs_are_left_unchanged(od):
    indoor, outdoor = [CAM], ["camera.yard"]
    od.set_entity_location(indoor, outdoor, CAM, "outdoor")
    assert indoor == [CAM] and outdoor == ["camera.yard"]


# ── location_mode ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("entity, indoor, outdoor, want", [
    (CAM, [CAM], [], "indoor"),
    (CAM, [], [CAM], "outdoor"),
    (CAM, [], [], "auto"),
    (CAM, None, None, "auto"),
    # a glob classifies through is_outdoor but is not a pin on this camera
    ("camera.denright", ["camera.den*"], [], "auto"),
    # the comparison ignores case on both sides
    ("camera.Den", [CAM], [], "indoor"),
    (CAM, [], ["CAMERA.DEN"], "outdoor"),
    # if both lists name it, indoor is reported, matching is_outdoor's order
    (CAM, [CAM], [CAM], "indoor"),
])
def test_mode_table(od, entity, indoor, outdoor, want):
    assert od.location_mode(entity, indoor, outdoor) == want


# ── pins decide is_outdoor ahead of the guesses ─────────────────────────────

def test_indoor_pin_overrides_an_outdoor_sounding_name(od, pins, fake_hass):
    # "backyard" in the id would make this outdoor; the ✎ overlay's indoor
    # pin is checked first and must win
    eid = "camera.backyard_playroom"
    assert od.is_outdoor(fake_hass, eid) is True        # before the pin
    pins(*od.set_entity_location([], [], eid, "indoor"))
    assert od.is_outdoor(fake_hass, eid) is False


def test_outdoor_pin_makes_a_plain_name_outdoor(od, pins, fake_hass):
    eid = "camera.hallway_east"
    pins([], [])
    assert od.is_outdoor(fake_hass, eid) is False       # no outdoor signal
    pins(*od.set_entity_location([], [], eid, "outdoor"))
    assert od.is_outdoor(fake_hass, eid) is True


def test_unpinning_hands_back_to_the_name_keyword(od, pins, fake_hass):
    eid = "camera.driveway_cam"
    lists = od.set_entity_location([], [], eid, "indoor")
    pins(*lists)
    assert od.is_outdoor(fake_hass, eid) is False
    pins(*od.set_entity_location(*lists, eid, "auto"))
    assert od.is_outdoor(fake_hass, eid) is True        # "driveway" again


def test_pin_saved_from_mixed_case_id_still_matches(od, pins, fake_hass):
    pins(*od.set_entity_location([], [], "Camera.Hallway_East", "outdoor"))
    assert od.is_outdoor(fake_hass, "camera.hallway_east") is True
