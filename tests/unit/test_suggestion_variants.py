"""One pending suggestion per behaviour across presence-release variants.

A gated sequence and the same sequence with "off when presence clears" have
different identities, so a dismissed plain version stays dismissed while the
release version can still be proposed. Only one of them may be pending.
"""
from __future__ import annotations

import sqlite3

import sys
import types
import pytest


def _sequence(models, *, release, count):
    details = {
        "trigger": {"entity": "binary_sensor.front_door", "state": "on"},
        "action": {"entity": "light.hall", "state": "on"},
        "delay_seconds": 60,
        "condition": [{"condition": "state",
                       "entity_id": "binary_sensor.hall_presence",
                       "state": "on"}],
        "presence_gate": {"entity_id": "binary_sensor.hall_presence",
                          "area_id": "hall", "area_name": "Hall"},
    }
    if release:
        details["presence_release"] = {
            "entity_id": "binary_sensor.hall_presence", "area_id": "hall",
            "area_name": "Hall", "settle_seconds": 60}
    # The real description: a release does not change it.
    description = (
        "When binary_sensor.front_door turns on, light.hall turns on shortly "
        f"after ({count} times in 30 days, ~60s later), only when presence is "
        "detected in Hall")
    return models.DetectedPattern(
        "sequence", description, ["binary_sensor.front_door", "light.hall"],
        0.9, count, details=details)


def _rows(db):
    conn = sqlite3.connect(db)
    try:
        return [(sid, status, "presence_release" in details, count)
                for sid, status, details, count in conn.execute(
                    "SELECT id, status, details, pattern_count FROM suggestions "
                    "ORDER BY id")]
    finally:
        conn.close()


def _store(load, db):
    suggestions, models = load("automation.suggestions"), load("automation.models")
    return suggestions.SuggestionStore(db), models


def test_flipping_detection_leaves_one_pending_row(load, store_db):
    store, models = _store(load, store_db)
    assert store.store(_sequence(models, release=False, count=11)) is True
    assert store.store(_sequence(models, release=True, count=10)) is True
    assert store.store(_sequence(models, release=False, count=11)) is False

    assert _rows(store_db) == [
        (1, "pending", False, 11),
        (2, "superseded", True, 10),
    ]
    assert [row["id"] for row in store.pending()] == [1]


def test_latest_variant_is_the_pending_one(load, store_db):
    store, models = _store(load, store_db)
    store.store(_sequence(models, release=False, count=11))
    store.store(_sequence(models, release=True, count=10))
    assert _rows(store_db) == [
        (1, "superseded", False, 11),
        (2, "pending", True, 10),
    ]
    assert store.store(_sequence(models, release=True, count=12)) is False
    assert _rows(store_db) == [
        (1, "superseded", False, 11),
        (2, "pending", True, 12),
    ]


def test_equal_counts_do_not_merge_variants_through_the_description(
        load, store_db):
    store, models = _store(load, store_db)
    store.store(_sequence(models, release=False, count=11))
    assert store.store(_sequence(models, release=True, count=11)) is True
    assert _rows(store_db) == [
        (1, "superseded", False, 11),
        (2, "pending", True, 11),
    ]


def test_dismissed_plain_stays_dismissed_and_release_is_still_new(
        load, store_db):
    store, models = _store(load, store_db)
    store.store(_sequence(models, release=False, count=11))
    assert store.dismiss(1) is True
    # Same count, so the description is identical to the dismissed row.
    assert store.store(_sequence(models, release=True, count=11)) is True
    assert store.store(_sequence(models, release=False, count=13)) is False
    assert _rows(store_db) == [
        (1, "dismissed", False, 13),
        (2, "pending", True, 11),
    ]


def test_installed_release_is_untouched_by_the_plain_variant(load, store_db):
    store, models = _store(load, store_db)
    store.store(_sequence(models, release=True, count=10))
    store.mark_installed(1, "nova_auto_hall")
    assert store.store(_sequence(models, release=False, count=12)) is True
    assert store.store(_sequence(models, release=True, count=14)) is False
    rows = _rows(store_db)
    assert rows[0][:3] == (1, "installed", True)
    assert rows[1][:3] == (2, "pending", False)


