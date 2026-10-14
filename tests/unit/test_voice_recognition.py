"""Who is speaking, read from an outside speaker-recognition service
(voice_recognition.py): both sensor styles, freshness, scores, the enrolment
chance and its cool-down, and wiring into identity."""
import time
import types
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def vr(load, monkeypatch):
    mod = load("voice_recognition")
    settings = {}
    monkeypatch.setattr(mod, "_cfg", lambda key, default: settings.get(key, default))
    monkeypatch.setattr(mod, "_last_enroll_fire", {})
    mod.settings = settings
    return mod


def sensor(hass, eid, state, age=0.0, **attrs):
    st = hass.states.async_set(eid, state, attrs)
    st.last_updated = datetime.now(timezone.utc) - timedelta(seconds=age)
    return st


def test_constants(vr):
    assert (vr._RECENCY_WINDOW, vr._ENROLL_COOLDOWN) == (20.0, 300.0)


def test_docstring_wording(vr):
    assert "ON while they speak" not in (vr.__doc__ or "")


def test_cfg_returns_stored_value_even_none(load, monkeypatch):
    mod = load("voice_recognition")
    cfg = load("nova_config")
    monkeypatch.setattr(cfg, "get", lambda key, default=None: None)
    assert mod._cfg("voice_recognition_source", "x") is None

    def boom(key, default=None):
        raise RuntimeError
    monkeypatch.setattr(cfg, "get", boom)
    assert mod._cfg("voice_recognition_source", "x") == "x"


# freshness

@pytest.mark.parametrize("age,expected", [(0, True), (19.5, True), (21, False), (3600, False)])
def test_fresh_by_age(vr, age, expected):
    st = types.SimpleNamespace(last_updated=datetime.now(timezone.utc) - timedelta(seconds=age))
    assert vr._fresh(st) is expected


def test_fresh_custom_window_and_fallbacks(vr):
    old = datetime.now(timezone.utc) - timedelta(seconds=50)
    assert vr._fresh(types.SimpleNamespace(last_updated=old), window=60) is True
    assert vr._fresh(types.SimpleNamespace(last_updated=None, last_changed=old)) is False
    assert vr._fresh(types.SimpleNamespace()) is True
    assert vr._fresh(types.SimpleNamespace(last_updated=datetime.now())) is True   # naive


# no source

def test_nothing_configured(vr, fake_hass):
    assert vr.identify(fake_hass) == {}
    vr.settings["voice_recognition_source"] = "   "
    assert vr.identify(fake_hass) == {}


# one current-speaker sensor

def test_named_speaker(vr, fake_hass):
    vr.settings["voice_recognition_source"] = " sensor.speaker "
    sensor(fake_hass, "sensor.speaker", "  Robin ")
    assert vr.identify(fake_hass) == {"Robin": 0.85}


def test_configured_default_confidence(vr, fake_hass):
    vr.settings.update(voice_recognition_source="sensor.speaker", voice_recognition_confidence="0.6")
    sensor(fake_hass, "sensor.speaker", "Robin")
    assert vr.identify(fake_hass) == {"Robin": 0.6}


@pytest.mark.parametrize("state", ["", "Unknown", "unavailable", "none", "no_speaker",
                                   "not_speaking", "IDLE", "off", "uncertain",
                                   "unbekannt", "tv-noise", "TV_NOISE"])
def test_nobody_states(vr, fake_hass, state):
    vr.settings["voice_recognition_source"] = "sensor.speaker"
    sensor(fake_hass, "sensor.speaker", state)
    assert vr.identify(fake_hass) == {}


def test_missing_or_stale_sensor(vr, fake_hass):
    vr.settings["voice_recognition_source"] = "sensor.speaker"
    assert vr.identify(fake_hass) == {}
    sensor(fake_hass, "sensor.speaker", "Robin", age=30)
    assert vr.identify(fake_hass) == {}


@pytest.mark.parametrize("attrs,score", [
    ({"confidence": 0.7}, 0.7), ({"score": 91}, 0.91), ({"probability": "0.5"}, 0.5),
    ({"similarity": -3}, 0.0), ({"score": 250}, 1.0),
    ({"confidence": "high", "score": 0.4}, 0.4), ({"confidence": None, "similarity": 0.3}, 0.3),
    ({"confidence": 0.2, "score": 0.9}, 0.2), ({"other": 0.1}, 0.85)])
