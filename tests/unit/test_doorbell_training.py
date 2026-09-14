"""Tests for doorbell visitor-pattern mining (find_patterns). This is NOT face
recognition — Nova has no local face model; identity only ever comes from
Frigate/DoubleTake (recognition.py), neither configured here. find_patterns
only clusters the vision model's own category label by camera + time of day.

Timestamps are always computed relative to real utcnow() (never hardcoded
calendar dates) since find_patterns() filters against datetime.utcnow() —
a fixed date would eventually drift outside the lookback window and start
failing for reasons unrelated to the code under test.
"""
import json
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def dbt(load, tmp_path, monkeypatch):
    mod = load("doorbell_training")
    # LOG_DIR is a separate constant (log_event calls os.makedirs(LOG_DIR, ...))
    # -- patch both, or log_event would try to create the real /config/nova
    # on whatever machine runs the tests.
    monkeypatch.setattr(mod, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "LOG_PATH", str(tmp_path / "doorbell_log.jsonl"))
    return mod


def _write(dbt, rows):
    """Write raw JSONL rows directly (bypassing log_event) so timestamps are
    fully controlled for pattern-detection tests."""
    with open(dbt.LOG_PATH, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _ago(days: int, hour: int, minute: int = 0) -> str:
    """A timestamp `days` ago at a fixed UTC hour, in the module's log format."""
    dt = (datetime.utcnow() - timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    return dt.isoformat() + "Z"


def _event(ts, camera="camera.front_door", category="delivery", notable=True):
    return {"ts": ts, "camera": camera, "entity_id": camera, "image_source": "live",
            "summary": "", "analysis": "", "category": category, "notable": notable}


# ── log_event: speak field (v7.101.8) ───────────────────────────────────────
# Caught live: with announcements_enabled off, the LLM-generated "speak" line
# was computed but never persisted anywhere, so there was no way to see after
# the fact what a notable event would have sounded like.

def test_log_event_persists_speak(dbt):
    dbt.log_event("Front Door", "camera.front_door_bell", "live", {
        "summary": "A man approached.", "analysis": "...", "category": "person",
        "notable": True, "speak": "Sir, you have a visitor at the door.",
    })
    events = dbt.load_events()
    assert len(events) == 1
    assert events[0]["speak"] == "Sir, you have a visitor at the door."


def test_log_event_speak_defaults_to_empty_string(dbt):
    # A known-resident / no-vision-call log (source="eufy") never had a
    # "speak" value at all — must not store None or crash.
    dbt.log_event("Front Door", "camera.front_door_bell", "eufy", {
        "summary": "", "analysis": "", "category": "known_resident", "notable": False,
    })
    events = dbt.load_events()
    assert events[0]["speak"] == ""


def test_log_event_speak_none_becomes_empty_string(dbt):
    dbt.log_event("Front Door", "camera.front_door_bell", "live", {
        "summary": "Empty porch.", "analysis": "...", "category": "person",
        "notable": True, "speak": None,
    })
    events = dbt.load_events()
    assert events[0]["speak"] == ""


def test_no_events_returns_empty(dbt):
    assert dbt.find_patterns() == []


def test_recurring_pattern_detected_across_distinct_days(dbt):
    # Delivery courier at the same camera, same hour, on 4 distinct days.
    rows = [_event(_ago(d, 15)) for d in (0, 1, 2, 3)]
    _write(dbt, rows)
    patterns = dbt.find_patterns(min_occurrences=3)
    assert len(patterns) == 1
    p = patterns[0]
    assert p["camera"] == "camera.front_door"
    assert p["category"] == "delivery"
    assert p["occurrences"] == 4
    assert p["distinct_days"] == 4
    assert p["typical_hour"] == 15
    assert "delivery" in p["description"].lower()
    assert "4 of the last" in p["description"]


def test_same_day_repeats_are_not_a_pattern(dbt):
    # Three presses in one afternoon, all on the SAME day — not a time-of-day
    # routine, just one chatty visit. Must not be reported.
    rows = [_event(_ago(0, h)) for h in (14, 15, 16)]
    _write(dbt, rows)
    assert dbt.find_patterns(min_occurrences=3) == []


def test_below_min_occurrences_is_not_reported(dbt):
    rows = [_event(_ago(d, 15)) for d in (0, 1)]
    _write(dbt, rows)
    assert dbt.find_patterns(min_occurrences=3) == []


def test_different_categories_grouped_separately(dbt):
    rows = (
        [_event(_ago(d, 15), category="delivery") for d in (0, 1, 2)]
        + [_event(_ago(d, 9), category="mail") for d in (0, 1, 2)]
    )
    _write(dbt, rows)
    patterns = dbt.find_patterns(min_occurrences=3)
    cats = {p["category"] for p in patterns}
    assert cats == {"delivery", "mail"}


def test_utc_offset_shifts_hour_across_midnight(dbt):
    # 23:30 UTC + 120min offset = 01:30 local, still the "same evening visit"
    # pattern but bucketed to the correct local hour.
    rows = [_event(_ago(d, 23, 30)) for d in (0, 1, 2)]
    _write(dbt, rows)
    local = dbt.find_patterns(min_occurrences=3, utc_offset_minutes=120)
    utc = dbt.find_patterns(min_occurrences=3, utc_offset_minutes=0)
    assert local[0]["typical_hour"] == 1
    assert utc[0]["typical_hour"] == 23


def test_events_outside_lookback_window_are_ignored(dbt):
    rows = [_event(_ago(d, 15)) for d in (90, 91, 92)]
    _write(dbt, rows)
    assert dbt.find_patterns(min_occurrences=3, lookback_days=30) == []


def test_results_sorted_by_distinct_days_descending(dbt):
    rows = (
        [_event(_ago(d, 15), category="delivery") for d in (0, 1, 2)]
        + [_event(_ago(d, 9), category="mail") for d in (0, 1, 2, 3, 4)]
    )
    _write(dbt, rows)
    patterns = dbt.find_patterns(min_occurrences=3)
    assert [p["category"] for p in patterns] == ["mail", "delivery"]


def test_never_raises_on_corrupt_log(dbt):
    with open(dbt.LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("not json\n")
    assert dbt.find_patterns() == []
