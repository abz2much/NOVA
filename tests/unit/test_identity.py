"""identity.resolve and identity.quick_identify: a best guess at who is there.

Each signal adds a weighted vote per person: presence (a sole occupant, or a
small prior for everyone at home), recent faces, faces seen in the event's
own room, device trackers placing someone in that room, and the optional
voice sensor. An unsure answer comes back as "unknown", and identity only
ever reads state: it never acts on a device, so on its own it can never
unlock or disarm anything.

test_identity_face_scale.py imports cfg, identity, sigs, _face and
_stub_room from this file, so those keep their names and signatures.
"""
from __future__ import annotations

import pathlib
import sys
import types

import pytest

ALEX, JORDAN, RILEY = "Alex", "Jordan", "Riley"


# ── fixtures and helpers (some shared with test_identity_face_scale.py) ─────

@pytest.fixture
def identity(load):
    return load("identity")


@pytest.fixture
def cfg(load, monkeypatch):
    """The settings identity reads, as a dict; anything left out takes its
    default."""
    settings: dict = {}
    monkeypatch.setattr(load("nova_config"), "get",
                        lambda key, default=None: settings.get(key, default))
    return settings


@pytest.fixture
def sigs(load, monkeypatch):
    """What presence and recognition report.

    ``home`` is the list of people at home, ``seen`` maps a camera to the
    name it last recognised, and ``last`` maps a camera to its sighting
    record (name, confidence as a percent, age in seconds)."""
    world = {"home": [], "seen": {}, "last": {}}
    presence, recognition = load("presence"), load("recognition")

    def summary(hass):
        return {"people": [{"name": who, "state": "home"} for who in world["home"]]}

    monkeypatch.setattr(presence, "get_presence_summary", summary)
    monkeypatch.setattr(recognition, "who_is_where", lambda hass: dict(world["seen"]))
    monkeypatch.setattr(recognition, "last_seen_at",
                        lambda hass, camera: world["last"].get(camera))
    return world


def _face(state, camera, name, confidence=90.0, age_seconds=5):
    """Record a face on ``camera``. ``confidence`` is a percent, 0 to 100,
    which is the scale recognition.py stores (not 0 to 1)."""
    state["seen"][camera] = name
    state["last"][camera] = dict(name=name, confidence=confidence, age_seconds=age_seconds)


class _RoomCameras:
    """Stand-in recognition module for the room tests. A camera with no
    sighting record of its own reports 90% seen ten seconds ago."""

    FALLBACK = {"confidence": 90.0, "age_seconds": 10.0}

    def __init__(self, seen, meta):
        self.seen, self.meta = seen, dict(meta or {})

    def who_is_where(self, hass):
        return self.seen

    def last_seen_at(self, hass, camera):
        return self.meta.get(camera, self.FALLBACK)


def _stub_room(identity, monkeypatch, seen, cam_areas, meta=None):
    """Swap in recognition (``seen``: camera -> name, ``meta``: camera ->
    sighting) and an area lookup (``cam_areas``: entity -> area id)."""
    cams = _RoomCameras(seen, meta)
    monkeypatch.setitem(sys.modules, "jc.recognition", types.SimpleNamespace(
        who_is_where=cams.who_is_where, last_seen_at=cams.last_seen_at))
    monkeypatch.setitem(sys.modules, "jc.audio_routing", types.SimpleNamespace(
        entity_area=lambda hass, entity_id: cam_areas.get(entity_id)))


@pytest.fixture
def voice_sensor(identity):
    """Install a voice provider for one test; it is always removed after,
    even when the test fails part way."""
    asked = []

    def install(scores):
        def provider(hass, device_id):
            asked.append(device_id)
            if isinstance(scores, Exception):
                raise scores
            return scores
        identity.register_voice_provider(provider)
        return asked

    identity.register_voice_provider(None)
    yield install
    identity.register_voice_provider(None)


def _answer(identity, person, confidence, method, candidates):
    return identity.Identification(person, confidence, method, candidates)


# ── presence on its own ─────────────────────────────────────────────────────

def test_one_person_home_is_named_at_the_sole_occupant_weight(identity, cfg, sigs, fake_hass):
    sigs["home"] = [ALEX]
    got = identity.resolve(fake_hass)
    assert got.known
    assert got == _answer(identity, ALEX, 0.6, "sole_occupant", {ALEX: 0.6})


def test_an_empty_house_with_no_sightings_has_no_signal(identity, cfg, sigs, fake_hass):
    got = identity.resolve(fake_hass)
    assert got == _answer(identity, identity.UNKNOWN, 0.0, "no_signal", {})
    assert not got.known


