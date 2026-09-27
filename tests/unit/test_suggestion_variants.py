"""One pending suggestion per behaviour across presence-release variants.

A gated sequence and the same sequence with "off when presence clears" have
different identities, so a dismissed plain version stays dismissed while the
release version can still be proposed. Only one of them may be pending.
"""
from __future__ import annotations

import sqlite3

from tests.unit.test_automation_phase5_fixes import store_db  # noqa: F401


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
