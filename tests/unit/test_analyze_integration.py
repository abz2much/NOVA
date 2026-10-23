"""PatternAnalyzer.analyze(hass) run end to end over a small real store.

The detectors have their own unit tests. These drive the whole pass with a
fake hass so a wiring fault shows up: a detector left out of the pass, the
sensor history no longer reaching the detectors that need it, or one failing
detector losing what the others found. Storage is replaced by in-memory
lists, so the checks are on what the pass produces, not on persistence.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time as clock, timedelta

import pytest

_SCHEMA = {
    "state_changes": ("timestamp TEXT NOT NULL", "entity_id TEXT NOT NULL",
                      "domain TEXT NOT NULL", "old_state TEXT", "new_state TEXT NOT NULL",
                      "area_id TEXT", "hour INTEGER", "day_of_week INTEGER",
                      "triggered_by TEXT DEFAULT 'system'", "person TEXT DEFAULT 'unknown'"),
    "commands": ("timestamp TEXT", "command TEXT", "entity_id TEXT", "hour INTEGER",
                 "day_of_week INTEGER", "person TEXT DEFAULT 'unknown'"),
    "person_patterns": ("person TEXT", "pattern_type TEXT", "description TEXT",
                        "entity_ids TEXT", "confidence REAL", "details TEXT",
                        "created TEXT", "last_seen TEXT"),
}

_DAYS = 12          # twelve repeats: 12 / 15 = 0.8, over the 0.65 bar


@pytest.fixture
def patterns(load, monkeypatch):
    load("pattern_analyzer")
    mod = load("automation.patterns")
    monkeypatch.setattr(mod, "MIN_OCCURRENCES", 5)
    monkeypatch.setattr(mod, "CONFIDENCE_THRESHOLD", 0.65)
    monkeypatch.setattr(mod, "_learned_threshold_delta", lambda: 0.0)
    return mod


def _evening(back: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(date.today() - timedelta(days=back), clock(hour, minute, second))


def _write(path, changes) -> str:
    """A store holding `changes`, each (entity_id, state, when)."""
    with sqlite3.connect(str(path)) as conn:
        for table, columns in _SCHEMA.items():
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, {', '.join(columns)})")
        conn.executemany(
            "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, new_state, "
            "area_id, hour, day_of_week) VALUES (?, ?, ?, 'off', ?, '', ?, ?)",
            [(when.isoformat(), eid, eid.split(".")[0], state, when.hour, when.weekday())
             for eid, state, when in changes])
    return str(path)


def _porch_each_evening():
    return [("light.porch", "on", _evening(back, 18)) for back in range(_DAYS + 2, 2, -1)]


def _hall_then_kitchen():
    out = []
    for back in range(_DAYS + 2, 2, -1):
        start = _evening(back, 20, 0, 17)
        out += [("light.hall", "on", start),
                ("light.kitchen", "on", start + timedelta(seconds=45))]
    return out


class _Run:
    """One analyzer on a store, with every store write kept in `filed`."""

    def __init__(self, patterns, db, monkeypatch):
        self.analyzer = patterns.PatternAnalyzer()
        self.analyzer._db = db
        self.filed: list = []

        def keep(pattern):
            self.filed.append(pattern)
            return True
        monkeypatch.setattr(self.analyzer, "_store_suggestion", keep)
        monkeypatch.setattr(self.analyzer, "_store_person_pattern", lambda pattern: False)
        monkeypatch.setattr(self.analyzer, "_promote_to_knowledge", lambda found: 0)

    async def go(self, hass):
        return await self.analyzer.analyze(hass)

    def of_type(self, kind):
        return [p for p in self.filed if p.pattern_type == kind]


async def test_one_pass_finds_both_a_daily_routine_and_a_sequence(
        patterns, tmp_path, monkeypatch, fake_hass):
    db = _write(tmp_path / "both.db", _porch_each_evening() + _hall_then_kitchen())
    run = _Run(patterns, db, monkeypatch)
    await run.go(fake_hass)

    kinds = {p.pattern_type for p in run.filed}
    assert "time_routine" in kinds, f"the time detector did not run; got {kinds}"
    assert "sequence" in kinds, f"the sequence detector did not run; got {kinds}"
    porch = [p for p in run.of_type("time_routine") if p.entity_ids == ["light.porch"]]
    assert porch and porch[0].details["hour"] == 18
    assert ["light.hall", "light.kitchen"] in [p.entity_ids for p in run.of_type("sequence")]
    assert run.analyzer._last_result["new_suggestions"] == len(run.filed)


async def test_with_no_recorder_the_sequence_detector_still_works(
        patterns, tmp_path, monkeypatch, fake_hass):
    # The fake hass has no recorder, so the sensor history comes back empty
    # and the sequences must still be found with it.
    db = _write(tmp_path / "plain.db", _hall_then_kitchen())
    run = _Run(patterns, db, monkeypatch)
    await run.go(fake_hass)
    assert run.of_type("sequence")
    # Without sensor history the numeric detector did not really run, so its
    # old suggestions are not retired.
    assert run.analyzer._numeric_ran is False
    assert "numeric_trigger" not in run.analyzer._completed_types
    assert {"time_routine", "repeated_command", "sequence"} <= run.analyzer._completed_types


async def test_a_pattern_an_automation_already_covers_is_counted_not_filed(
        patterns, tmp_path, monkeypatch, fake_hass):
    db = _write(tmp_path / "covered.db", _porch_each_evening())
    run = _Run(patterns, db, monkeypatch)
    verdict = {"status": "already_automated",
               "matches": [{"entity_id": "automation.porch", "name": "Porch"}],
               "reason": "equivalent"}
    monkeypatch.setattr(run.analyzer, "_automation_match", lambda hass, pattern: verdict)
    found = await run.go(fake_hass)

    assert run.filed == []
    result = run.analyzer._last_result
    assert result["already_automated"] >= 1
    assert result["already_automated"] == len(found)
    assert result["new_suggestions"] == 0
    assert found[0].details["automation_match"] is verdict


async def test_a_failing_detector_keeps_what_the_earlier_ones_found(
        patterns, tmp_path, monkeypatch, fake_hass):
    db = _write(tmp_path / "broken.db", _porch_each_evening() + _hall_then_kitchen())
    run = _Run(patterns, db, monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError("detector fault")
    monkeypatch.setattr(run.analyzer, "_find_sequence_patterns", broken)
    await run.go(fake_hass)

    assert run.of_type("time_routine")
    assert run.of_type("sequence") == []
    # Only finished detectors may retire their old suggestions.
    assert "time_routine" in run.analyzer._completed_types
    assert "sequence" not in run.analyzer._completed_types


async def test_no_store_on_disk_is_a_quiet_empty_pass(patterns, tmp_path, monkeypatch, fake_hass):
    run = _Run(patterns, str(tmp_path / "never-made.db"), monkeypatch)
    assert await run.go(fake_hass) == []
    assert run.filed == []


# ── the numeric detector, fed by the fetched sensor history ─────────────────

def _heater_mornings(tmp_path):
    """A heater on at 06:02:17 on twelve mornings, and a temperature series
    sampled every fifteen minutes that dips to 61 two minutes before each.
    Built from midnight so no sample lands between a dip and its heater-on."""
    first = datetime.combine(date.today() - timedelta(days=16), clock(0, 0))
    switched = [first + timedelta(days=d, hours=6, minutes=2, seconds=17) for d in range(_DAYS)]
    db = _write(tmp_path / "heater.db", [("switch.space_heater", "on", t) for t in switched])
    start = first.timestamp()
    series = [(start + n * 900, 72.0 + (n % 5)) for n in range(17 * 96)]
    # A real crossing shortly before the action (needed since v7.126.0).
    series += [(t.timestamp() - 120, 61.0) for t in switched]
    return db, series


def _feed(monkeypatch, analyzer, series, areas):
    async def history(hass):
        return {"sensor.living_room_temperature": series}

    async def where(hass, entity_ids):
        return dict(areas)
    monkeypatch.setattr(analyzer, "_fetch_numeric_sensor_history", history)
    monkeypatch.setattr(analyzer, "_fetch_entity_areas", where)


async def test_the_numeric_detector_learns_heat_when_cold_in_the_same_room(
        patterns, tmp_path, monkeypatch, fake_hass):
    db, series = _heater_mornings(tmp_path)
    run = _Run(patterns, db, monkeypatch)
    _feed(monkeypatch, run.analyzer, series, {
        "sensor.living_room_temperature": "living_room",
        "switch.space_heater": "living_room"})
    await run.go(fake_hass)

    learned = run.of_type("numeric_trigger")
    assert learned, "the numeric detector is not part of analyze()"
    details = learned[0].details
    assert details["op"] == "below"
    assert details["action"]["entity"] == "switch.space_heater"
    assert details["action"]["state"] == "on"
    assert details["trigger_sensor"] == "sensor.living_room_temperature"
    assert details["followed"] == _DAYS
    assert "numeric_trigger" in run.analyzer._completed_types


async def test_the_numeric_detector_ignores_a_sensor_in_another_room(
        patterns, tmp_path, monkeypatch, fake_hass):
    db, series = _heater_mornings(tmp_path)
    run = _Run(patterns, db, monkeypatch)
    _feed(monkeypatch, run.analyzer, series, {
        "sensor.living_room_temperature": "living_room",
        "switch.space_heater": "bedroom"})
    await run.go(fake_hass)
    assert run.of_type("numeric_trigger") == []
