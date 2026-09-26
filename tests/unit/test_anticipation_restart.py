"""Anticipation after restart (v7.120.2).

predict_overdue fails uncertain when Nova has not observed since local midnight,
and the once-per-day _RECUR_ALERTED ledger is persisted in patterns.db so a
restart or reload never repeats an anticipation already delivered today.
"""
import ast
import datetime
import json
import pathlib
import sqlite3

import pytest

COMPONENT = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
EID = "binary_sensor.example_morning_activity"
USUAL = 1 * 3600 + 18 * 60          # 01:18


def _at(day_offset=0, hour=3, minute=0):
    base = datetime.datetime.now().replace(hour=hour, minute=minute, second=0,
                                           microsecond=0)
    return (base + datetime.timedelta(days=day_offset)).timestamp()


@pytest.fixture
def cog(load):
    c = load("cognition")
    c.reset()
    yield c
    c.reset()


def _learn_routine(cog, now):
    """Ten prior days of first activity at 01:18, none yet on `now`'s day."""
    today = cog._local_day(now)
    e = cog._Entry(now - 20 * 86400)
    for d in range(today - 10, today):
        e.daily_first.append((d, USUAL))
    e.last_first_day = today - 1
    cog._MODEL[EID] = e


def _hass(fake_hass):
    fake_hass.states.set(EID, "on", friendly_name="Sun Solar rising")
    return fake_hass


def _observed_since_yesterday(cog, now):
    cog._OBSERVING_SINCE = now - 86400


# ── fail uncertain after an observation gap ──────────────────────────────────
def test_restart_after_usual_time_makes_no_none_yet_claim(cog, fake_hass):
    now = _at(hour=3)
    _learn_routine(cog, now)
    cog._OBSERVING_SINCE = _at(hour=2)        # Nova (re)started at 02:00 today
    assert cog.predict_overdue(_hass(fake_hass), now) == []
    assert EID not in cog._RECUR_ALERTED


def test_never_observed_fails_uncertain(cog, fake_hass):
    now = _at(hour=3)
    _learn_routine(cog, now)
    assert cog._OBSERVING_SINCE == 0.0
    assert cog.predict_overdue(_hass(fake_hass), now) == []


def test_first_processed_event_starts_observation_and_gap_clears_it(cog, fake_hass):
    from homeassistant.core import Event, State
    now = _at(hour=3)
    _learn_routine(cog, now)
    cog.process(Event("state_changed", {
        "entity_id": "sensor.x", "old_state": State("sensor.x", "1"),
        "new_state": State("sensor.x", "2")}))
    assert cog._OBSERVING_SINCE > 0            # observation starts now, i.e. today
    assert cog.predict_overdue(_hass(fake_hass), now) == []
    cog.mark_unobserved()
    assert cog._OBSERVING_SINCE == 0.0


def test_reset_clears_observation_window(cog):
    cog._OBSERVING_SINCE = _at(day_offset=-1)
    cog.reset()
    assert cog._OBSERVING_SINCE == 0.0


def test_observer_marks_gaps():
    src = (COMPONENT / "observer.py").read_text()
    stop = src[src.index("async def stop()"):src.index("def is_running()")]
    assert "cognition.mark_unobserved()" in stop
    handler = src[src.index("if _cognition_enabled():"):src.index("if _should_pre_filter(event):")]
    assert "cognition.mark_unobserved()" in handler


# ── continuous observation still anticipates, once per day ───────────────────
def test_continuous_observation_permits_one_alert(cog, fake_hass):
    now = _at(hour=3)
    _learn_routine(cog, now)
    _observed_since_yesterday(cog, now)
    preds = cog.predict_overdue(_hass(fake_hass), now)
    assert len(preds) == 1
    assert preds[0]["type"] == "anticipation_overdue"
    assert "none yet today" in preds[0]["message"]


def test_repeated_cycles_alert_once(cog, fake_hass):
    now = _at(hour=3)
    _learn_routine(cog, now)
    _observed_since_yesterday(cog, now)
    hass = _hass(fake_hass)
    total = sum(len(cog.predict_overdue(hass, now + i * 900)) for i in range(8))
    assert total == 1


def test_activity_today_suppresses(cog, fake_hass):
    now = _at(hour=3)
    _learn_routine(cog, now)
    _observed_since_yesterday(cog, now)
    cog._MODEL[EID].last_first_day = cog._local_day(now)
    assert cog.predict_overdue(_hass(fake_hass), now) == []


def test_next_local_day_may_alert_again(cog, fake_hass):
    now = _at(hour=3)
    _learn_routine(cog, now)
    _observed_since_yesterday(cog, now)
    hass = _hass(fake_hass)
    assert len(cog.predict_overdue(hass, now)) == 1
    tomorrow = _at(day_offset=1, hour=3)
    assert len(cog.predict_overdue(hass, tomorrow)) == 1
    assert cog.predict_overdue(hass, tomorrow + 900) == []