def test_score_attributes(vr, fake_hass, attrs, score):
    vr.settings["voice_recognition_source"] = "sensor.speaker"
    sensor(fake_hass, "sensor.speaker", "Robin", **attrs)
    assert vr.identify(fake_hass) == {"Robin": pytest.approx(score)}


# per-person sensors

def test_glob_sensors(vr, fake_hass):
    vr.settings["voice_recognition_source"] = "binary_sensor.*_voice"
    sensor(fake_hass, "binary_sensor.robin_voice", "on")
    sensor(fake_hass, "binary_sensor.casey_voice", "off")
    sensor(fake_hass, "binary_sensor.drew_voice", "On", confidence=95)
    sensor(fake_hass, "binary_sensor.robin_motion", "on")
    assert vr.identify(fake_hass) == {"robin": 0.85, "drew": pytest.approx(0.95)}


def test_glob_skips_sensors_left_on(vr, fake_hass):
    vr.settings["voice_recognition_source"] = "binary_sensor.*_voice"
    sensor(fake_hass, "binary_sensor.robin_voice", "on", age=120)
    assert vr.identify(fake_hass) == {}


def test_plain_id_ending_in_voice_is_a_pattern(vr, fake_hass):
    vr.settings["voice_recognition_source"] = "binary_sensor.robin_voice"
    sensor(fake_hass, "binary_sensor.robin_voice", "on")
    sensor(fake_hass, "binary_sensor.casey_voice", "on")
    assert vr.identify(fake_hass) == {"robin": 0.85}


@pytest.mark.parametrize("eid,name,person", [
    ("binary_sensor.robin_voice", None, "robin"),
    ("binary_sensor.robin_speaking", None, "robin"),
    ("binary_sensor.robin_is_speaking", None, "robin_is"),
    ("binary_sensor.robin_x", "Robin Voice", "robin"),
    ("binary_sensor.robin_x", "Robin Is Speaking", "robin is"),
    ("binary_sensor.robin_x", "  Casey  ", "casey"),
])
def test_person_names(vr, fake_hass, eid, name, person):
    vr.settings["voice_recognition_source"] = "binary_sensor.robin_*"
    attrs = {"friendly_name": name} if name else {}
    sensor(fake_hass, eid, "on", **attrs)
    assert list(vr.identify(fake_hass)) == [person]


def test_same_person_keeps_highest_score(vr, fake_hass):
    vr.settings["voice_recognition_source"] = "binary_sensor.*"
    sensor(fake_hass, "binary_sensor.a", "on", friendly_name="Robin", score=0.4)
    sensor(fake_hass, "binary_sensor.b", "on", friendly_name="robin voice", score=0.9)
    sensor(fake_hass, "binary_sensor.c", "on", friendly_name="ROBIN", score=0.5)
    assert vr.identify(fake_hass) == {"robin": 0.9}


@pytest.mark.parametrize("blank", [None, "", "abc"])
def test_blank_confidence_falls_back_to_default(vr, fake_hass, blank):
    vr.settings["voice_recognition_source"] = "binary_sensor.robin_*"
    sensor(fake_hass, "binary_sensor.robin_x", "on", friendly_name="Robin")
    vr.settings["voice_recognition_confidence"] = 0.85
    expected = vr.identify(fake_hass)
    vr.settings["voice_recognition_confidence"] = blank
    assert vr.identify(fake_hass) == expected == {"robin": 0.85}


# enrolment chance

@pytest.fixture
def who(load, monkeypatch):
    ident = load("identity")
    now = {"r": types.SimpleNamespace(known=True, method="presence", person="Robin")}
    seen = []

    def resolve(hass, device_id=None, **kw):
        seen.append(device_id)
        return now["r"]
    monkeypatch.setattr(ident, "resolve", resolve)
    now["seen"] = seen
    return now


def test_candidate_when_voice_does_not_know_them(vr, fake_hass, who):
    assert vr.enrollment_candidate(fake_hass, "sat1") == "Robin"
    assert who["seen"] == ["sat1"]