def test_two_people_home_and_nothing_else_is_a_tie(identity, cfg, sigs, fake_hass):
    # Equal small priors: the margin is zero, so the confidence halves.
    sigs["home"] = [ALEX, JORDAN]
    got = identity.resolve(fake_hass)
    assert got.person == identity.UNKNOWN
    assert got.method == "low_confidence"
    assert got.confidence == pytest.approx(0.075)
    assert got.candidates == {ALEX: 0.15, JORDAN: 0.15}


# ── faces ───────────────────────────────────────────────────────────────────

def test_a_fresh_face_settles_who_it_is_when_two_are_home(identity, cfg, sigs, fake_hass):
    sigs["home"] = [ALEX, JORDAN]
    _face(sigs, "camera.office", JORDAN, confidence=95.0, age_seconds=3)
    got = identity.resolve(fake_hass)
    assert got.known and got.person == JORDAN
    # 0.15 prior + 0.8 * 0.95 * (1 - 3/300) for Jordan against 0.15 for Alex.
    assert got.method == "face+home_prior"
    assert got.confidence == pytest.approx(0.827)
    assert got.candidates == {JORDAN: pytest.approx(0.902), ALEX: 0.15}


@pytest.mark.parametrize("age", [300, 10_000], ids=["at_the_window_edge", "long_gone"])
def test_a_face_at_or_past_the_recency_window_adds_nothing(identity, cfg, sigs, fake_hass, age):
    sigs["home"] = [ALEX, JORDAN]
    _face(sigs, "camera.office", JORDAN, confidence=95.0, age_seconds=age)
    got = identity.resolve(fake_hass)
    assert got.person == identity.UNKNOWN and got.method == "low_confidence"
    assert got.candidates == {ALEX: 0.15, JORDAN: 0.15}   # no face vote at all


def test_presence_and_a_face_that_agree_reach_full_confidence(identity, cfg, sigs, fake_hass):
    sigs["home"] = [ALEX]
    _face(sigs, "camera.kitchen", ALEX, confidence=95.0, age_seconds=2)
    got = identity.resolve(fake_hass)
    assert got.person == ALEX
    assert got.confidence > 0.8
    assert got.confidence == 1.0          # capped at 1, and nobody else voted
    assert got.method == "face+sole_occupant"


# ── the optional voice sensor ───────────────────────────────────────────────

def test_the_voice_sensor_is_not_asked_while_its_setting_is_off(identity, cfg, sigs,
                                                               voice_sensor, fake_hass):
    cfg["identity_voice_fingerprint"] = False
    asked = voice_sensor({JORDAN: 1.0})
    sigs["home"] = [ALEX, JORDAN]
    got = identity.resolve(fake_hass, device_id="satellite.one")
    assert got.person == identity.UNKNOWN
    assert asked == []


def test_the_voice_sensor_decides_when_its_setting_is_on(identity, cfg, sigs,
                                                        voice_sensor, fake_hass):
    cfg["identity_voice_fingerprint"] = True
    asked = voice_sensor({JORDAN: 1.0})
    sigs["home"] = [ALEX, JORDAN]
    got = identity.resolve(fake_hass, device_id="satellite.one")
    assert got.person == JORDAN and "voice" in got.method
    assert got.method == "home_prior+voice"
    assert got.confidence == pytest.approx(0.929)
    assert asked == ["satellite.one"]    # the device is passed through


@pytest.mark.parametrize("reply", [None, {}, RuntimeError("sensor offline")],
                         ids=["none", "empty", "raises"])
def test_a_silent_or_broken_voice_sensor_falls_back_to_presence(identity, cfg, sigs,
                                                               voice_sensor, fake_hass, reply):
    cfg["identity_voice_fingerprint"] = True
    voice_sensor(reply)
    sigs["home"] = [ALEX]
    got = identity.resolve(fake_hass)
    assert got == _answer(identity, ALEX, 0.6, "sole_occupant", {ALEX: 0.6})


def test_voice_on_with_no_sensor_registered_is_harmless(identity, cfg, sigs, voice_sensor,
                                                       fake_hass):
    cfg["identity_voice_fingerprint"] = True
    assert identity.has_voice_provider() is False
    sigs["home"] = [ALEX]
    assert identity.resolve(fake_hass).person == ALEX


# ── the master switch and the confidence floor ─────────────────────────────

def test_identity_switched_off_names_nobody(identity, cfg, sigs, fake_hass):
    cfg["identity_enabled"] = False
    sigs["home"] = [ALEX]
    _face(sigs, "camera.kitchen", ALEX, confidence=99.0, age_seconds=0)
    got = identity.resolve(fake_hass)
    assert got == _answer(identity, identity.UNKNOWN, 0.0, "disabled", {})
    assert identity.quick_identify(fake_hass, area_id="kitchen").method == "disabled"


