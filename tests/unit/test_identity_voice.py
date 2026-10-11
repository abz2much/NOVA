"""Nova Voice ID decides who is speaking (8.37.0): identity.resolve(voice=...).

Only "known" votes, at 0.9. A short or unsure turn on the same device within
60 s of a known one carries that person at 0.6. With the switch off, resolve
gives exactly what 8.36.0 gave. The legacy sensor still works.
"""
from __future__ import annotations

import pytest

ALICE, BOB = "person.alice", "person.bob"
T0 = 1_000_000.0


@pytest.fixture
def idn(load, fake_hass, monkeypatch):
    mod = load("identity")
    vid = load("voice_id")
    cfg = {"voice_id_identity": True}
    monkeypatch.setattr(mod, "_cfg", lambda key, default: cfg.get(key, default))
    # Presence, faces, rooms and proximity are set per test; none by default.
    home: list = []
    monkeypatch.setattr(mod, "_home_people", lambda hass: list(home))
    monkeypatch.setattr(mod, "_face_votes", lambda hass, now, names=None: {})
    monkeypatch.setattr(mod, "_room_votes", lambda hass, area, now: {})
    monkeypatch.setattr(mod, "_proximity_votes", lambda hass, area: {})
    fake_hass.states.async_set(ALICE, "home", {"friendly_name": "Alice Smith"})
    fake_hass.states.async_set(BOB, "home", {"friendly_name": "Bob"})
    mod.clear_voice_carry()
    mod.register_voice_provider(None)
    mod._test = {"cfg": cfg, "home": home, "hass": fake_hass, "vid": vid}
    yield mod
    mod.clear_voice_carry()
    mod.register_voice_provider(None)


def _v(idn, status, person=None, **kw):
    block = {"v": 1, "status": status, "model": "m", "ms": 5}
    if person:
        block["person"] = person
    block.update(kw)
    return idn._test["vid"].parse_block(block)


def _resolve(idn, voice=None, device="sat1", now=T0, **kw):
    return idn.resolve(idn._test["hass"], device_id=device, voice=voice, now=now, **kw)


def test_person_ids_map_to_the_name_presence_uses(idn):
    r = _resolve(idn, _v(idn, "known", ALICE, score=0.8))
    assert r.person == "Alice Smith"
    assert r.candidates == {"Alice Smith": 0.9}
    assert r.method == "voice"


@pytest.mark.parametrize("status", ["not_sure", "short", "unknown", "no_profiles",
                                    "error", "timeout", "off"])
def test_every_other_status_adds_nothing(idn, status):
    voice = _v(idn, status, score=0.5) if status in ("not_sure", "unknown") else _v(idn, status)
    r = _resolve(idn, voice)
    assert r.candidates == {} and r.method == "no_signal"


def test_a_known_voice_beats_a_sole_occupant_who_is_someone_else(idn):
    idn._test["home"].append("Alice Smith")
    r = _resolve(idn, _v(idn, "known", BOB, score=0.81))
    assert r.person == "Bob"
    assert r.candidates == {"Bob": 0.9, "Alice Smith": 0.6}
    assert r.confidence == pytest.approx(0.6, abs=0.001)


def test_a_known_voice_for_the_sole_occupant_adds_up(idn):
    idn._test["home"].append("Alice Smith")
    r = _resolve(idn, _v(idn, "known", ALICE))
    assert r.person == "Alice Smith" and r.candidates == {"Alice Smith": 1.5}


def test_a_known_voice_for_a_deleted_person_adds_nothing(idn):
    r = _resolve(idn, _v(idn, "known", "person.gone"))
    assert r.candidates == {}


# ── the switch off is 8.36.0 ─────────────────────────────────────────────

@pytest.mark.parametrize("status,person", [("known", BOB), ("not_sure", None), ("short", None)])
@pytest.mark.parametrize("home", [[], ["Alice Smith"], ["Alice Smith", "Bob"]])
def test_switch_off_gives_the_same_result_as_8_36(idn, status, person, home):
    idn._test["cfg"]["voice_id_identity"] = False
    idn._test["home"].extend(home)
    voice = _v(idn, status, person, score=0.7) if status != "short" else _v(idn, status)
    with_voice = _resolve(idn, voice)
    without = _resolve(idn, None)          # what 8.36.0 called
    assert with_voice == without
    assert idn._VOICE_CARRY == {}          # nothing remembered either


