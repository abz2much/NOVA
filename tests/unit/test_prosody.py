"""How Nova sounds for one announcement (audio/prosody.py).

The module has no Home Assistant imports, so it is loaded straight from its
file. Each rule in the spec's table gets a case, plus the quiet window, the
media keys, unreadable numbers and the attentive-listener rate change."""
import importlib.util
import math
import pathlib
import sys
from datetime import datetime

import pytest

_FILE = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "audio" / "prosody.py"


def _module():
    spec = importlib.util.spec_from_file_location("prosody_under_test", _FILE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prosody_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


P = _module()
KEYS = {"volume", "speech_rate", "style", "whisper_mode", "duck_media"}


def voice(**telemetry):
    return P.ProsodyController().calculate_vocal_profile(telemetry)


def test_constants():
    assert (P.DARK_LUX, P.QUIET_DB, P.LOUD_DB) == (5.0, 45.0, 60.0)
    assert (P.DEFAULT_QUIET_START, P.DEFAULT_QUIET_END) == (22, 7)


def test_profile_dataclass_is_frozen_and_converts():
    prof = P.VocalProfile(0.5, 1.0, "neutral", False, True)
    assert prof.as_dict() == {"volume": 0.5, "speech_rate": 1.0, "style": "neutral",
                              "whisper_mode": False, "duck_media": True}
    with pytest.raises(Exception):
        prof.volume = 1.0


def test_constructor_keeps_settings():
    c = P.ProsodyController(46, -1, dark_lux=3.0, quiet_db=40.0, loud_db=70.0)
    assert (c.quiet_hours_start, c.quiet_hours_end) == (22, 23)
    assert (c.dark_lux, c.quiet_db, c.loud_db) == (3.0, 40.0, 70.0)


@pytest.mark.parametrize("hour,expected", [
    (21, False), (22, True), (0, True), (6, True), (7, False), (15, False)])
def test_window_across_midnight(hour, expected):
    assert P.ProsodyController(22, 7).in_quiet_hours(hour) is expected


@pytest.mark.parametrize("hour,expected", [(0, False), (1, True), (5, True), (6, False)])
def test_window_inside_one_day(hour, expected):
    assert P.ProsodyController(1, 6).in_quiet_hours(hour) is expected


def test_equal_start_and_end_means_no_window():
    c = P.ProsodyController(9, 9)
    assert not any(c.in_quiet_hours(h) for h in range(24))


def test_critical_wins_even_in_a_dark_silent_night():
    assert voice(critical_alert=True, hour=2, ambient_lux=0, ambient_db=10) == {
        "volume": 1.0, "speech_rate": 1.15, "style": "authoritative",
        "whisper_mode": False, "duck_media": True}


def test_critical_takes_any_truthy_value():
    assert voice(critical_alert="yes", hour=12)["style"] == "authoritative"


def test_whisper_needs_night_dark_and_quiet_together():
    assert voice(hour=1, ambient_lux=4.9, ambient_db=44.9) == {
        "volume": 0.25, "speech_rate": 0.95, "style": "whisper",
        "whisper_mode": True, "duck_media": False}


def test_night_alone_is_subdued_and_leaves_media_alone():
    out = voice(hour=23, ambient_lux=50, ambient_db=30)
    assert out == {"volume": 0.4, "speech_rate": 0.97, "style": "subdued",
                   "whisper_mode": False, "duck_media": False}


def test_subdued_ducks_playing_media():
    assert voice(hour=23, media_active=True)["duck_media"] is True


def test_dark_quiet_room_by_day_is_subdued():
    assert voice(hour=13, ambient_lux=1, ambient_db=20)["style"] == "subdued"


def test_night_loud_room_stays_subdued():
    assert voice(hour=23, ambient_db=90)["style"] == "subdued"


def test_loud_room_by_day_projects():
    assert voice(hour=13, ambient_db=60) == {
        "volume": 0.9, "speech_rate": 1.0, "style": "projected",
        "whisper_mode": False, "duck_media": True}


def test_media_by_day_projects_with_either_key():
    assert voice(hour=13, media_active=True)["style"] == "projected"
    assert voice(hour=13, media_playing=True)["style"] == "projected"


def test_media_active_false_overrides_media_playing():
    assert voice(hour=13, media_active=False, media_playing=True)["style"] == "neutral"


def test_plain_daytime_is_neutral():
    assert voice(hour=13, ambient_lux=5, ambient_db=45) == {
        "volume": 0.6, "speech_rate": 1.0, "style": "neutral",
        "whisper_mode": False, "duck_media": False}


def test_numbers_given_as_text_are_read():
    assert voice(hour=1, ambient_lux="2", ambient_db="20")["style"] == "whisper"


@pytest.mark.parametrize("bad", [None, True, "unavailable", "NaN", math.inf, -math.inf, [], {}])
def test_unreadable_noise_never_counts(bad):
    out = voice(hour=13, ambient_db=bad, ambient_lux=bad)
    assert out["style"] == "neutral"


def test_no_telemetry_at_all():
    c = P.ProsodyController(clock=lambda: datetime(2026, 1, 1, 12))
    for t in (None, {}):
        out = c.calculate_vocal_profile(t)
        assert set(out) == KEYS and out["style"] == "neutral"


def test_clock_is_used_when_hour_missing_or_invalid():
    c = P.ProsodyController(clock=lambda: datetime(2026, 1, 1, 23))
    assert c.calculate_vocal_profile({})["style"] == "subdued"
    assert c.calculate_vocal_profile({"hour": 24})["style"] == "subdued"
    assert c.calculate_vocal_profile({"hour": "3"})["style"] == "subdued"
    assert c.calculate_vocal_profile({"hour": 12})["style"] == "neutral"


def test_clock_not_called_when_hour_given():
    def never():
        raise AssertionError("clock read")
    assert P.ProsodyController(clock=never).calculate_vocal_profile({"hour": 5})


@pytest.mark.parametrize("telemetry,rate", [
    ({"critical_alert": True}, 1.1),
    ({"hour": 1, "ambient_lux": 0, "ambient_db": 0}, 0.9),
    ({"hour": 23}, 0.92),
    ({"hour": 12, "media_active": True}, 0.95),
    ({"hour": 12}, 0.95),
])
def test_attentive_listener_slows_rate_only(telemetry, rate):
    plain = voice(**telemetry)
    eased = voice(skip_preamble=True, **telemetry)
    assert eased["speech_rate"] == rate
    assert {k: v for k, v in eased.items() if k != "speech_rate"} == \
        {k: v for k, v in plain.items() if k != "speech_rate"}