# ── persisted once-per-day ledger ────────────────────────────────────────────
def test_alert_already_emitted_stays_suppressed_after_restart(cog, fake_hass, tmp_path):
    db = str(tmp_path / "patterns.db")
    now = _at(hour=3)
    _learn_routine(cog, now)
    _observed_since_yesterday(cog, now)
    hass = _hass(fake_hass)
    assert len(cog.predict_overdue(hass, now)) == 1
    cog.save_to_db(db, now)

    cog.reset()                                  # restart: all memory lost
    assert cog.load_from_db(db, now) == 1
    assert cog._RECUR_ALERTED[EID] == cog._local_day(now)
    _observed_since_yesterday(cog, now)          # even with full coverage
    assert cog.predict_overdue(hass, now + 900) == []


def test_ledger_bounded_and_old_entries_pruned(cog, tmp_path):
    db = str(tmp_path / "patterns.db")
    now = _at(hour=3)
    today = cog._local_day(now)
    for i in range(cog.RECUR_ALERT_MAX + 200):
        cog._RECUR_ALERTED["depart:event%d" % i] = today
    cog._RECUR_ALERTED["old:a"] = today - 5
    cog._RECUR_ALERTED["yesterday:b"] = today - 1
    cog.save_to_db(db, now)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT key, day FROM cognition_alerted").fetchall()
    assert len(rows) == cog.RECUR_ALERT_MAX
    assert all(day >= today - cog.RECUR_ALERT_KEEP_DAYS + 1 for _k, day in rows)
    assert "old:a" not in cog._RECUR_ALERTED
    assert len(cog._RECUR_ALERTED) <= cog.RECUR_ALERT_MAX


def test_stale_persisted_entries_not_restored(cog, tmp_path):
    db = str(tmp_path / "patterns.db")
    now = _at(hour=3)
    today = cog._local_day(now)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE cognition_alerted (key TEXT PRIMARY KEY, day INTEGER)")
        conn.executemany("INSERT INTO cognition_alerted VALUES (?, ?)",
                         [("fresh", today), ("stale", today - 30)])
    cog.load_from_db(db, now)
    assert cog._RECUR_ALERTED == {"fresh": today}


def test_absent_ledger_fails_safe(cog, tmp_path):
    db = str(tmp_path / "patterns.db")
    with sqlite3.connect(db) as conn:            # pre-v7.120.2 database
        conn.execute("CREATE TABLE cognition_model "
                     "(entity_id TEXT PRIMARY KEY, data TEXT, updated REAL)")
    assert cog.load_from_db(db) == 0
    assert cog._RECUR_ALERTED == {}
    assert cog.load_from_db(str(tmp_path / "missing" / "patterns.db")) == 0


def test_corrupt_ledger_fails_safe(cog, tmp_path):
    db = str(tmp_path / "patterns.db")
    now = _at(hour=3)
    today = cog._local_day(now)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE cognition_alerted (key, day)")
        conn.executemany("INSERT INTO cognition_alerted VALUES (?, ?)",
                         [(42, today), ("bad-day", "today"), (None, None),
                          ("ok", today)])
    cog.load_from_db(db, now)
    assert cog._RECUR_ALERTED == {"ok": today}

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a sqlite database" * 50)
    cog.reset()
    assert cog.load_from_db(str(garbage), now) == 0
    assert cog._RECUR_ALERTED == {}


def test_other_anticipation_types_keep_once_per_day_across_restart(
        cog, load, monkeypatch, fake_hass, tmp_path):
    db = str(tmp_path / "patterns.db")
    pp, jc, idm = load("person_patterns"), load("nova_config"), load("identity")
    now_dt = datetime.datetime.now().replace(minute=5, second=0, microsecond=0)
    now = now_dt.timestamp()
    routine = {"id": 1, "person": "username", "description": "start the coffee",
               "data": json.dumps({"hour": now_dt.hour}), "confidence": 0.8}
    monkeypatch.setattr(pp, "read", lambda person=None, db_path=None: [routine])
    monkeypatch.setattr(idm, "_home_people", lambda hass: ["username"])
    monkeypatch.setattr(idm, "normalize", lambda n: n.strip().lower())
    monkeypatch.setattr(jc, "get", lambda k, d=None: d)

    assert len(cog.predict_routine_start(fake_hass, now)) == 1
    cog._RECUR_ALERTED["dep:person.username"] = cog._local_day(now)
    cog.save_to_db(db, now)
    cog.reset()
    cog.load_from_db(db, now)
    assert cog.predict_routine_start(fake_hass, now + 60) == []
    assert cog._RECUR_ALERTED["dep:person.username"] == cog._local_day(now)


# ── database work stays off the event loop ───────────────────────────────────
def test_patterns_db_io_only_via_executor():
    tree = ast.parse((COMPONENT / "cognitive_core.py").read_text())
    names = {"save_to_db", "load_from_db"}
    executor_args, direct_calls = 0, 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "async_add_executor_job":
            executor_args += sum(isinstance(a, ast.Attribute) and a.attr in names
                                 for a in node.args)
        elif isinstance(fn, ast.Attribute) and fn.attr in names:
            direct_calls += 1
    assert executor_args == 2 and direct_calls == 0

    cog_tree = ast.parse((COMPONENT / "cognition.py").read_text())
    for node in cog_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name not in {"save_to_db", "load_from_db"}:
            assert "sqlite3" not in ast.unparse(node), node.name
