"""Regression tests for the automation defects fixed in Phase 5 (D1–D11).

Every test here failed on the pre-Phase-5 code. Destructive paths use
temporary files, a fake Home Assistant reload and a file-backed inventory
that behaves like Home Assistant's loaded automation component: after a
reload it reports exactly what automations.yaml contains.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import types
from datetime import datetime, timedelta

import pytest
import yaml

from fakes import FakeHass


# ── Shared harness ──────────────────────────────────────────────────────────

class ThreadedHass(FakeHass):
    """Runs executor jobs on a real thread pool, like Home Assistant."""

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)


class FileInventory:
    """Home Assistant's loaded automations, as read from automations.yaml
    at the last reload."""

    def __init__(self, path, *, load_new=True):
        self.path = path
        self.load_new = load_new
        self._records = []
        self.refreshes = 0
        self.reload()

    def reload(self):
        records_mod = sys.modules["jc.automation.models"]
        try:
            items = yaml.safe_load(self.path.read_text()) or []
        except FileNotFoundError:
            items = []
        self._records = [
            records_mod.AutomationRecord(
                entity_id=f"automation.{i.get('id')}", unique_id=str(i.get("id")),
                name=str(i.get("alias", "")), raw_config=i)
            for i in items
            if self.load_new or not str(i.get("id", "")).startswith("nova_auto_")
        ]

    def refresh(self):
        self.refreshes += 1
        return self.records()

    def records(self):
        return list(self._records)


class Services:
    def __init__(self, inventory=None, *, fail=(), hang_on=None):
        self.calls = []
        self.inventory = inventory
        self.fail = set(fail)
        self.hang_on = hang_on
        self.started = asyncio.Event()

    async def async_call(self, domain, service, data=None, blocking=False):
        self.calls.append((domain, service))
        n = len(self.calls)
        if self.hang_on == n:
            self.started.set()
            await asyncio.sleep(0.05)
        if n in self.fail:
            raise RuntimeError(f"reload {n} failed")
        if self.inventory is not None:
            self.inventory.reload()


class InstallHass:
    def __init__(self, path, services):
        self.config = types.SimpleNamespace(path=lambda name: str(path))
        self.services = services
        self.data = {}

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)


@pytest.fixture
def audit(monkeypatch):
    rows = {"start": [], "execution": []}
    mod = types.ModuleType("jc.action_log")
    mod.new_request_id = lambda: "request-1"

    def start(*args, **kwargs):
        rows["start"].append((args, kwargs))
        return len(rows["start"])

    mod.start = start
    mod.set_execution = lambda *a, **k: rows["execution"].append((a, k))
    monkeypatch.setitem(sys.modules, "jc.action_log", mod)
    return rows


@pytest.fixture
def installation(load):
    return load("automation.installation")


def _use_inventory(load, monkeypatch, inventory):
    monkeypatch.setattr(load("automation.inventory"), "get_inventory",
                        lambda hass: inventory)


def _light(entity="light.porch", at="18:00:00"):
    return dict(trigger={"trigger": "time", "at": at},
                action={"action": "light.turn_on", "entity_id": entity})


# ── D1: pattern analysis keeps each SQLite connection in its own thread ─────

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


async def test_d1_analysis_finds_patterns_with_a_real_executor(load, tmp_path, monkeypatch):
    patterns = load("automation.patterns")
    stored = []
    an = _quiet_analyzer(patterns, _history_db(tmp_path), monkeypatch, stored)
    found = await an.analyze(ThreadedHass())
    assert any(p.pattern_type == "time_routine" for p in found)
    assert stored, "a real thread pool must still store the routine"
    assert an._last_result["patterns_found"] == len(found)


async def test_d1_no_sqlite_connection_is_opened_on_the_event_loop(
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


# ── D2: collision-resistant ids never replace another automation ────────────

async def test_d2_long_shared_alias_prefix_gets_distinct_ids(
        installation, load, audit, tmp_path, monkeypatch):
    path = tmp_path / "automations.yaml"
    path.write_text("- id: legacy\n  alias: Legacy\n")
    inventory = FileInventory(path)
    _use_inventory(load, monkeypatch, inventory)
    hass = InstallHass(path, Services(inventory))
    prefix = "Nova Learned: light.living_room_floor_lamp after "
    first = await installation.create_automation(
        hass, alias=prefix + "binary_sensor.hall_motion",
        trigger={"trigger": "state", "entity_id": "binary_sensor.hall_motion", "to": "on"},
        action={"action": "light.turn_on", "entity_id": "light.living_room_floor_lamp"})
    second = await installation.create_automation(
        hass, alias=prefix + "binary_sensor.stairs_motion",
        trigger={"trigger": "state", "entity_id": "binary_sensor.stairs_motion", "to": "on"},
        action={"action": "light.turn_on", "entity_id": "light.living_room_floor_lamp"})
    assert first["success"] and second["success"], (first, second)
    assert first["automation_id"] != second["automation_id"]
    ids = [i["id"] for i in yaml.safe_load(path.read_text())]
    assert ids == ["legacy", first["automation_id"], second["automation_id"]]


def test_d2_ids_are_deterministic_and_keep_the_prefix(installation):
    config = {"triggers": [{"trigger": "time", "at": "18:00:00"}],
              "actions": [{"action": "light.turn_on", "entity_id": "light.p"}]}
    one = installation.automation_id_for("Porch on", config)
    assert one == installation.automation_id_for("Porch on", dict(config))
    assert one.startswith("nova_auto_porch_on_")
    other = installation.automation_id_for(
        "Porch on", {**config, "mode": "restart"})
    assert other != one


async def test_d2_existing_entry_with_the_same_id_is_never_replaced(
        installation, load, audit, tmp_path, monkeypatch):
    kw = _light()
    config = {"alias": "Nova · Porch", "description": "Created by Nova: Porch",
              "mode": "single", "triggers": [kw["trigger"]], "actions": [kw["action"]]}
    auto_id = installation.automation_id_for("Porch", config)
    path = tmp_path / "automations.yaml"
    original = yaml.safe_dump([{"id": auto_id, "alias": "Someone else's",
                                "triggers": [{"trigger": "sun", "event": "sunset"}],
                                "actions": [{"action": "light.turn_off",
                                             "entity_id": "light.x"}]}]).encode()
    path.write_bytes(original)
    inventory = FileInventory(path)
    _use_inventory(load, monkeypatch, inventory)
    hass = InstallHass(path, Services(inventory))
    result = await installation.create_automation(hass, alias="Porch", **kw)
    assert result["success"] is False
    assert path.read_bytes() == original
    assert hass.services.calls == []


# ── D3: suggestions deduplicate on stable identity, not on counts ──────────

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


def _rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT id, status, description, pattern_count FROM suggestions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_d3_changing_counts_update_the_same_suggestion(load, store_db):
    suggestions, models = load("automation.suggestions"), load("automation.models")
    store = suggestions.SuggestionStore(store_db)
    assert store.store(_routine(models, 12)) is True
    assert store.store(_routine(models, 14)) is False
    rows = _rows(store_db)
    assert len(rows) == 1
    assert rows[0][1] == "pending" and "14 of 30" in rows[0][2] and rows[0][3] == 14


def test_d3_dismissed_suggestion_stays_dismissed(load, store_db):
    suggestions, models = load("automation.suggestions"), load("automation.models")
    store = suggestions.SuggestionStore(store_db)
    store.store(_routine(models, 12))
    assert store.dismiss(1) is True
    assert store.store(_routine(models, 20)) is False
    assert [r[1] for r in _rows(store_db)] == ["dismissed"]
    assert store.pending() == []


def test_d3_material_change_is_a_new_suggestion(load, store_db):
    suggestions, models = load("automation.suggestions"), load("automation.models")
    store = suggestions.SuggestionStore(store_db)
    store.store(_routine(models, 12))
    store.dismiss(1)
    assert store.store(_routine(models, 12, hour=19)) is True
    assert [r[1] for r in _rows(store_db)] == ["dismissed", "pending"]


def test_d3_sequence_identity_ignores_measured_delay_and_conditions(load):
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