def test_no_candidate_when_unknown_or_voice_known(vr, fake_hass, who):
    who["r"] = types.SimpleNamespace(known=False, method="none", person="")
    assert vr.enrollment_candidate(fake_hass) is None
    who["r"] = types.SimpleNamespace(known=True, method="voice+presence", person="Robin")
    assert vr.enrollment_candidate(fake_hass) is None


def test_no_candidate_when_voice_already_hears_them(vr, fake_hass, who):
    vr.settings["voice_recognition_source"] = "sensor.speaker"
    sensor(fake_hass, "sensor.speaker", "robin")
    assert vr.enrollment_candidate(fake_hass) is None


def test_normalize_uses_identity(vr, load, monkeypatch):
    monkeypatch.setattr(load("identity"), "normalize", lambda n: f"<{n}>")
    assert vr.normalize("x") == "<x>"


def _enabled(vr, **extra):
    vr.settings.update(identity_voice_fingerprint=True,
                       voice_recognition_source="sensor.speaker", **extra)


@pytest.mark.parametrize("settings", [
    {},
    {"identity_voice_fingerprint": True},
    {"identity_voice_fingerprint": True, "voice_recognition_source": "sensor.s",
     "voice_recognition_auto_enroll": False},
    {"identity_voice_fingerprint": False, "voice_recognition_source": "sensor.s"},
])
def test_enrol_event_needs_every_switch(vr, fake_hass, who, settings):
    vr.settings.update(settings)
    assert vr.maybe_fire_enrollment(fake_hass) is None
    assert fake_hass.bus.fired == []


def test_enrol_event_fires_then_cools_down(vr, fake_hass, who, monkeypatch):
    _enabled(vr)
    fired = []
    monkeypatch.setattr(fake_hass.bus, "async_fire", lambda t, d=None, **k: fired.append((t, d)))
    clock = {"t": 10_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    assert vr.maybe_fire_enrollment(fake_hass, "sat1") == "Robin"
    assert fired == [("nova_voice_enroll_candidate", {"person": "Robin", "device_id": "sat1"})]
    clock["t"] += 299
    assert vr.maybe_fire_enrollment(fake_hass, "sat1") is None
    clock["t"] += 2
    assert vr.maybe_fire_enrollment(fake_hass) == "Robin"
    assert len(fired) == 2


def test_cool_down_is_per_person(vr, fake_hass, who, monkeypatch):
    _enabled(vr)
    fired = []
    monkeypatch.setattr(fake_hass.bus, "async_fire", lambda t, d=None, **k: fired.append(d))
    vr.maybe_fire_enrollment(fake_hass)
    who["r"] = types.SimpleNamespace(known=True, method="face", person="Casey")
    assert vr.maybe_fire_enrollment(fake_hass) == "Casey"
    assert [d["person"] for d in fired] == ["Robin", "Casey"]


def test_failed_event_still_counts(vr, fake_hass, who, monkeypatch):
    _enabled(vr)

    def boom(*a, **k):
        raise RuntimeError("bus closed")
    monkeypatch.setattr(fake_hass.bus, "async_fire", boom)
    assert vr.maybe_fire_enrollment(fake_hass) == "Robin"
    assert vr.maybe_fire_enrollment(fake_hass) is None


def test_no_event_without_candidate(vr, fake_hass, who):
    _enabled(vr)
    who["r"] = types.SimpleNamespace(known=False, method="none", person="")
    assert vr.maybe_fire_enrollment(fake_hass) is None


# wiring

def test_register_and_unregister(vr, load, monkeypatch, fake_hass):
    ident = load("identity")
    got = []
    monkeypatch.setattr(ident, "register_voice_provider", got.append)
    vr.register(fake_hass)
    vr.settings["voice_recognition_source"] = "sensor.speaker"
    sensor(fake_hass, "sensor.speaker", "Robin")
    assert got[0](fake_hass, None) == {"Robin": 0.85}
    vr.unregister()
    assert got[1] is None


def test_register_failures_are_contained(vr, load, monkeypatch, fake_hass):
    def boom(fn):
        raise RuntimeError("identity not ready")
    monkeypatch.setattr(load("identity"), "register_voice_provider", boom)
    vr.register(fake_hass)
    vr.unregister()
