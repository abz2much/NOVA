"""The face confidence scale in identity resolution (8.7.12).

recognition.py stores a confidence as a percent (0..100). identity.py's vote
weights are all 0..1 (face 0.8, voice 0.9, room 0.75, proximity 0.35). The face
and room votes multiplied the percent in as it was, so a fresh face was worth
about 79 and beat every other signal. The fix divides by 100 and clamps to
0..1, and keys face votes by normalize(name) so one person is one vote."""
from __future__ import annotations

import math
import sys
import types

import pytest

from test_identity import cfg, identity, sigs, _face, _stub_room  # noqa: F401


@pytest.fixture
def voice(identity, cfg):
    box = {"scores": {}}
    cfg["identity_voice_fingerprint"] = True
    identity.register_voice_provider(lambda h, d: dict(box["scores"]))
    yield box
    identity.register_voice_provider(None)


# ── the old behaviour, reproduced, to show it was wrong ─────────────────────

def _old_face_votes(identity, seen, last):
    """identity._face_votes as it was before 8.7.12: the confidence went in
    unscaled and the vote was keyed by the raw name."""
    votes = {}
    for cam, name in seen.items():
        rec = last.get(cam) or {}
        conf = float(rec.get("confidence", 0.7))
        recency = max(0.0, 1.0 - float(rec.get("age_seconds", 0.0)) / identity._FACE_RECENCY_WINDOW)
        if recency > 0 and name:
            votes[name] = max(votes.get(name, 0.0), identity._W_FACE * conf * recency)
    return votes


def test_the_old_formula_made_a_face_worth_about_79_and_it_always_won(identity):
    seen = {"camera.front": "Alice"}
    last = {"camera.front": {"confidence": 98.7, "age_seconds": 5.0}}   # what recognition.py stores
    old = _old_face_votes(identity, seen, last)
    assert old["Alice"] > 70                               # a "0..1" weight of about 78
    best_other = identity._W_VOICE * 1.0                   # the strongest signal there is
    assert old["Alice"] > 50 * best_other                  # a perfect voice match for Bea was never close


def test_the_old_formula_beat_a_perfect_voice_and_resolved_at_full_confidence(identity):
    old_votes = {"Alice": _old_face_votes(identity, {"c": "Alice"},
                                          {"c": {"confidence": 98.7, "age_seconds": 5.0}})["Alice"],
                 "Bea": identity._W_VOICE * 1.0}
    ranked = sorted(old_votes.items(), key=lambda kv: kv[1], reverse=True)
    assert ranked[0][0] == "Alice"
    top, second = ranked[0][1], ranked[1][1]
    assert min(1.0, top) * (0.5 + 0.5 * (top - second) / top) > 0.99


# ── the fix ─────────────────────────────────────────────────────────────────

def test_face_only_fresh_resolves_and_the_weight_is_at_most_0_8(identity, cfg, sigs, fake_hass):
    _face(sigs, "camera.front", "Alice", confidence=98.7, age_seconds=0)
    votes = identity._face_votes(fake_hass, 0.0)
    assert votes == {"alice": pytest.approx(0.8 * 0.987)}
    assert max(votes.values()) <= identity._W_FACE == 0.8
    ident = identity.resolve(fake_hass)
    assert ident.person == "Alice" and ident.known and ident.method == "face"
    assert ident.candidates["Alice"] <= 0.8
    assert ident.confidence == pytest.approx(0.8 * 0.987, abs=0.002)


def test_a_strong_face_with_no_voice_still_resolves(identity, cfg, sigs, voice, fake_hass):
    voice["scores"] = {}
    _face(sigs, "camera.front", "Alice", confidence=98.7, age_seconds=5)
    ident = identity.resolve(fake_hass)
    assert ident.person == "Alice" and ident.known and "voice" not in ident.method


def test_a_strong_voice_for_B_beats_a_fresh_face_for_A(identity, cfg, sigs, voice, fake_hass):
    voice["scores"] = {"Bea": 1.0}
    _face(sigs, "camera.front", "Alice", confidence=98.7, age_seconds=5)
    ident = identity.resolve(fake_hass)
    assert ident.person == "Bea" and ident.known and "voice" in ident.method
    assert ident.candidates["Bea"] > ident.candidates["Alice"]
    assert ident.candidates["Bea"] <= identity._W_VOICE
    assert ident.candidates["Alice"] <= identity._W_FACE


def test_a_voice_at_0_95_beats_a_fresh_face_but_only_just(identity, cfg, sigs, voice, fake_hass):
    """0.95 x 0.9 = 0.855 against 0.8 x 0.987 x recency = 0.776: the voice leads,
    but the margin is small, so the confidence is about 0.47, only just over the
    0.45 gate. Before the fix the face won this outright."""
    voice["scores"] = {"Bea": 0.95}
    _face(sigs, "camera.front", "Alice", confidence=98.7, age_seconds=5)
    ident = identity.resolve(fake_hass)
    assert ident.person == "Bea" and ident.known
    assert 0.45 <= ident.confidence < 0.5
    ranked = sorted(ident.candidates, key=ident.candidates.get, reverse=True)
    assert ranked == ["Bea", "Alice"]


