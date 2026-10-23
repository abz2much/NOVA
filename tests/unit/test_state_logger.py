"""Who-did-it stamping in the pattern store.

Two layers are covered here: StateLogger writing rows (with the person
columns, and their migration onto an older patterns.db), and the
state_changed listener deciding which person, if any, a change is put down
to. Other listener rules (ignores, rate limit, areas) live in
test_cognitive_core_tick.py.
"""
from __future__ import annotations

import sqlite3

import pytest

# Every column the person work added to state_changes.
PERSON_COLUMNS = {"person", "person_confidence", "detection_confidence",
                  "source_entity_id", "source_confidence"}


def columns(db_path):
    with sqlite3.connect(db_path) as conn:
        return {info[1] for info in conn.execute("PRAGMA table_info(state_changes)")}


def stored(db_path, entity_id, *fields):
    """Every row logged for `entity_id`, as tuples of the asked-for fields."""
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            f"SELECT {', '.join(fields)} FROM state_changes WHERE entity_id = ? ORDER BY id",
            (entity_id,)).fetchall()


@pytest.fixture
def cc(load):
    return load("cognitive_core")


@pytest.fixture
def store(cc, tmp_path):
    return cc.StateLogger(str(tmp_path / "learning" / "patterns.db"))


# ── the logger itself ───────────────────────────────────────────────────────

def test_a_new_store_has_every_person_column(store):
    assert PERSON_COLUMNS <= columns(store._db_path)


@pytest.mark.parametrize("kwargs, expected", [
    ({}, ("unknown", 0.0)),
    ({"person": "alex"}, ("alex", 0.0)),
    ({"person": "alex", "person_confidence": 0.8}, ("alex", 0.8)),
])
def test_the_person_is_stored_with_its_confidence(store, kwargs, expected):
    store.log_state_change("light.kitchen", "off", "on", **kwargs)
    assert stored(store._db_path, "light.kitchen", "person", "person_confidence") == [expected]


def test_what_caused_the_change_is_kept(store):
    store.log_state_change("light.porch", "off", "on", triggered_by="automation",
                           source_entity_id="automation.porch_schedule",
                           source_confidence=1.0)
    assert stored(store._db_path, "light.porch",
                  "triggered_by", "source_entity_id", "source_confidence") == [
        ("automation", "automation.porch_schedule", 1.0)]


@pytest.mark.parametrize("forced, rows", [(False, 0), (True, 1)])
def test_a_noisy_domain_needs_the_opt_in(store, forced, rows):
    store.log_state_change("binary_sensor.garage_door_1", "off", "on", force_include=forced)
    assert len(stored(store._db_path, "binary_sensor.garage_door_1", "id")) == rows


LEGACY_SCHEMA = """
    CREATE TABLE state_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL, entity_id TEXT NOT NULL, domain TEXT NOT NULL,
        old_state TEXT, new_state TEXT NOT NULL, area_id TEXT,
        hour INTEGER, day_of_week INTEGER, triggered_by TEXT DEFAULT 'system'
    );
    INSERT INTO state_changes (timestamp, entity_id, domain, old_state,
                               new_state, hour, day_of_week)
    VALUES ('2026-01-01T07:30:00', 'light.hall', 'light', 'off', 'on', 7, 3);
"""


def test_an_older_store_gains_the_person_columns_in_place(cc, tmp_path):
    """A pattern store from before person stamping must be upgraded, not
    only fresh ones, and its existing rows keep their data."""
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_SCHEMA)

    first = cc.StateLogger(path)
    cc.StateLogger(path)                     # a second start must be harmless

    assert PERSON_COLUMNS <= columns(path)
    assert stored(path, "light.hall", "person", "person_confidence",
                  "new_state", "hour", "day_of_week") == [("unknown", 0.0, "on", 7, 3)]
    first.log_state_change("light.hall", "on", "off", person="sam")
    assert [r[0] for r in stored(path, "light.hall", "person")] == ["unknown", "sam"]


# ── the listener's attribution ──────────────────────────────────────────────

@pytest.fixture
def listening(cc, fake_hass, monkeypatch, tmp_path):
    """A running core with a fresh store and an empty per-entity rate limit."""
    core = cc._CoreState()
    core.hass = fake_hass
    core.running = True
    core.state_logger = cc.StateLogger(str(tmp_path / "patterns.db"))
    monkeypatch.setattr(cc, "_CORE", core)
    monkeypatch.setattr(cc, "_PATTERN_LOG_LAST", {})
    return core


def switch_on(cc, entity_id="light.den"):
    from homeassistant.core import State
    cc._on_state_changed(cc.Event("state_changed", {
        "entity_id": entity_id,
        "old_state": State(entity_id, "off"),
        "new_state": State(entity_id, "on"),
    }))


def presence_says(monkeypatch, load, person, confidence, how, candidates):
    identity = load("identity")
    name = identity.UNKNOWN if person is None else person
    monkeypatch.setattr(identity, "quick_identify",
                        lambda hass, area=None: identity.Identification(
                            name, confidence, how, candidates))


def stamp(listening, entity_id="light.den"):
    rows = stored(listening.state_logger._db_path, entity_id, "person", "person_confidence")
    assert len(rows) == 1
    return rows[0]


def test_nobody_home_is_stamped_unknown(cc, listening):
    # No presence stub: with nobody home the real quick check says unknown.
    switch_on(cc)
    assert stamp(listening) == ("unknown", 0.0)


def test_a_confident_identification_is_stamped_as_is(cc, listening, load, monkeypatch):
    presence_says(monkeypatch, load, "alex", 0.6, "sole_occupant", {"alex": 0.6})
    switch_on(cc)
    person, confidence = stamp(listening)
    assert person == "alex" and confidence == pytest.approx(0.6)


# Below the bar, a clear leader is kept as a capped low-confidence guess so
# that person's routines can still build up; a near tie stays unknown.
@pytest.mark.parametrize("candidates, person, confidence", [
    ({"sam": 0.42, "alex": 0.10}, "sam", 0.36),
    ({"alex": 0.30, "sam": 0.15}, "alex", 0.21),
    ({"alex": 0.90}, "alex", 0.44),                     # capped
    ({"sam": 0.30, "alex": 0.28}, "unknown", 0.0),      # coin flip
    ({"sam": 0.0, "alex": 0.0}, "unknown", 0.0),        # no signal at all
])
def test_a_best_guess_below_the_bar(cc, listening, load, monkeypatch,
                                    candidates, person, confidence):
    presence_says(monkeypatch, load, None, 0.0, "low_confidence", candidates)
    switch_on(cc)
    got_person, got_confidence = stamp(listening)
    assert got_person == person
    assert got_confidence == pytest.approx(confidence)
    assert got_confidence <= 0.44


def test_a_broken_presence_check_still_logs_the_change(cc, listening, load, monkeypatch):
    identity = load("identity")

    def _fail(hass, area_id=None):
        raise RuntimeError("presence offline")
    monkeypatch.setattr(identity, "quick_identify", _fail)
    switch_on(cc)                            # must not raise
    assert stamp(listening) == ("unknown", 0.0)
