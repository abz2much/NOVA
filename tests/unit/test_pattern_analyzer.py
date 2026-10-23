"""PatternAnalyzer: the routine, command and sequence detectors, the gate on
when an analysis may run, promotion of routines into knowledge, and the
per-person routine store.

Every scenario writes a small history into its own SQLite store. Times are
built from local midnight so a test means the same thing whenever it runs.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime, time as clock, timedelta

import pytest
import asyncio
import sys
import threading
import types
from datetime import datetime, timedelta
import yaml
from fakes import FakeAutomationInventory as FileInventory, FakeHass
import json

# Columns of the history tables, as the logger writes them. Built into DDL
# below so a test can also ask for an older store without the owner column.
_HISTORY_COLUMNS = (
    ("timestamp", "TEXT NOT NULL"), ("entity_id", "TEXT NOT NULL"),
    ("domain", "TEXT NOT NULL"), ("old_state", "TEXT"),
    ("new_state", "TEXT NOT NULL"), ("area_id", "TEXT"), ("hour", "INTEGER"),
    ("day_of_week", "INTEGER"), ("triggered_by", "TEXT DEFAULT 'system'"),
    ("person", "TEXT DEFAULT 'unknown'"),
)
_SPOKEN_COLUMNS = (
    ("timestamp", "TEXT NOT NULL"), ("text", "TEXT NOT NULL"),
    ("handled_by", "TEXT DEFAULT 'agent'"), ("entity_ids", "TEXT DEFAULT '[]'"),
    ("person", "TEXT DEFAULT 'unknown'"), ("hour", "INTEGER"),
    ("day_of_week", "INTEGER"),
)
_OWNED_COLUMNS = (
    ("person", "TEXT NOT NULL"), ("pattern_type", "TEXT NOT NULL"),
    ("description", "TEXT NOT NULL"), ("data", "TEXT DEFAULT '{}'"),
    ("confidence", "REAL DEFAULT 0.0"), ("last_seen", "TEXT"),
    ("occurrences", "INTEGER DEFAULT 1"),
)


def _ddl(table: str, columns) -> str:
    body = ", ".join(f"{name} {kind}" for name, kind in columns)
    return f"CREATE TABLE {table} (id INTEGER PRIMARY KEY AUTOINCREMENT, {body});"


def _at(days_back: int, hour: int, minute: int = 0) -> datetime:
    """Local wall time ``days_back`` days ago at hour:minute."""
    midnight = datetime.combine(date.today(), clock())
    return midnight - timedelta(days=days_back) + timedelta(hours=hour, minutes=minute)


class _History:
    """A pattern store for one test, with writers for changes and commands."""

    def __init__(self, where=":memory:", *, with_person=True, with_commands=True):
        self.path = str(where)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        columns = _HISTORY_COLUMNS if with_person else _HISTORY_COLUMNS[:-1]
        script = [_ddl("state_changes", columns), _ddl("person_patterns", _OWNED_COLUMNS)]
        if with_commands:
            script.append(_ddl("commands", _SPOKEN_COLUMNS))
        self.db.executescript("\n".join(script))

    def change(self, entity, state, days_back, hour, minute=0, *,
               who="unknown", source="system"):
        _add_state(self.db, entity, state, days_back, hour, minute,
                   person=who, triggered_by=source)

    def command(self, text, days_back, hour, *, who="unknown"):
        _add_command(self.db, text, days_back, hour, person=who)

    def saved(self) -> str:
        """Commit, close and hand back the file path (for should_analyze)."""
        self.db.commit()
        self.db.close()
        return self.path


# Plain function writers. test_routine_alerts.py imports these three by name,
# so their names and signatures stay as they are.

def _conn(path):
    """A fresh file store with every history table, rows readable by name."""
    return _History(path).db


def _add_state(conn, entity_id, new_state, days_ago, hour, minute=0,
               person="unknown", triggered_by="system"):
    when = _at(days_ago, hour, minute)
    conn.execute(
        "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, new_state,"
        " area_id, hour, day_of_week, person, triggered_by)"
        " VALUES (?, ?, ?, 'off', ?, '', ?, ?, ?, ?)",
        (when.isoformat(), entity_id, entity_id.partition(".")[0], new_state,
         when.hour, when.weekday(), person, triggered_by))


def _add_command(conn, text, days_ago, hour, person="unknown"):
    when = _at(days_ago, hour)
    conn.execute(
        "INSERT INTO commands (timestamp, text, hour, day_of_week, person)"
        " VALUES (?, ?, ?, ?, ?)",
        (when.isoformat(), text, when.hour, when.weekday(), person))


@pytest.fixture
def mod(load):
    return load("pattern_analyzer")


@pytest.fixture
def facts(load, tmp_path, monkeypatch):
    """The knowledge module, pointed at a private database."""
    knowledge = load("knowledge")
    monkeypatch.setattr(knowledge, "DB_PATH", str(tmp_path / "facts.db"))
    return knowledge


def _routine(mod, confidence, *, hour=18, state="on", entity="light.porch", **extra):
    return mod.DetectedPattern(
        pattern_type="time_routine", description="learned routine",
        entity_ids=[entity], confidence=confidence, occurrences=8,
        details={"hour": hour, "state": state, **extra})


def _only(patterns, **wanted):
    """The single pattern whose attributes match ``wanted``."""
    hits = [p for p in patterns if all(getattr(p, k) == v for k, v in wanted.items())]
    assert len(hits) == 1, f"expected one match for {wanted}, got {patterns!r}"
    return hits[0]


# ── daily routines ──────────────────────────────────────────────────────────

def test_a_light_switched_on_at_six_every_evening_is_a_routine(mod):
    h = _History()
    for back in range(1, 9):
        h.change("light.porch", "on", back, 18)

    routine = _only(mod.PatternAnalyzer()._find_time_routines(h.db),
                    entity_ids=["light.porch"])

    assert routine.pattern_type == "time_routine"
    assert (routine.details["hour"], routine.details["minute"]) == (18, 0)
    assert routine.details["state"] == "on"
    assert routine.occurrences == 8
    assert routine.details["observed_days"] == 8
    assert routine.description == "porch turns on around 18:00 on 8 of 8 days"
    assert "person" not in routine.details


@pytest.mark.parametrize("source", ["automation", "nova_automation"])
def test_changes_an_automation_made_never_teach_a_routine(mod, source):
    # Nova must not learn its own (or Home Assistant's) automations back.
    h = _History()
    for back in range(1, 12):
        h.change("light.porch", "on", back, 18, source=source)

    found = mod.PatternAnalyzer()._find_time_routines(h.db)

    assert [p for p in found if p.entity_ids == ["light.porch"]] == []


def test_one_person_behind_every_occurrence_owns_the_routine(mod):
    h = _History()
    for back in range(1, 9):
        h.change("light.office", "on", back, 7, who="Alex")

    routine = _only(mod.PatternAnalyzer()._find_time_routines(h.db),
                    entity_ids=["light.office"])

    assert routine.details["person"] == "Alex"
    assert "Alex" in routine.description


def test_unattributed_changes_do_not_take_ownership_away(mod):
    # The "unknown" bucket is ignored rather than counted against an owner.
    h = _History()
    for back in range(1, 9):
        h.change("light.office", "on", back, 7, who="Alex" if back <= 6 else "unknown")

    routine = _only(mod.PatternAnalyzer()._find_time_routines(h.db),
                    entity_ids=["light.office"])

    assert routine.details["person"] == "Alex"


def test_an_even_split_between_two_people_stays_with_the_household(mod):
    h = _History()
    for back in range(1, 9):
        h.change("light.hall", "on", back, 20, who="Alex" if back < 5 else "Sam")

    routine = _only(mod.PatternAnalyzer()._find_time_routines(h.db),
                    entity_ids=["light.hall"])

    assert "person" not in routine.details


def test_owner_lookup_on_a_store_without_the_person_column_is_household(mod):
    # A store from before people were recorded: no crash, just no owner.
    h = _History(with_person=False)
    for back in range(1, 9):
        h.db.execute(
            "INSERT INTO state_changes (timestamp, entity_id, domain, new_state, hour)"
            " VALUES (?, 'light.x', 'light', 'on', 8)", (_at(back, 8).isoformat(),))

    owner = mod.PatternAnalyzer()._dominant_person(
        h.db, "state_changes", entity="light.x", state="on", hour=8)

    assert owner is None


# ── repeated commands ───────────────────────────────────────────────────────

def test_a_phrase_said_nightly_is_a_repeated_command(mod):
    h = _History()
    for back in range(1, 7):
        h.command("goodnight", back, 23)

    cmd = _only(mod.PatternAnalyzer()._find_repeated_commands(h.db),
                pattern_type="repeated_command")

    assert cmd.details == {"command": "goodnight", "hour": 23}
    assert cmd.occurrences == 6
    assert cmd.confidence == pytest.approx(1.0)
    assert cmd.description == "'goodnight' is said around 23:00 regularly (6 times)"


def test_a_phrase_said_too_few_times_is_not_a_pattern(mod):
    h = _History()
    for back in range(1, mod.MIN_OCCURRENCES):   # one short of the bar
        h.command("goodnight", back, 23)

    assert mod.PatternAnalyzer()._find_repeated_commands(h.db) == []


def test_a_store_without_a_commands_table_gives_no_commands(mod):
    h = _History(with_commands=False)

    assert mod.PatternAnalyzer()._find_repeated_commands(h.db) == []


def test_a_command_one_person_always_says_is_theirs(mod):
    h = _History()
    for back in range(1, 7):
        h.command("play jazz", back, 21, who="Alex")

    found = mod.PatternAnalyzer()._find_repeated_commands(h.db)
    cmd = next(p for p in found if p.details["command"] == "play jazz")

    assert cmd.details["person"] == "Alex"
    assert cmd.description.startswith("Alex says 'play jazz' around 21:00")


# ── sequences ───────────────────────────────────────────────────────────────

def test_one_light_reliably_following_another_is_a_sequence(mod):
    h = _History()
    for back in range(1, 7):
        h.change("light.lamp_one", "on", back, 18, 0)
        h.change("light.lamp_two", "on", back, 18, 2)

    seq = _only(mod.PatternAnalyzer()._find_sequence_patterns(h.db),
                pattern_type="sequence")

    assert seq.entity_ids == ["light.lamp_one", "light.lamp_two"]
    assert seq.details["delay_seconds"] == 120
    assert seq.occurrences == 6


def test_an_automation_chain_is_not_learned_as_a_sequence(mod):
    h = _History()
    for back in range(1, 8):
        h.change("switch.trigger", "on", back, 18, 0, source="automation")
        h.change("light.result", "on", back, 18, 1, source="automation")

    assert mod.PatternAnalyzer()._find_sequence_patterns(h.db) == []


def test_a_store_without_history_gives_no_sequences(mod):
    empty = sqlite3.connect(":memory:")
    empty.row_factory = sqlite3.Row

    assert mod.PatternAnalyzer()._find_sequence_patterns(empty) == []


# ── when an analysis may run ────────────────────────────────────────────────

def _analyzer_on(mod, path, *, last_run=0.0):
    analyzer = mod.PatternAnalyzer()
    analyzer._db = path
    analyzer._last_analysis = last_run
    return analyzer


def _week_and_more(tmp_path, *, per_day=8, source="system"):
    """Nine days (the oldest eight days back) with ``per_day`` changes each."""
    h = _History(tmp_path / "history.db")
    for back in range(9):
        for slot in range(per_day):
            h.change("light.x", "on" if slot % 2 else "off", back, 6 + slot, source=source)
    return h.saved()


def test_two_days_of_history_is_too_little(mod, tmp_path):
    h = _History(tmp_path / "young.db")
    for back in range(2):
        h.change("light.x", "on", back, 18)

    assert _analyzer_on(mod, h.saved()).should_analyze() is False


def test_no_store_on_disk_means_no_analysis(mod, tmp_path):
    assert _analyzer_on(mod, str(tmp_path / "absent.db")).should_analyze() is False


def test_enough_days_and_changes_allow_an_analysis(mod, tmp_path):
    assert _analyzer_on(mod, _week_and_more(tmp_path)).should_analyze() is True


def test_enough_days_but_under_fifty_changes_is_too_little(mod, tmp_path):
    # Nine days of five changes is 45, below the 50 change floor.
    assert _analyzer_on(mod, _week_and_more(tmp_path, per_day=5)).should_analyze() is False


def test_changes_automations_made_do_not_count_towards_the_floor(mod, tmp_path):
    path = _week_and_more(tmp_path, source="automation")

    assert _analyzer_on(mod, path).should_analyze() is False


@pytest.mark.parametrize("seconds_ago, allowed", [
    (0, False),             # just ran
    (60, False),            # still inside the six hour gap
    (None, True),           # one second past the gap
])
def test_the_interval_between_scheduled_runs(mod, tmp_path, seconds_ago, allowed):
    gap = mod.ANALYSIS_INTERVAL
    ago = gap + 1 if seconds_ago is None else seconds_ago
    analyzer = _analyzer_on(mod, _week_and_more(tmp_path), last_run=time.time() - ago)

    assert analyzer.should_analyze() is allowed


def test_clearing_the_last_run_lets_a_manual_run_through(mod, tmp_path):
    analyzer = _analyzer_on(mod, _week_and_more(tmp_path), last_run=time.time())
    assert analyzer.should_analyze() is False

    analyzer._last_analysis = 0.0     # what Analyze Now does

    assert analyzer.should_analyze() is True


def test_a_manual_run_still_needs_enough_history(mod, tmp_path):
    # Skipping the interval must not skip the data floor on a fresh install.
    h = _History(tmp_path / "fresh.db")
    for back in range(2):
        h.change("light.x", "on", back, 18)

    assert _analyzer_on(mod, h.saved(), last_run=0.0).should_analyze() is False


# ── knowledge promotion ─────────────────────────────────────────────────────

def test_a_confident_routine_becomes_one_observed_fact(mod, facts):
    written = mod.PatternAnalyzer()._promote_to_knowledge([_routine(mod, 0.9)])

    assert written == 1
    (fact,) = facts.all_facts()
    assert fact["source"] == "observed"
    assert fact["subject"] == "household"
    assert "18:00" in fact["value"]


def test_a_learned_fact_stays_pending_until_someone_confirms_it(mod, facts):
    # Learned facts never reach the model until a person confirms them.
    mod.PatternAnalyzer()._promote_to_knowledge([_routine(mod, 0.9)])

    (fact,) = facts.all_facts()
    assert fact["status"] == "pending"
    assert facts.all_facts(status="confirmed") == []
    assert "18:00" not in (facts.prompt_block() or "")


def test_seeing_a_confirmed_fact_again_only_reopens_it_when_it_changes(mod, facts):
    analyzer = mod.PatternAnalyzer()
    analyzer._promote_to_knowledge([_routine(mod, 0.9)])
    facts.confirm_fact(facts.all_facts()[0]["id"])

    analyzer._promote_to_knowledge([_routine(mod, 0.9)])
    assert facts.all_facts()[0]["status"] == "confirmed"

    analyzer._promote_to_knowledge([_routine(mod, 0.9, hour=19)])
    assert facts.all_facts()[0]["status"] == "pending"


@pytest.mark.parametrize("confidence, expected", [
    (0.5, 0),
    (0.74, 0),
    (0.75, 1),     # exactly KNOWLEDGE_FACT_CONFIDENCE is enough
])
def test_only_routines_at_or_above_the_fact_bar_are_promoted(mod, facts, confidence,
                                                             expected):
    assert mod.KNOWLEDGE_FACT_CONFIDENCE == 0.75
    assert mod.PatternAnalyzer()._promote_to_knowledge([_routine(mod, confidence)]) == expected
    assert len(facts.all_facts()) == expected


def test_something_a_person_said_is_never_overwritten_by_observation(mod, facts):
    analyzer = mod.PatternAnalyzer()
    pattern = _routine(mod, 0.9)
    subject, _kind, key, _value = analyzer._fact_for(pattern)
    facts.remember(key, "I handle this myself", subject=subject, source="stated")

    analyzer._promote_to_knowledge([pattern])

    (fact,) = facts.all_facts()
    assert (fact["value"], fact["source"]) == ("I handle this myself", "stated")


def test_an_observed_fact_moves_with_the_routine(mod, facts):
    analyzer = mod.PatternAnalyzer()
    analyzer._promote_to_knowledge([_routine(mod, 0.8, hour=18)])
    analyzer._promote_to_knowledge([_routine(mod, 0.9, hour=19)])

    (fact,) = facts.all_facts()     # updated in place, not added twice
    assert "19:00" in fact["value"]


def test_an_owned_routine_is_filed_under_its_owner(mod, facts):
    analyzer = mod.PatternAnalyzer()
    pattern = _routine(mod, 0.9, person="Alex")

    assert analyzer._fact_for(pattern)[0] == "alex"
    assert analyzer._promote_to_knowledge([pattern]) == 1
    assert facts.all_facts()[0]["subject"] == "alex"


def test_fact_wording_for_routines_commands_and_sequences(mod):
    analyzer = mod.PatternAnalyzer()
    said = mod.DetectedPattern(
        pattern_type="repeated_command", description="said", entity_ids=[],
        confidence=0.9, occurrences=6, details={"command": "goodnight", "hour": 23})
    chain = mod.DetectedPattern(
        pattern_type="sequence", description="chain", entity_ids=["light.a", "light.b"],
        confidence=0.9, occurrences=6, details={})

    assert analyzer._fact_for(_routine(mod, 0.9)) == (
        "household", "fact", "porch turns on", "around 18:00 most days")
    assert analyzer._fact_for(said) == (
        "household", "fact", 'asks "goodnight"', "usually around 23:00")
    assert analyzer._fact_for(chain) is None


# ── the per-person routine store ────────────────────────────────────────────

def test_storing_an_owned_routine_twice_updates_one_row(mod, tmp_path):
    analyzer = _analyzer_on(mod, _History(tmp_path / "owned.db").saved())

    def morning_light(confidence, seen):
        return mod.DetectedPattern(
            pattern_type="time_routine", description="Alex's morning light",
            entity_ids=["light.office"], confidence=confidence, occurrences=seen,
            details={"hour": 7, "state": "on", "person": "Alex"})

    assert analyzer._store_person_pattern(morning_light(0.8, 8)) is True
    first = analyzer.get_person_patterns("alex")
    assert [row["person"] for row in first] == ["alex"]

    analyzer._store_person_pattern(morning_light(0.95, 10))
    (row,) = analyzer.get_person_patterns("alex")
    assert row["occurrences"] == 10


def test_a_household_routine_is_not_stored_against_anyone(mod, tmp_path):
    analyzer = _analyzer_on(mod, _History(tmp_path / "owned.db").saved())

    assert analyzer._store_person_pattern(_routine(mod, 0.9)) is False
    assert analyzer.get_person_patterns() == []


def test_a_sensor_reading_is_never_a_persons_routine(mod, tmp_path):
    # Even with an owner, a sensor changing is not something a person does.
    analyzer = _analyzer_on(mod, _History(tmp_path / "owned.db").saved())
    reading = _routine(mod, 0.9, entity="sensor.hall_temperature", person="Alex")

    assert analyzer._store_person_pattern(reading) is False
    assert analyzer.get_person_patterns() == []


# ── analysis runs off the event loop, one at a time ───────────────────────────

class ThreadedHass(FakeHass):
    """Runs executor jobs on a real thread pool, like Home Assistant."""

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)


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


def _history_db(tmp_path, days=12):
    db = tmp_path / "patterns.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    base = datetime.now() - timedelta(days=days + 2)
    for d in range(days):
        when = (base + timedelta(days=d)).replace(hour=18, minute=0, second=0)
        conn.execute(
            "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, "
            "new_state, area_id, hour, day_of_week) VALUES (?,?,?,?,?,?,?,?)",
            (when.isoformat(), "light.porch", "light", "off", "on", "", 18,
             when.weekday()))
    conn.commit()
    conn.close()
    return str(db)


def _quiet_analyzer(patterns, db, monkeypatch, stored):
    an = patterns.PatternAnalyzer()
    an._db = db
    monkeypatch.setattr(an, "_store_suggestion", lambda p: (stored.append(p), True)[1])
    monkeypatch.setattr(an, "_store_person_pattern", lambda p: False)
    monkeypatch.setattr(an, "_promote_to_knowledge", lambda pats: 0)
    monkeypatch.setattr(patterns, "_learned_threshold_delta", lambda: 0.0)
    return an


async def test_analysis_finds_patterns_with_a_real_executor(load, tmp_path, monkeypatch):
    patterns = load("automation.patterns")
    stored = []
    an = _quiet_analyzer(patterns, _history_db(tmp_path), monkeypatch, stored)
    found = await an.analyze(ThreadedHass())
    assert any(p.pattern_type == "time_routine" for p in found)
    assert stored, "a real thread pool must still store the routine"
    assert an._last_result["patterns_found"] == len(found)


async def test_no_sqlite_connection_is_opened_on_the_event_loop(
        load, tmp_path, monkeypatch):
    patterns = load("automation.patterns")
    stored = []
    an = _quiet_analyzer(patterns, _history_db(tmp_path), monkeypatch, stored)
    loop_thread = threading.get_ident()
    opened_on = []
    real_connect = sqlite3.connect

    def spy(*a, **k):
        opened_on.append(threading.get_ident())
        return real_connect(*a, **k)

    monkeypatch.setattr(patterns.sqlite3, "connect", spy)
    await an.analyze(ThreadedHass())
    assert opened_on and loop_thread not in opened_on


async def test_manual_and_scheduled_analysis_never_overlap(load, monkeypatch):
    patterns = load("automation.patterns")
    an = patterns.PatternAnalyzer()
    running = {"now": 0, "max": 0, "runs": 0}

    async def slow_once(hass):
        running["now"] += 1
        running["runs"] += 1
        running["max"] = max(running["max"], running["now"])
        await asyncio.sleep(0.02)
        running["now"] -= 1
        return ["pattern"]

    monkeypatch.setattr(an, "_analyze_once", slow_once)
    first, second = await asyncio.gather(an.analyze(None), an.analyze(None))
    assert running["max"] == 1 and running["runs"] == 1
    assert first == second == ["pattern"]
    assert not an.analysis_running


# ── routine times, sequences and the cap ──────────────────────────────────────

_SCHEMA_rl = """
CREATE TABLE state_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, entity_id TEXT NOT NULL, domain TEXT NOT NULL,
    old_state TEXT, new_state TEXT NOT NULL, area_id TEXT,
    hour INTEGER, day_of_week INTEGER, triggered_by TEXT DEFAULT 'system',
    person TEXT DEFAULT 'unknown'
);
CREATE TABLE commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, text TEXT NOT NULL,
    handled_by TEXT DEFAULT 'agent', entity_ids TEXT DEFAULT '[]',
    person TEXT DEFAULT 'unknown', hour INTEGER, day_of_week INTEGER
);
"""


@pytest.fixture
def pa(load):
    return load("automation.patterns")


@pytest.fixture
def sugg(load):
    return load("automation.suggestions")


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.executescript(_SCHEMA_rl)
    db.row_factory = sqlite3.Row
    yield db
    db.close()


def _at_rl(days_ago: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return (datetime.now() - timedelta(days=days_ago)).replace(
        hour=hour, minute=minute, second=second, microsecond=0)


def _change(conn, entity, state, when: datetime):
    conn.execute(
        "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, new_state, "
        "area_id, hour, day_of_week) VALUES (?,?,?,?,?,?,?,?)",
        (when.isoformat(), entity, entity.split(".")[0], "off", state, "", when.hour,
         when.weekday()))


def _said(conn, text, when: datetime):
    conn.execute("INSERT INTO commands (timestamp, text, hour, day_of_week) VALUES (?,?,?,?)",
                 (when.isoformat(), text, when.hour, when.weekday()))


def test_a_routine_runs_at_its_usual_minute(pa, sugg, conn):
    for day in range(1, 13):
        _change(conn, "light.landing", "off", _at_rl(day, 22, 45))
    [routine] = pa.PatternAnalyzer()._find_time_routines(conn)
    assert (routine.details["hour"], routine.details["minute"]) == (22, 45)
    assert "around 22:45" in routine.description
    auto = json.loads(sugg.generate_automation(routine))
    assert auto["trigger"] == {"platform": "time", "at": "22:45:00"}
    assert auto["alias"].endswith("off at 22:45")


def test_the_usual_minute_is_the_median_rounded_to_five(pa, conn):
    for day, minute in zip(range(1, 10), (38, 40, 41, 42, 44, 45, 47, 50, 52)):
        _change(conn, "light.landing", "off", _at_rl(day, 22, minute))
    [routine] = pa.PatternAnalyzer()._find_time_routines(conn)
    assert (routine.details["hour"], routine.details["minute"]) == (22, 45)


def test_an_old_suggestion_without_a_minute_still_reads_on_the_hour(pa, sugg, load):
    models = load("automation.models")
    old = models.DetectedPattern(
        pattern_type="time_routine", description="x", entity_ids=["light.a"],
        confidence=0.9, occurrences=9, details={"hour": 7, "state": "on"})
    assert json.loads(sugg.generate_automation(old))["trigger"]["at"] == "07:00:00"


def test_a_routine_across_the_hour_is_one_routine(pa, conn):
    for day in range(1, 13):
        _change(conn, "light.porch", "off", _at_rl(day, 22, 55) if day % 2 else _at_rl(day, 23, 5))
    found = pa.PatternAnalyzer()._find_time_routines(conn)
    assert len(found) == 1
    [routine] = found
    assert routine.details["observed_days"] == 12
    assert (routine.details["hour"], routine.details["minute"]) == (23, 0)


def test_a_routine_across_midnight_keeps_its_time(pa, conn):
    for day in range(1, 13):
        _change(conn, "lock.back_door", "locked",
                _at_rl(day, 23, 55) if day % 2 else _at_rl(day, 0, 5))
    [routine] = pa.PatternAnalyzer()._find_time_routines(conn)
    assert routine.details["observed_days"] == 12
    assert (routine.details["hour"], routine.details["minute"]) in ((23, 55), (0, 0), (0, 5))


def test_two_routines_for_one_device_are_both_found(pa, conn):
    for day in range(1, 13):
        _change(conn, "light.hall", "on", _at_rl(day, 7, 0))
        _change(conn, "light.hall", "on", _at_rl(day, 19, 0))
    found = pa.PatternAnalyzer()._find_time_routines(conn)
    assert sorted(p.details["hour"] for p in found) == [7, 19]


def test_the_cap_keeps_the_most_reliable_routines(pa, conn):
    # Twenty busy but patchy routines (three times a day, half the days)
    # and one reliable one (once a day, every day).
    for n in range(20):
        for day in range(2, 14, 2):
            for minute in (0, 10, 20):
                _change(conn, f"switch.busy_{n:02d}", "on", _at_rl(day, 9, minute))
    for day in range(1, 14):
        _change(conn, "light.reliable", "on", _at_rl(day, 18, 0))
    found = pa.PatternAnalyzer()._find_time_routines(conn)
    assert len(found) == 20
    assert found[0].entity_ids == ["light.reliable"]
    assert [p.confidence for p in found] == sorted((p.confidence for p in found), reverse=True)


def test_a_command_is_scored_against_the_same_30_days(pa, conn):
    for day in range(1, 6):
        _said(conn, "good morning", _at_rl(day, 8))
    for day in range(60, 80):           # said a lot, but long ago
        _said(conn, "good morning", _at_rl(day, 8))
    [command] = pa.PatternAnalyzer()._find_repeated_commands(conn)
    assert command.occurrences == 5
    assert command.confidence == pytest.approx(1.0)


def test_a_sequence_waits_the_typical_gap(pa, conn):
    gaps = [30, 30, 32, 28, 30, 31, 29, 540]     # one slow evening
    for day, gap in zip(range(1, 9), gaps):
        start = _at_rl(day, 20, 0)
        _change(conn, "binary_sensor.front_door", "on", start)
        _change(conn, "light.hall", "on", start + timedelta(seconds=gap))
    found = [p for p in pa.PatternAnalyzer()._find_sequence_patterns(conn)
             if p.entity_ids == ["binary_sensor.front_door", "light.hall"]]
    assert [p.details["delay_seconds"] for p in found] == [30]
    assert "~30s later" in found[0].description