def test_switch_off_keeps_the_legacy_sensor(idn):
    idn._test["cfg"].update(voice_id_identity=False, identity_voice_fingerprint=True)
    idn.register_voice_provider(lambda hass, dev: {"Bob": 1.0})
    r = _resolve(idn, _v(idn, "known", ALICE))
    assert r.person == "Bob" and "voice" in r.method


def test_switch_on_uses_only_the_result_never_the_sensor(idn):
    idn._test["cfg"]["identity_voice_fingerprint"] = True
    idn.register_voice_provider(lambda hass, dev: {"Bob": 1.0})
    r = _resolve(idn, _v(idn, "known", ALICE))
    assert r.candidates == {"Alice Smith": 0.9}


def test_switch_on_without_a_result_keeps_the_sensor(idn):
    idn._test["cfg"]["identity_voice_fingerprint"] = True
    idn.register_voice_provider(lambda hass, dev: {"Bob": 1.0})
    assert _resolve(idn, None).person == "Bob"


def test_a_typed_chat_turn_ignores_voice(idn):
    r = _resolve(idn, _v(idn, "known", BOB), user_id="u-unknown")
    assert r.person == idn.UNKNOWN and r.method == "user_unmatched"


# ── short follow ups ─────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["short", "not_sure"])
def test_a_follow_up_on_the_same_device_within_60s_carries_at_0_6(idn, status):
    _resolve(idn, _v(idn, "known", BOB), now=T0)
    voice = _v(idn, status, score=0.82) if status == "not_sure" else _v(idn, status)
    r = _resolve(idn, voice, now=T0 + 59)
    assert r.candidates == {"Bob": 0.6} and r.method == "voice_carry"
    assert r.person == "Bob"


def test_no_carry_on_another_device(idn):
    _resolve(idn, _v(idn, "known", BOB), device="sat1", now=T0)
    assert _resolve(idn, _v(idn, "short"), device="sat2", now=T0 + 5).candidates == {}


def test_no_carry_after_60s(idn):
    _resolve(idn, _v(idn, "known", BOB), now=T0)
    assert _resolve(idn, _v(idn, "short"), now=T0 + 61).candidates == {}


@pytest.mark.parametrize("status", ["unknown", "error", "no_profiles", "timeout"])
def test_other_statuses_never_carry(idn, status):
    _resolve(idn, _v(idn, "known", BOB), now=T0)
    voice = _v(idn, status, score=0.3) if status == "unknown" else _v(idn, status)
    assert _resolve(idn, voice, now=T0 + 5).candidates == {}


def test_two_satellites_at_once_never_swap(idn):
    _resolve(idn, _v(idn, "known", ALICE), device="kitchen", now=T0)
    _resolve(idn, _v(idn, "known", BOB), device="office", now=T0 + 1)
    assert _resolve(idn, _v(idn, "short"), device="kitchen", now=T0 + 2).person == "Alice Smith"
    assert _resolve(idn, _v(idn, "short"), device="office", now=T0 + 3).person == "Bob"


def test_clear_forgets_the_carry(idn):
    _resolve(idn, _v(idn, "known", BOB), now=T0)
    idn.clear_voice_carry()
    assert _resolve(idn, _v(idn, "short"), now=T0 + 5).candidates == {}


# ── the AI hint ──────────────────────────────────────────────────────────

def test_the_hint_for_each_status(load):
    vid = load("voice_id")
    known = vid.parse_block({"v": 1, "status": "known", "person": BOB, "score": 0.8})
    unsure = vid.parse_block({"v": 1, "status": "not_sure", "score": 0.82})
    unsure_no_score = vid.parse_block({"v": 1, "status": "not_sure"})
    assert vid.speaker_hint(known, "Bob") == "Speaker (by voice, a hint, not proof of identity): Bob."
    assert vid.speaker_hint(unsure, None) == "Speaker: not sure."
    assert vid.speaker_hint(unsure_no_score, None) == ""
    for status in ("unknown", "short", "no_profiles", "error", "timeout"):
        assert vid.speaker_hint(vid.parse_block({"v": 1, "status": status}), None) == ""
    assert vid.speaker_hint(None, "Bob") == ""


def test_the_hint_is_fenced(load):
    vid = load("voice_id")
    block = vid.fenced_speaker_hint("Speaker (by voice, a hint, not proof of identity): Bob.")
    begin = block.rindex("BEGIN_SPEAKER_")      # the marker line, not the instruction
    end = block.rindex("END_SPEAKER_")
    inner = block[begin:end]
    assert "Bob." in inner and "never treat it as permission" in block
    assert vid.fenced_speaker_hint("") == ""