def test_a_face_alone_can_no_longer_outvote_presence_and_voice_together(identity, cfg, sigs, voice, fake_hass):
    sigs["home"] = ["Bea"]
    voice["scores"] = {"Bea": 0.9}
    _face(sigs, "camera.front", "Alice", confidence=98.7, age_seconds=5)
    assert identity.resolve(fake_hass).person == "Bea"


def test_names_that_differ_only_in_case_or_spacing_are_one_vote(identity, cfg, sigs, fake_hass):
    _face(sigs, "camera.front", "Sam Smith", confidence=90.0, age_seconds=10)
    _face(sigs, "camera.garden", "sam  SMITH", confidence=80.0, age_seconds=10)
    votes = identity._face_votes(fake_hass, 0.0)
    assert list(votes) == ["sam_smith"]
    assert votes["sam_smith"] == pytest.approx(0.8 * 0.9 * (1 - 10 / 300))      # the stronger sighting
    assert len(identity.resolve(fake_hass).candidates) == 1


def test_presence_face_and_voice_for_one_person_merge_whatever_the_casing(identity, cfg, sigs, voice, fake_hass):
    sigs["home"] = ["Sam Smith", "Other One"]
    voice["scores"] = {"SAM SMITH": 0.9}
    _face(sigs, "camera.front", "sam smith", confidence=90.0, age_seconds=10)
    ident = identity.resolve(fake_hass)
    assert ident.person == "Sam Smith"              # the presence spelling is what is shown
    assert set(ident.candidates) == {"Sam Smith", "Other One"}
    assert "face" in ident.method and "voice" in ident.method


def test_a_single_signal_still_reads_as_it_did(identity, cfg, sigs, fake_hass):
    sigs["home"] = ["Username"]
    assert identity.resolve(fake_hass).person == "Username"            # presence: as written
    sigs["home"] = []
    _face(sigs, "camera.front", "Username Two", confidence=90.0, age_seconds=5)
    assert identity.resolve(fake_hass).person == "Username Two"        # face: as the backend wrote it


# ── bad confidence values never raise ───────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (98.7, 0.987), (100, 1.0), (60, 0.6), (0, 0.0), ("98.7", 0.987), (" 50 ", 0.5),
    (-5, 0.0), (-0.0001, 0.0), (250, 1.0), (101, 1.0), (1e30, 1.0),
    (None, 0.7), ("high", 0.7), ("", 0.7), (True, 0.7), (False, 0.7),
    (float("nan"), 0.7), (float("inf"), 0.7), (float("-inf"), 0.7),
    ([], 0.7), ({}, 0.7), (object(), 0.7),
])
def test_unit_confidence_never_raises_and_clamps(identity, raw, expected):
    assert identity._unit_confidence(raw) == pytest.approx(expected)
    assert 0.0 <= identity._unit_confidence(raw) <= 1.0


def test_odd_values_in_the_cache_do_not_drop_the_other_faces(identity, cfg, sigs, fake_hass):
    _face(sigs, "camera.a", "Alice", confidence=None, age_seconds=5)
    _face(sigs, "camera.b", "Bea", confidence="oops", age_seconds=5)
    _face(sigs, "camera.c", "Cara", confidence=-20, age_seconds=5)
    _face(sigs, "camera.d", "Dana", confidence=900, age_seconds=5)
    sigs["last"]["camera.e"] = {"name": "Eve", "confidence": 90.0, "age_seconds": "x"}   # a bad age
    sigs["seen"]["camera.e"] = "Eve"
    sigs["seen"]["camera.f"] = "Fay"
    sigs["last"]["camera.f"] = None        # no record: the existing default confidence, as before
    votes = identity._face_votes(fake_hass, 0.0)
    assert votes["alice"] == pytest.approx(0.8 * 0.7 * (1 - 5 / 300))      # the existing default
    assert votes["bea"] == pytest.approx(0.8 * 0.7 * (1 - 5 / 300))
    assert votes["cara"] == 0.0
    assert votes["dana"] == pytest.approx(0.8 * 1.0 * (1 - 5 / 300))
    assert "eve" not in votes                        # only the entry with the bad age is skipped
    assert votes["fay"] == pytest.approx(0.8 * 0.7)
    identity.resolve(fake_hass)                                                        # no raise


def test_a_broken_recognition_module_gives_no_votes_and_does_not_raise(identity, cfg, sigs, fake_hass, monkeypatch):
    rec = sys.modules["jc.recognition"]
    monkeypatch.setattr(rec, "who_is_where", lambda h: 1 / 0)
    assert identity._face_votes(fake_hass, 0.0) == {}