def test_decided_rows_are_never_retired(load, store_db):
    store, models = _store(load, store_db)
    store.store(_sequence(models, release=False, count=11))
    store.approve(1)
    store.store(_sequence(models, release=True, count=10))
    store.mark_covered(2)
    store.store(_sequence(models, release=False, count=12))
    store.store(_sequence(models, release=True, count=13))
    assert [row[1] for row in _rows(store_db)] == ["approved", "already_automated"]


# ── a suggestion keeps its identity as counts change ──────────────────────────

_SCHEMA = """
CREATE TABLE state_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    entity_id TEXT NOT NULL, domain TEXT NOT NULL, old_state TEXT,
    new_state TEXT NOT NULL, area_id TEXT, hour INTEGER, day_of_week INTEGER,
    triggered_by TEXT DEFAULT 'system', person TEXT DEFAULT 'unknown');
CREATE TABLE commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, text TEXT NOT NULL,
    handled_by TEXT DEFAULT 'agent', entity_ids TEXT DEFAULT '[]',
    person TEXT DEFAULT 'unknown', hour INTEGER, day_of_week INTEGER);
CREATE TABLE suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL,
    description TEXT NOT NULL, automation_yaml TEXT, status TEXT DEFAULT 'pending',
    confidence REAL DEFAULT 0.0, pattern_count INTEGER DEFAULT 0,
    approved_at TEXT, dismissed_at TEXT, pattern_type TEXT DEFAULT '',
    entity_ids TEXT DEFAULT '', details TEXT DEFAULT '{}');
"""


@pytest.fixture
def store_db(tmp_path, monkeypatch):
    db = tmp_path / "patterns.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    dr = types.ModuleType("jc.decision_record")
    dr.record = lambda *a, **k: None
    dr.set_outcome_by_ref = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "jc.decision_record", dr)
    return str(db)


def _routine(models, days, hour=18, total=30):
    return models.DetectedPattern(
        pattern_type="time_routine",
        description=f"light.porch turns on around {hour:02d}:00 on {days} of {total} days",
        entity_ids=["light.porch"], confidence=days / total, occurrences=days,
        details={"hour": hour, "state": "on", "observed_days": days})


def _rows_p5(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT id, status, description, pattern_count FROM suggestions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_changing_counts_update_the_same_suggestion(load, store_db):
    suggestions, models = load("automation.suggestions"), load("automation.models")
    store = suggestions.SuggestionStore(store_db)
    assert store.store(_routine(models, 12)) is True
    assert store.store(_routine(models, 14)) is False
    rows = _rows_p5(store_db)
    assert len(rows) == 1
    assert rows[0][1] == "pending" and "14 of 30" in rows[0][2] and rows[0][3] == 14


def test_dismissed_suggestion_stays_dismissed(load, store_db):
    suggestions, models = load("automation.suggestions"), load("automation.models")
    store = suggestions.SuggestionStore(store_db)
    store.store(_routine(models, 12))
    assert store.dismiss(1) is True
    assert store.store(_routine(models, 20)) is False
    assert [r[1] for r in _rows_p5(store_db)] == ["dismissed"]
    assert store.pending() == []


def test_material_change_is_a_new_suggestion(load, store_db):
    suggestions, models = load("automation.suggestions"), load("automation.models")
    store = suggestions.SuggestionStore(store_db)
    store.store(_routine(models, 12))
    store.dismiss(1)
    assert store.store(_routine(models, 12, hour=19)) is True
    assert [r[1] for r in _rows_p5(store_db)] == ["dismissed", "pending"]


def test_sequence_identity_ignores_measured_delay_and_conditions(load):
    suggestions = load("automation.suggestions")
    base = {"trigger": {"entity": "binary_sensor.door", "state": "on"},
            "action": {"entity": "light.hall", "state": "on"}}
    one = suggestions.suggestion_identity(
        "sequence", ["binary_sensor.door", "light.hall"],
        {**base, "delay_seconds": 40, "condition": None})
    two = suggestions.suggestion_identity(
        "sequence", ["binary_sensor.door", "light.hall"],
        {**base, "delay_seconds": 55,
         "condition": [{"condition": "sun", "after": "sunset"}]})
    assert one == two is not None
    other = suggestions.suggestion_identity(
        "sequence", [], {**base, "action": {"entity": "light.hall", "state": "off"}})
    assert other != one
