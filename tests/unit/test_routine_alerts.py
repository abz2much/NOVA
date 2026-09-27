"""Routine-start alerts built from what the analyzer really stores.

Every routine here comes out of PatternAnalyzer._find_time_routines /
_find_repeated_commands over a seeded patterns.db and goes into the real
person_patterns store, so a test cannot pass on a description Nova never
produces (the old tests used a hand-written "start the coffee").
"""
import datetime
import sqlite3

import pytest

from test_pattern_analyzer import _add_command, _add_state, _conn


@pytest.fixture
def pa_mod(load):
    return load("pattern_analyzer")


@pytest.fixture
def cog(load):
    c = load("cognition")
    c._RECUR_ALERTED.clear()
    return c


def _seed(conn, entity, state, positive, total, hour=11, person="abi"):
    """`total` observed days (another entity changes daily); the routine on
    `positive` of them, attributed to `person`."""
    for d in range(1, total + 1):
        _add_state(conn, "sensor.filler", "x%d" % d, d, 3)
    for d in range(1, positive + 1):
        _add_state(conn, entity, state, d, hour, minute=5, person=person)
    conn.commit()


def _detect(pa_mod, tmp_path, name, entity, state, positive, total):
    conn = _conn(tmp_path / name)
    _seed(conn, entity, state, positive, total)
    found = [p for p in pa_mod.PatternAnalyzer()._find_time_routines(conn)
             if p.entity_ids == [entity]]
    conn.close()
    assert found and found[0].details.get("person") == "abi"
    return found[0]


def _analyzer(pa_mod, tmp_path):
    db = tmp_path / "store.db"
    _conn(db).close()
    pa = pa_mod.PatternAnalyzer()
    pa._db = str(db)
    return pa


@pytest.fixture
def alerts(load, monkeypatch, fake_hass):
    """Run predict_routine_start over the rows in `pa`'s store at 11:10."""
    pp = load("person_patterns")
    idm = load("identity")
    jc = load("nova_config")
    monkeypatch.setattr(jc, "get",
                        lambda k, d=None: {"routine_alerts_enabled": True}.get(k, d))
    fake_hass.states.set("person.abi", "home", friendly_name="Abi")
    fake_hass.states.set("light.kitchen_light", "off", friendly_name="Kitchen Light")
    fake_hass.states.set("device_tracker.home_cloud", "home", friendly_name="Home Cloud")

    def run(cog, pa, home=("abi",), minute=10):
        rows = pa.get_person_patterns()
        monkeypatch.setattr(pp, "read", lambda person=None, db_path=None: list(rows))
        monkeypatch.setattr(idm, "_home_people", lambda hass: list(home))
        now = datetime.datetime.now().replace(
            hour=11, minute=minute, second=0, microsecond=0).timestamp()
        return cog.predict_routine_start(fake_hass, now)
    return run


def test_count_variants_are_one_routine_and_one_alert(pa_mod, cog, alerts, tmp_path):
    """A1: the "15 of 23" and "16 of 24" measurements are the same routine."""
    first = _detect(pa_mod, tmp_path, "a.db", "light.kitchen_light", "on", 15, 23)
    second = _detect(pa_mod, tmp_path, "b.db", "light.kitchen_light", "on", 16, 24)
    pa = _analyzer(pa_mod, tmp_path)
    assert pa._store_person_pattern(first)
    assert pa._store_person_pattern(second)
    rows = pa.get_person_patterns()
    assert len(rows) == 1
    assert rows[0]["occurrences"] == second.occurrences
    out = alerts(cog, pa)
    assert len(out) == 1
    assert out[0]["pattern_key"] == "routine:abi:light.kitchen_light|on|11"
    assert alerts(cog, pa, minute=25) == []          # once a day


def test_tracker_time_routine_is_not_a_person_routine(pa_mod, cog, alerts, tmp_path):
    """A2: a device_tracker leaving is not something Abi does at 11:00."""
    p = _detect(pa_mod, tmp_path, "a.db", "device_tracker.home_cloud", "not_home", 15, 23)
    pa = _analyzer(pa_mod, tmp_path)
    pa._store_person_pattern(p)
    assert alerts(cog, pa) == []


def test_spoken_sentence_uses_name_and_you_without_counts(pa_mod, cog, alerts, tmp_path):
    """A3: second person, friendly name, no entity id and no counts."""
    p = _detect(pa_mod, tmp_path, "a.db", "light.kitchen_light", "on", 15, 23)
    pa = _analyzer(pa_mod, tmp_path)
    pa._store_person_pattern(p)
    out = alerts(cog, pa)
    assert [o["message"] for o in out] == [
        "You usually turn the Kitchen Light on around now."]


def test_named_when_someone_else_is_home(pa_mod, cog, alerts, tmp_path):
    p = _detect(pa_mod, tmp_path, "a.db", "light.kitchen_light", "on", 15, 23)
    pa = _analyzer(pa_mod, tmp_path)
    pa._store_person_pattern(p)
    out = alerts(cog, pa, home=("abi", "sam"))
    assert [o["message"] for o in out] == [
        "Abi, you usually turn the Kitchen Light on around now."]