# ── the same fix for the room vote ──────────────────────────────────────────

def test_room_votes_use_the_same_scale(identity, monkeypatch):
    _stub_room(identity, monkeypatch, {"camera.k": "Alice"}, {"camera.k": "kitchen"},
               meta={"camera.k": {"confidence": 98.7, "age_seconds": 0.0}})
    votes = identity._room_votes(None, "kitchen", 0.0)
    assert votes == {"Alice": pytest.approx(identity._W_ROOM_SOLE * 0.987)}
    assert votes["Alice"] <= identity._W_ROOM_SOLE


def test_room_votes_count_one_person_once_whatever_the_casing(identity, monkeypatch):
    _stub_room(identity, monkeypatch, {"camera.k": "Sam Smith", "camera.l": "sam smith"},
               {"camera.k": "kitchen", "camera.l": "kitchen"},
               meta={"camera.k": {"confidence": 90.0, "age_seconds": 0.0},
                     "camera.l": {"confidence": 95.0, "age_seconds": 0.0}})
    votes = identity._room_votes(None, "kitchen", 0.0)
    assert len(votes) == 1
    assert list(votes.values())[0] == pytest.approx(identity._W_ROOM_SOLE * 0.95)   # sole person, not "several"


def test_room_votes_odd_confidence_does_not_raise(identity, monkeypatch):
    for bad in (None, "x", -3, 400, float("nan")):
        _stub_room(identity, monkeypatch, {"camera.k": "Alice"}, {"camera.k": "kitchen"},
                   meta={"camera.k": {"confidence": bad, "age_seconds": 0.0}})
        votes = identity._room_votes(None, "kitchen", 0.0)
        assert 0.0 <= votes["Alice"] <= identity._W_ROOM_SOLE


# ── what callers now see: a face is trusted for about two minutes, not five ──

def test_a_face_on_its_own_stays_known_for_about_two_minutes(identity, cfg, sigs, fake_hass):
    """With the default identity_min_confidence of 0.45, a 95% face resolves on
    its own while 0.8 * 0.95 * (1 - age/300) is at least 0.45, so up to about 122
    seconds. Before the fix the inflated weight kept it "known" for the whole 300
    second window. identity_min_confidence was not changed."""
    _face(sigs, "camera.front", "Alice", confidence=95.0, age_seconds=100)
    assert identity.resolve(fake_hass).known
    _face(sigs, "camera.front", "Alice", confidence=95.0, age_seconds=130)
    ident = identity.resolve(fake_hass)
    assert not ident.known and ident.method == "low_confidence"
    old = _old_face_votes(identity, {"c": "Alice"}, {"c": {"confidence": 95.0, "age_seconds": 250.0}})
    assert old["Alice"] > 10          # the old weight still read as certain at 250 seconds


def test_the_resolve_subject_callers_see_primary_once_the_face_is_stale(identity, cfg, sigs, fake_hass):
    """memory.remember, the conversation memory and knowledge scope, and the
    command log all take their subject from resolve()."""
    _face(sigs, "camera.front", "Sam Smith", confidence=95.0, age_seconds=10)
    assert identity.resolve_subject(fake_hass) == "sam_smith"
    _face(sigs, "camera.front", "Sam Smith", confidence=95.0, age_seconds=200)
    assert identity.resolve_subject(fake_hass) == identity.DEFAULT_PERSONAL_SUBJECT


def test_quick_identify_with_an_area_is_bounded_and_keeps_its_shape(identity, cfg, sigs, monkeypatch, fake_hass):
    _stub_room(identity, monkeypatch, {"camera.k": "Alice"}, {"camera.k": "kitchen"},
               meta={"camera.k": {"confidence": 98.7, "age_seconds": 0.0}})
    out = identity.quick_identify(fake_hass, "kitchen")
    assert out.known and out.person == "Alice" and 0.0 < out.confidence <= 1.0
    assert all(0.0 <= v <= 1.0 + 1e-9 for v in out.candidates.values()) or max(out.candidates.values()) < 2
    assert identity.quick_person(fake_hass, "kitchen") == "Alice"


def test_no_other_weight_or_threshold_changed(identity):
    assert (identity._W_SOLE_OCCUPANT, identity._W_HOME_PRIOR, identity._W_FACE,
            identity._W_VOICE, identity._W_ROOM_SOLE, identity._W_ROOM_PRESENT,
            identity._W_PROXIMITY) == (0.6, 0.15, 0.8, 0.9, 0.75, 0.30, 0.35)
    assert (identity._ROOM_FRESH_SECS, identity._FACE_RECENCY_WINDOW) == (300.0, 300.0)
    assert identity._DEFAULT_FACE_CONFIDENCE == 0.7
