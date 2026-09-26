"""Characterization of entity search and time-routine scoring, pinned before
Phase 8 changed either.

search_entities: every query in QUERIES is run over one synthetic home and
the returned entity_ids (in order) or ambiguity candidates are pinned. The
Phase 8 resolver may change a pinned case only on purpose; such a case is
listed in INTENDED_CHANGES with its reason, and everything else must stay
exactly as it was.

Time routines: confidence, coverage and day counts from
PatternAnalyzer._find_time_routines over synthetic histories are pinned, so
the scoring rework can prove which numbers it kept.

Regenerate after reviewing the diff:
    NOVA_WRITE_COGNITIVE_GOLDEN=1 python -m pytest tests/unit/test_entity_pattern_characterization.py

Focused run:
    python -m pytest tests/unit/test_entity_pattern_characterization.py -q
"""
import json
import os
import pathlib
import sqlite3
from datetime import datetime, timedelta

import pytest

from fakes import FakeHass

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "cognitive"
_WRITE = os.environ.get("NOVA_WRITE_COGNITIVE_GOLDEN") == "1"

HOME = [
    ("light.kitchen_ceiling", "on", "Kitchen Ceiling"),
    ("light.porch", "on", "Porch Light"),
    ("light.hall", "off", "Hall Light"),
    ("light.office", "off", "Office"),
    ("light.office_desk", "off", "Office Desk"),
    ("light.bedroom_lamp_left", "off", "Bedroom Lamp Left"),
    ("light.bedroom_lamp_right", "off", "Bedroom Lamp Right"),
    ("sensor.kitchen_light_current", "0.42", "Kitchen Light Current"),
    ("sensor.porch_light_current", "0.10", "Porch Light Current"),
    ("sensor.kitchen_humidity", "40", "Kitchen Humidity Sensor"),
    ("switch.current_monitor", "on", "Current Monitor"),
    ("switch.garden_lights", "off", "Garden Lights"),
    ("binary_sensor.light_current_fault", "on", "Light Current Fault"),
    ("lock.front_door", "locked", "Front Door"),
    ("lock.back_door", "unlocked", "Back Door"),
    ("cover.garage_door", "closed", "Garage Door"),
    ("climate.hallway", "heat", "Hallway Thermostat"),
    ("fan.bedroom", "off", "Bedroom Fan"),
    ("person.alex", "home", "Alex"),
]

# (query, domain, require_unique)
QUERIES = [
    ("lights", None, False), ("lights", "light", False), ("light", None, False),
    ("porch light", None, False), ("porch light", None, True), ("porch", None, True),
    ("kitchen ceiling", None, True), ("kitchen light", None, False),
    ("kitchen light", "light", True), ("hall light", "light", True),
    ("hall", None, False), ("office light", None, True), ("office", None, True),
    ("bedroom lamp", None, True), ("bedroom", None, False), ("front door", None, True),
    ("front door", "lock", True), ("doors", None, False), ("locks", None, False),
    ("garage", None, True), ("thermostat", None, False), ("fan", None, False),
    ("alex", None, True), ("current", None, False), ("garden lights", None, True),
    ("light.porch", None, True), ("porch_light", None, True), ("xyzzy", None, False),
    ("kitchen lights", None, False), ("lights on", None, False),
]

INTENDED_CHANGES: dict = {}


def _home():
    hass = FakeHass()
    for eid, state, name in HOME:
        hass.states.set(eid, state, friendly_name=name)
    return hass


def _shape(out):
    if isinstance(out, dict) and out.get("ambiguous"):
        return {"ambiguous": sorted(c["entity_id"] for c in out["candidates"])}
    return [r["entity_id"] for r in out]


def _golden(name, current):
    path = GOLDEN / f"{name}.json"
    if _WRITE:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{json.dumps(k)}: {json.dumps(current[k], sort_keys=True)}"
                 for k in sorted(current)]
        path.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


async def test_search_entities_matches_the_characterization(load, monkeypatch):
    agent = load("agent")
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})
    hass = _home()
    current = {}
    for q, dom, uniq in QUERIES:
        args = {"query": q, "require_unique": uniq}
        if dom:
            args["domain"] = dom
        out = json.loads(await agent._exec_search_entities(hass, args))
        current[f"{q}|{dom}|{int(uniq)}"] = _shape(out)
    pinned = _golden("search_entities", current)
    assert sorted(pinned) == sorted(current)
    drift = {k: {"pinned": pinned[k], "now": current[k]}
             for k in sorted(current) if pinned[k] != current[k]}
    unexpected = {k: v for k, v in drift.items() if k not in INTENDED_CHANGES}
    assert unexpected == {}, json.dumps(unexpected, indent=1)


# ── Time routines ───────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE state_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
  entity_id TEXT NOT NULL, domain TEXT NOT NULL, old_state TEXT, new_state TEXT NOT NULL,
  area_id TEXT, hour INTEGER, day_of_week INTEGER, triggered_by TEXT DEFAULT 'system',
  person TEXT DEFAULT 'unknown');
"""

# name -> list of (entity_id, state, [(days_ago, hour, minute)...], triggered_by)
HISTORIES = {
    "daily_8": [("light.porch", "on", [(d, 18, 5) for d in range(1, 9)], "system")],
    "daily_20": [("light.porch", "on", [(d, 18, 5) for d in range(1, 21)], "user")],
    "same_day_burst": [("light.porch", "on", [(1, 18, m) for m in range(0, 50, 5)], "user")],
    "three_days": [("light.porch", "on", [(d, 18, 0) for d in (1, 2, 3)], "user")],
    "sparse_over_busy_month": [
        ("light.porch", "on", [(d, 18, 0) for d in (2, 9, 16, 23, 28)], "user"),
        ("sensor.noise", "1", [(d, 12, 0) for d in range(1, 30)], "system")],
    "stale_routine": [
        ("light.porch", "on", [(d, 18, 0) for d in range(18, 29)], "user"),
        ("sensor.noise", "1", [(d, 12, 0) for d in range(1, 30)], "system")],
    "automation_caused": [("light.porch", "on", [(d, 18, 0) for d in range(1, 11)], "automation")],
    "split_hours": [("light.porch", "on",
                     [(d, 18 if d % 2 else 19, 0) for d in range(1, 13)], "user")],
}


def _db(history):
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    now = datetime.now().replace(second=0, microsecond=0)
    for eid, state, times, source in history:
        for days_ago, hour, minute in times:
            dt = (now - timedelta(days=days_ago)).replace(hour=hour, minute=minute)
            conn.execute(
                "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, new_state, "
                "area_id, hour, day_of_week, triggered_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (dt.isoformat(), eid, eid.split(".")[0], "off", state, "", hour,
                 dt.weekday(), source))
    conn.commit()
    return conn


@pytest.mark.parametrize("min_occ", [4, 5])
def test_time_routine_scores_match_the_characterization(load, monkeypatch, min_occ):
    pa = load("automation.patterns")
    monkeypatch.setattr(pa, "MIN_OCCURRENCES", min_occ)
    current = {}
    for name, history in sorted(HISTORIES.items()):
        conn = _db(history)
        try:
            found = pa.PatternAnalyzer()._find_time_routines(conn)
        finally:
            conn.close()
        current[name] = sorted(
            [p.entity_ids[0], p.details["state"], p.details["hour"], p.confidence,
             p.occurrences, p.details["observed_days"], p.details["opportunity_days"]]
            for p in found)
    pinned = _golden(f"time_routines_min{min_occ}", current)
    assert pinned == current