@pytest.mark.parametrize("floor, named", [(0.95, False), (0.6, True), (0.59, True)])
def test_the_confidence_floor_is_inclusive(identity, cfg, sigs, fake_hass, floor, named):
    # A sole occupant scores exactly 0.6, so a floor of 0.6 still names them.
    cfg["identity_min_confidence"] = floor
    sigs["home"] = [ALEX]
    got = identity.resolve(fake_hass)
    assert got.known is named
    assert got.confidence == 0.6 and got.candidates == {ALEX: 0.6}
    assert got.method == ("sole_occupant" if named else "low_confidence")


# ── subjects ────────────────────────────────────────────────────────────────

def test_the_knowledge_subject_is_the_normalised_name_or_primary(identity, cfg, sigs, fake_hass):
    sigs["home"] = ["Alex  Smith "]
    assert identity.subject_for(identity.resolve(fake_hass)) == "alex_smith"
    assert identity.subject_for(identity.Identification()) == "primary"
    assert identity.DEFAULT_PERSONAL_SUBJECT == "primary"


# ── a typed Chat turn names its logged in user and nobody else ─────────────

def test_a_logged_in_user_is_named_whatever_presence_and_faces_say(identity, cfg, sigs,
                                                                  fake_hass):
    fake_hass.states.set("person.jordan", "home", friendly_name=JORDAN, user_id="user-j")
    sigs["home"] = [ALEX]
    _face(sigs, "camera.kitchen", ALEX, confidence=99.0, age_seconds=0)
    got = identity.resolve(fake_hass, user_id="user-j")
    assert got == _answer(identity, JORDAN, 1.0, "user", {JORDAN: 1.0})


def test_an_unmatched_user_is_unknown_and_never_the_sole_occupant(identity, cfg, sigs,
                                                                 fake_hass):
    sigs["home"] = [ALEX]
    got = identity.resolve(fake_hass, user_id="user-nobody")
    assert got == _answer(identity, identity.UNKNOWN, 0.0, "user_unmatched", {})


def test_person_for_user_falls_back_to_a_title_cased_entity_name(identity, fake_hass):
    fake_hass.states.set("person.sam_lee", "not_home", user_id="user-s")
    assert identity.person_for_user(fake_hass, "user-s") == "Sam Lee"
    assert identity.person_for_user(fake_hass, None) is None
    assert identity.person_for_user(fake_hass, "") is None


# ── the room an event happened in ──────────────────────────────────────────
# Sole occupancy of the house is rare in a shared home; being the only one
# seen in a given room is common, which is what makes per-person routines
# possible.

def test_the_only_person_seen_in_the_room_gets_the_sole_room_weight(identity, monkeypatch):
    _stub_room(identity, monkeypatch, {"camera.study": RILEY}, {"camera.study": "study"})
    votes = identity._room_votes(None, "study", 0.0)
    assert set(votes) == {RILEY}
    assert votes[RILEY] >= identity._W_ROOM_SOLE * 0.25
    # 0.75 * 0.9 confidence * (1 - 10/300) freshness
    assert votes[RILEY] == pytest.approx(0.75 * 0.9 * (1 - 10 / 300))


def test_a_sighting_in_another_room_does_not_count(identity, monkeypatch):
    _stub_room(identity, monkeypatch, {"camera.kitchen": ALEX}, {"camera.kitchen": "kitchen"})
    assert identity._room_votes(None, "bedroom", 0.0) == {}


def test_an_old_room_sighting_is_ignored(identity, monkeypatch):
    stale = {"confidence": 90.0, "age_seconds": identity._ROOM_FRESH_SECS + 60}
    _stub_room(identity, monkeypatch, {"camera.kitchen": ALEX}, {"camera.kitchen": "kitchen"},
               meta={"camera.kitchen": stale})
    assert identity._room_votes(None, "kitchen", 0.0) == {}


def test_several_people_in_one_room_each_get_the_smaller_share(identity, monkeypatch):
    _stub_room(identity, monkeypatch,
               {"camera.a": ALEX, "camera.b": RILEY},
               {"camera.a": "lounge", "camera.b": "lounge"})
    votes = identity._room_votes(None, "lounge", 0.0)
    assert set(votes) == {ALEX, RILEY}
    assert max(votes.values()) < identity._W_ROOM_SOLE
    for weight in votes.values():
        assert weight == pytest.approx(0.30 * 0.9 * (1 - 10 / 300))


def test_an_unrecognised_face_in_the_room_is_not_a_person(identity, monkeypatch):
    _stub_room(identity, monkeypatch, {"camera.a": "unknown", "camera.b": RILEY},
               {"camera.a": "lounge", "camera.b": "lounge"})
    assert set(identity._room_votes(None, "lounge", 0.0)) == {RILEY}


