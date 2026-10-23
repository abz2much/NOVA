"""The "And if" part of a learned sequence: a clock window or an after-dark
condition worked out from when the action happened.

A sequence confined to one clear stretch of the day gets a time condition so
the suggested automation does not fire at odd hours; one spread across the
day gets none. Mostly-in-the-dark sequences get a sun condition instead,
which follows the seasons. Whatever is learned must survive generation and
normalisation into the installed automation.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import pytest

# Somewhere with a real sunset; any coordinates do while _is_dark_at is stubbed.
_LAT, _LON = 40.7, -74.0
_AFTER_DARK = {"condition": "sun", "after": "sunset", "before": "sunrise"}


@pytest.fixture
def mod(load):
    return load("pattern_analyzer")


def _times(hours, days=range(1, 6)):
    """Local epochs on each of ``days`` (June 2026) at each of ``hours``."""
    return [datetime(2026, 6, d, h).timestamp() for d in days for h in hours]


def _window(after, before):
    return {"condition": "time", "after": after, "before": before}


def _dark_answers(monkeypatch, mod, *answers):
    """Make _is_dark_at give ``answers`` in turn (or one answer for every call)."""
    if len(answers) == 1:
        monkeypatch.setattr(mod, "_is_dark_at", lambda epoch, lat, lon: answers[0])
    else:
        queue = list(answers)
        monkeypatch.setattr(mod, "_is_dark_at", lambda epoch, lat, lon: queue.pop(0))


# ── clock windows ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("epochs, expected", [
    pytest.param(_times((17, 19, 21, 23)), _window("17:00:00", "00:00:00"),
                 id="evening-up-to-midnight"),
    pytest.param(_times((22, 23, 0, 1, 2)), _window("22:00:00", "03:00:00"),
                 id="overnight-wraps"),
    pytest.param(_times((2, 8, 14, 20)), None,
                 id="spread-over-the-day"),          # every gap 6 h, below 8
    pytest.param(_times((18,), range(1, 7)), _window("17:00:00", "19:00:00"),
                 id="one-hour-gets-an-hour-either-side"),
    pytest.param(_times((0,)), _window("23:00:00", "01:00:00"),
                 id="one-hour-at-midnight-wraps"),
    pytest.param(_times(range(17), range(1, 2)), _window("00:00:00", "17:00:00"),
                 id="eight-quiet-hours-is-enough"),
    pytest.param(_times(range(18), range(1, 2)), None,
                 id="seven-quiet-hours-is-not"),
])
def test_time_window_from_when_it_happened(mod, epochs, expected):
    assert mod._time_window_condition(epochs) == expected


@pytest.mark.parametrize("count", [0, 2, 4])
def test_too_few_times_give_no_window(mod, count):
    assert count < mod.MIN_OCCURRENCES
    assert mod._time_window_condition(_times((18,), range(1, count + 1))) is None


def test_the_minimum_number_of_times_is_enough_for_a_window(mod):
    epochs = _times((18,), range(1, mod.MIN_OCCURRENCES + 1))

    assert mod._time_window_condition(epochs) == _window("17:00:00", "19:00:00")


# ── after-dark conditions ───────────────────────────────────────────────────

def test_always_dark_gives_the_sun_condition(mod, monkeypatch):
    _dark_answers(monkeypatch, mod, True)

    assert mod._sun_condition(_times((20, 22)), _LAT, _LON) == _AFTER_DARK


def test_always_light_gives_no_sun_condition(mod, monkeypatch):
    _dark_answers(monkeypatch, mod, False)

    assert mod._sun_condition(_times((10, 12)), _LAT, _LON) is None


@pytest.mark.parametrize("answers, expected", [
    ((True, True, True, False, False, False), None),          # half: unreliable
    ((True, True, True, True, False), _AFTER_DARK),           # 80% is the bar
    ((True, True, True, False, False), None),                 # 60%
])
def test_the_share_of_dark_times_needed(mod, monkeypatch, answers, expected):
    _dark_answers(monkeypatch, mod, *answers)
    epochs = _times((12,), range(1, len(answers) + 1))

    assert mod._sun_condition(epochs, _LAT, _LON) == expected


@pytest.mark.parametrize("lat, lon", [(None, None), (None, _LON), (_LAT, None)])
def test_no_location_means_no_sun_condition(mod, monkeypatch, lat, lon):
    _dark_answers(monkeypatch, mod, True)

    assert mod._sun_condition(_times((22,), range(1, 7)), lat, lon) is None


def test_too_few_times_give_no_sun_condition(mod, monkeypatch):
    _dark_answers(monkeypatch, mod, True)

    assert mod._sun_condition(_times((22,), range(1, 3)), _LAT, _LON) is None


def test_unknown_darkness_is_not_counted_as_dark(mod, monkeypatch):
    # When the sun cannot be worked out, there is nothing to base it on.
    _dark_answers(monkeypatch, mod, None)

    assert mod._sun_condition(_times((22,), range(1, 7)), _LAT, _LON) is None


def test_without_astral_darkness_is_unknown(mod, monkeypatch):
    monkeypatch.setitem(sys.modules, "astral", None)     # import now fails

    assert mod._is_dark_at(datetime(2026, 1, 15, 7).timestamp(), _LAT, _LON) is None


def test_darkness_from_the_real_sun_times(mod):
    pytest.importorskip("astral")
    # New York in mid-January: 07:00 UTC is about 02:00 local, before sunrise;
    # 17:00 UTC is about midday.
    night = datetime(2026, 1, 15, 7, tzinfo=timezone.utc).timestamp()
    noon = datetime(2026, 1, 15, 17, tzinfo=timezone.utc).timestamp()

    assert mod._is_dark_at(night, 40.7128, -74.0060) is True
    assert mod._is_dark_at(noon, 40.7128, -74.0060) is False


# ── wording ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("condition, words", [
    (_window("17:00:00", "00:00:00"), ", mostly between 17:00 and 00:00"),
    (_AFTER_DARK, ", mostly after dark"),
    (None, ""),
    ([_window("06:00:00", "08:00:00"), _AFTER_DARK],
     ", mostly between 06:00 and 08:00, mostly after dark"),
])
def test_how_a_condition_reads_in_a_description(mod, condition, words):
    assert mod._condition_phrase(condition) == words


# ── into the automation ─────────────────────────────────────────────────────

def _hall_sequence(mod, condition):
    return mod.DetectedPattern(
        pattern_type="sequence", description="hall chain",
        entity_ids=["binary_sensor.hall_motion", "light.hall"], confidence=0.7,
        occurrences=8,
        details={"trigger": {"entity": "binary_sensor.hall_motion", "state": "on"},
                 "action": {"entity": "light.hall", "state": "on"},
                 "delay_seconds": 30, "condition": condition})


@pytest.mark.parametrize("learned", [
    _window("17:00:00", "00:00:00"),
    _AFTER_DARK,
    [_window("17:00:00", "00:00:00")],
])
def test_a_learned_condition_reaches_the_automation(mod, learned):
    auto = json.loads(mod.PatternAnalyzer()._generate_automation(_hall_sequence(mod, learned)))

    assert auto["trigger"]["platform"] == "state"
    assert auto["condition"] == (learned if isinstance(learned, list) else [learned])


def test_no_condition_key_when_nothing_was_learned(mod):
    auto = json.loads(mod.PatternAnalyzer()._generate_automation(_hall_sequence(mod, None)))

    assert "condition" not in auto


def _stored(condition):
    return json.dumps({
        "alias": "Hall light after motion",
        "trigger": {"platform": "state", "entity_id": "binary_sensor.hall_motion", "to": "on"},
        "action": [{"service": "light.turn_on"}],
        "condition": condition,
    })


def test_normalising_keeps_the_condition_and_renames_old_keys(mod):
    window = _window("17:00:00", "00:00:00")

    result = mod.normalize_suggestion_automation(_stored([window]))

    assert result["installable"] is True
    assert result["condition"] == [window]
    assert result["trigger"][0]["trigger"] == "state"
    assert result["action"] == [{"action": "light.turn_on"}]


def test_normalising_turns_a_single_condition_into_a_list(mod):
    result = mod.normalize_suggestion_automation(_stored(_AFTER_DARK))

    assert result["condition"] == [_AFTER_DARK]
