"""Legacy current-speaker sensors (voice_recognition.py), fixed in 8.37.0."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def vr(load, monkeypatch):
    mod = load("voice_recognition")
    cfg = {"voice_recognition_confidence": 0.85}
    monkeypatch.setattr(mod, "_cfg", lambda key, default: cfg.get(key, default))
    mod._test_cfg = cfg
    return mod


def _set(hass, eid, state, age_s=0.0, **attrs):
    st = hass.states.async_set(eid, state, attrs)
    st.last_updated = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return st


@pytest.mark.parametrize("state", ["uncertain", "unbekannt", "tv-noise", "Uncertain", "TV-Noise"])
def test_no_speaker_states_name_nobody(vr, fake_hass, state):
    vr._test_cfg["voice_recognition_source"] = "sensor.current_speaker"
    _set(fake_hass, "sensor.current_speaker", state)
    assert vr.identify(fake_hass) == {}


def test_a_real_name_still_counts(vr, fake_hass):
    vr._test_cfg["voice_recognition_source"] = "sensor.current_speaker"
    _set(fake_hass, "sensor.current_speaker", "Alice")
    assert vr.identify(fake_hass) == {"Alice": 0.85}


def test_the_glob_path_skips_a_stale_sensor(vr, fake_hass):
    vr._test_cfg["voice_recognition_source"] = "binary_sensor.*_voice"
    _set(fake_hass, "binary_sensor.alice_voice", "on", age_s=5)
    _set(fake_hass, "binary_sensor.bob_voice", "on", age_s=600)    # left on, long ago
    assert vr.identify(fake_hass) == {"alice": 0.85}


def test_the_docstring_no_longer_says_on_while_they_speak(vr):
    assert "ON while they speak" not in (vr.__doc__ or "")