def test_voice_command_routines_are_not_announced(pa_mod, cog, alerts, tmp_path):
    conn = _conn(tmp_path / "c.db")
    for d in range(1, 7):
        _add_command(conn, "play jazz", d, 11, person="abi")
    conn.commit()
    p = pa_mod.PatternAnalyzer()._find_repeated_commands(conn)[0]
    conn.close()
    pa = _analyzer(pa_mod, tmp_path)
    assert pa._store_person_pattern(p)          # still kept for the Memory panel
    assert alerts(cog, pa) == []


# ── One-time cleanup of stored routines ──────────────────────────────────────

# Descriptions exactly as v7.124.1's analyzer wrote them (captured from
# _find_time_routines / _find_repeated_commands at dd2881c); the stored data
# is trimmed to the fields the upgrade reads.
_V1 = [
    ("abi", "time_routine",
     "light.kitchen_light turns on around 11:00 on 15 of 23 days when abi is home",
     '{"hour": 11, "state": "on", "person": "abi"}', 0.65, "2026-09-20T11:00:00", 15),
    ("abi", "time_routine",
     "light.kitchen_light turns on around 11:00 on 16 of 24 days when abi is home",
     '{"hour": 11, "state": "on", "person": "abi"}', 0.67, "2026-09-21T11:00:00", 16),
    ("abi", "time_routine",
     "device_tracker.home_cloud turns not_home around 11:00 on 15 of 23 days when abi is home",
     '{"hour": 11, "state": "not_home", "person": "abi"}', 0.65, "2026-09-21T11:00:00", 15),
    ("abi", "time_routine",
     "sensor.hall_temperature turns 19.5 around 06:00 on 9 of 10 days when abi is home",
     '{"hour": 6, "state": "19.5", "person": "abi"}', 0.8, "2026-09-21T06:00:00", 9),
    ("abi", "repeated_command",
     "abi says 'play jazz' around 21:00 regularly (6 times)",
     '{"command": "play jazz", "hour": 21, "person": "abi"}', 1.0, "2026-09-20T21:00:00", 6),
    ("abi", "repeated_command",
     "abi says 'play jazz' around 21:00 regularly (7 times)",
     '{"command": "play jazz", "hour": 21, "person": "abi"}', 1.0, "2026-09-21T21:00:00", 7),
]


def _v1_store(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE person_patterns (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "person TEXT NOT NULL, pattern_type TEXT NOT NULL, description TEXT NOT NULL, "
        "data TEXT DEFAULT '{}', confidence REAL DEFAULT 0.0, last_seen TEXT, "
        "occurrences INTEGER DEFAULT 1)")
    conn.execute("CREATE TABLE nova_schema_components (component TEXT PRIMARY KEY, "
                 "version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    conn.execute("INSERT INTO nova_schema_components VALUES ('person_patterns', 1, 'x')")
    conn.executemany(
        "INSERT INTO person_patterns (person, pattern_type, description, data, "
        "confidence, last_seen, occurrences) VALUES (?, ?, ?, ?, ?, ?, ?)", _V1)
    conn.commit()
    conn.close()


def _rows(path):
    with sqlite3.connect(path) as conn:
        return sorted(conn.execute(
            "SELECT pattern_type, description, occurrences FROM person_patterns"))


def test_upgrade_collapses_duplicates_and_drops_non_routines_once(load, tmp_path):
    ps = load("persistence.sqlite")
    schema = load("persistence.schema")
    store = next(s for s in schema.STORES if s.key == "patterns")
    db = str(tmp_path / "patterns.db")
    _v1_store(db)
    assert ps.upgrade_store(db, store).ok
    after = _rows(db)
    assert after == [
        ("repeated_command", "abi says 'play jazz' around 21:00 regularly (7 times)", 7),
        ("time_routine",
         "light.kitchen_light turns on around 11:00 on 16 of 24 days when abi is home", 16),
    ]
    assert ps.upgrade_store(db, store).state == "current"
    assert _rows(db) == after


def test_store_after_upgrade_refreshes_the_collapsed_row(load, pa_mod, tmp_path):
    ps = load("persistence.sqlite")
    schema = load("persistence.schema")
    store = next(s for s in schema.STORES if s.key == "patterns")
    db = tmp_path / "patterns.db"
    _v1_store(str(db))
    assert ps.upgrade_store(str(db), store).ok
    pa = pa_mod.PatternAnalyzer()
    pa._db = str(db)
    p = _detect(pa_mod, tmp_path, "a.db", "light.kitchen_light", "on", 17, 25)
    assert pa._store_person_pattern(p)
    rows = [r for r in pa.get_person_patterns() if r["pattern_type"] == "time_routine"]
    assert len(rows) == 1 and rows[0]["occurrences"] == p.occurrences


def test_upgrade_allowlist_matches_the_routine_rule(load):
    """persistence may not import cognitive, so the upgrade carries its own
    copy of the person-routine domains; they must agree."""
    schema = load("persistence.schema")
    routines = load("cognitive.routines")
    mirrored = {d: (None if s is None else frozenset(s))
                for d, s in schema._PERSON_ROUTINE_STATES.items()}
    assert mirrored == routines.ROUTINE_DOMAINS