@pytest.mark.parametrize("area", [None, ""])
def test_no_room_means_no_room_votes(identity, area):
    assert identity._room_votes(None, area, 0.0) == {}


# ── device trackers in the room ────────────────────────────────────────────

@pytest.fixture
def tracker_areas(monkeypatch):
    """Area lookup for tracker entities: entity id -> area id."""
    where: dict = {}
    monkeypatch.setitem(sys.modules, "jc.audio_routing", types.SimpleNamespace(
        entity_area=lambda hass, entity_id: where.get(entity_id)))
    return where


def test_a_tracker_naming_the_room_votes_for_its_person(identity, fake_hass, tracker_areas):
    fake_hass.states.set("device_tracker.phone_j", "Living Room", person="Jordan Lee")
    assert identity._proximity_votes(fake_hass, "living_room") == {"jordan_lee": 0.35}


def test_a_tracker_that_sits_in_the_room_votes_more_weakly(identity, fake_hass, tracker_areas):
    fake_hass.states.set("device_tracker.tag_a", "home", friendly_name=ALEX)
    tracker_areas["device_tracker.tag_a"] = "hall"
    votes = identity._proximity_votes(fake_hass, "hall")
    assert votes == {"alex": pytest.approx(0.35 * 0.6)}


def test_a_tracker_is_credited_to_the_person_who_owns_it(identity, fake_hass, tracker_areas):
    fake_hass.states.set("person.riley", "home", friendly_name=RILEY,
                         device_trackers=["device_tracker.watch_r"])
    fake_hass.states.set("device_tracker.watch_r", "hall")
    assert identity._proximity_votes(fake_hass, "hall") == {"riley": 0.35}


def test_trackers_elsewhere_or_without_an_owner_add_nothing(identity, fake_hass, tracker_areas):
    fake_hass.states.set("device_tracker.phone_j", "kitchen", person=JORDAN)
    fake_hass.states.set("device_tracker.orphan", "hall")       # no owner, no name
    assert identity._proximity_votes(fake_hass, "hall") == {}
    assert identity._proximity_votes(fake_hass, None) == {}


# ── quick_identify, the per state change guess ─────────────────────────────

def test_quick_identify_names_a_sole_occupant_with_their_weight(identity, monkeypatch):
    monkeypatch.setattr(identity, "_home_people", lambda hass: [ALEX])
    got = identity.quick_identify(None)
    assert got.person == ALEX and got.confidence > 0 and ALEX in got.candidates
    assert got == _answer(identity, ALEX, 0.6, "sole_occupant", {ALEX: 0.6})


def test_quick_identify_with_several_home_keeps_them_as_candidates(identity, monkeypatch):
    monkeypatch.setattr(identity, "_home_people", lambda hass: [ALEX, RILEY])
    got = identity.quick_identify(None)
    assert got.person == identity.UNKNOWN
    assert set(got.candidates) == {ALEX, RILEY}
    assert got == _answer(identity, identity.UNKNOWN, 0.0, "ambiguous_home",
                          {ALEX: 0.15, RILEY: 0.15})


def test_quick_identify_with_nobody_home_has_no_signal(identity, monkeypatch):
    monkeypatch.setattr(identity, "_home_people", lambda hass: [])
    assert identity.quick_identify(None) == _answer(identity, identity.UNKNOWN, 0.0,
                                                    "no_signal", {})


def test_quick_identify_never_raises(identity, monkeypatch):
    def broken(hass):
        raise RuntimeError("presence unavailable")
    monkeypatch.setattr(identity, "_home_people", broken)
    assert identity.quick_identify(None).method == "no_signal"


# ── identity reads state; it never acts ────────────────────────────────────

def test_resolving_with_every_signal_present_sends_no_service_call(identity, cfg, sigs,
                                                                  voice_sensor, fake_hass,
                                                                  tracker_areas):
    cfg["identity_voice_fingerprint"] = True
    voice_sensor({ALEX: 1.0})
    sigs["home"] = [ALEX]
    _face(sigs, "camera.front", ALEX, confidence=99.0, age_seconds=0)
    fake_hass.states.set("device_tracker.phone_a", "hall", person=ALEX)
    assert identity.resolve(fake_hass, area_id="hall").person == ALEX
    assert identity.quick_identify(fake_hass, area_id="hall").person == ALEX
    assert fake_hass.service_calls == []


def test_the_identity_module_has_no_way_to_call_a_service_or_set_state(identity):
    text = pathlib.Path(identity.__file__).read_text(encoding="utf-8")
    for forbidden in ("async_call", "services.call", "states.async_set", "states.set("):
        assert forbidden not in text, forbidden
