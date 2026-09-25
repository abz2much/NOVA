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

from fakes import FakeAutomationInventory as FileInventory, FakeHass


# ── Shared harness ──────────────────────────────────────────────────────────

class ThreadedHass(FakeHass):
    """Runs executor jobs on a real thread pool, like Home Assistant."""

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)


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


# ── D4–D7: the installation transaction ─────────────────────────────────────

@pytest.fixture
def home(load, audit, store_db, tmp_path, monkeypatch):
    """A real suggestion store with one pending routine, an automations.yaml,
    and Home Assistant's loaded automations following each reload."""
    suggestions, models = load("automation.suggestions"), load("automation.models")
    patterns = load("automation.patterns")
    an = patterns.PatternAnalyzer()
    an._db = store_db
    an._store_suggestion(_routine(models, 12))
    monkeypatch.setattr(patterns, "get_analyzer", lambda: an)
    path = tmp_path / "automations.yaml"
    path.write_bytes(b"# household automations\n- id: legacy\n  alias: Legacy\n")
    inventory = FileInventory(path)
    _use_inventory(load, monkeypatch, inventory)
    return types.SimpleNamespace(analyzer=an, path=path, inventory=inventory,
                                 store=suggestions.SuggestionStore(store_db),
                                 original=path.read_bytes(), audit=audit)


def _status(home, sid=1):
    return home.store.get(sid)["status"]


async def test_d4_concurrent_approvals_install_once(installation, home):
    hass = InstallHass(home.path, Services(home.inventory, hang_on=1))
    first, second = await asyncio.gather(
        installation.install_approved_suggestion(hass, 1),
        installation.install_approved_suggestion(hass, 1))
    assert sorted([first["installed"], second["installed"]]) == [False, True]
    ids = [i["id"] for i in yaml.safe_load(home.path.read_text())]
    assert len(ids) == 2 and ids[0] == "legacy"
    assert _status(home) == "installed"
    loser = first if not first["installed"] else second
    assert "installed" in loser["reason"]


async def test_d4_dismissed_suggestion_is_never_installed(installation, home):
    home.store.dismiss(1)
    hass = InstallHass(home.path, Services(home.inventory))
    result = await installation.install_approved_suggestion(hass, 1)
    assert result["ok"] is False and result["installed"] is False
    assert "dismissed" in result["reason"]
    assert home.path.read_bytes() == home.original and hass.services.calls == []


def test_d4_duplicate_check_runs_inside_the_write_lock():
    import contract_extract as ce
    src = (ce.COMP / "automation" / "installation.py").read_text(encoding="utf-8")
    body = src[src.index("async with installation_transaction():"):]
    lock_block = body[:body.index("_LOGGER.info(\"Nova created automation")]
    assert "_duplicate_automation(auto_config" in lock_block
    assert "inventory.refresh()" in lock_block


async def test_d5_cancellation_restores_bytes_reloads_and_reraises(installation, home):
    hass = InstallHass(home.path, Services(home.inventory, hang_on=1))
    task = asyncio.ensure_future(installation.install_approved_suggestion(hass, 1))
    await hass.services.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert home.path.read_bytes() == home.original
    assert hass.services.calls == [("automation", "reload")] * 2
    assert [r.unique_id for r in home.inventory.records()] == ["legacy"]
    assert _status(home) == "pending"
    assert home.audit["execution"][-1][1]["reason_code"] == "installation_cancelled"


async def test_d5_a_second_cancel_cannot_skip_the_rollback(installation, home):
    hass = InstallHass(home.path, Services(home.inventory, hang_on=1))
    task = asyncio.ensure_future(installation.install_approved_suggestion(hass, 1))
    await hass.services.started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert home.path.read_bytes() == home.original
    assert len(hass.services.calls) == 2


async def test_d6_unavailable_inventory_fails_closed(installation, home, load, monkeypatch):
    _use_inventory(load, monkeypatch, None)
    hass = InstallHass(home.path, Services())
    result = await installation.install_approved_suggestion(hass, 1)
    assert result["ok"] is False and "unavailable" in result["reason"]
    assert home.path.read_bytes() == home.original and hass.services.calls == []
    assert _status(home) == "pending"
    assert home.audit["execution"][-1][1]["reason_code"] == "inventory_unavailable"


async def test_d6_unconfirmed_load_rolls_back_and_says_so(installation, home):
    home.inventory.load_new = False
    hass = InstallHass(home.path, Services(home.inventory))
    result = await installation.install_approved_suggestion(hass, 1)
    assert result["ok"] is False
    assert "did not load" in result["reason"]
    assert "restored and reloaded" in result["reason"]
    assert home.path.read_bytes() == home.original
    assert len(hass.services.calls) == 2
    assert _status(home) == "pending"


async def test_d6_unconfirmable_load_fails_closed(installation, home, load, monkeypatch):
    hass = InstallHass(home.path, Services(home.inventory))
    real_refresh = home.inventory.refresh
    calls = {"n": 0}

    def flaky_refresh():
        calls["n"] += 1
        if calls["n"] == 2:        # the post-write confirmation
            raise RuntimeError("automation component gone")
        return real_refresh()

    home.inventory.refresh = flaky_refresh
    result = await installation.install_approved_suggestion(hass, 1)
    assert result["ok"] is False and "could not confirm" in result["reason"]
    assert home.path.read_bytes() == home.original


async def test_d6_failed_rollback_reload_is_reported(installation, home):
    hass = InstallHass(home.path, Services(home.inventory, fail={1, 2}))
    result = await installation.install_approved_suggestion(hass, 1)
    assert result["ok"] is False
    assert "rollback failed" in result["reason"]
    assert home.path.read_bytes() == home.original
    assert home.audit["execution"][-1][1]["reason_code"] == "rollback_failed"


async def test_d7_failed_install_stays_retryable(installation, home):
    hass = InstallHass(home.path, Services(home.inventory, fail={1}))
    failed = await installation.install_approved_suggestion(hass, 1)
    assert failed["ok"] is False and _status(home) == "pending"
    assert [s["id"] for s in home.analyzer.get_pending_suggestions()] == [1]
    retried = await installation.install_approved_suggestion(hass, 1)
    assert retried["installed"] is True and _status(home) == "installed"


async def test_d7_advisory_approval_is_unchanged(installation, home, load):
    conn = sqlite3.connect(home.store._db)
    conn.execute("UPDATE suggestions SET automation_yaml = ? WHERE id = 1",
                 ('{"note": "x", "type": "manual_review"}',))
    conn.commit()
    conn.close()
    hass = InstallHass(home.path, Services(home.inventory))
    result = await installation.install_approved_suggestion(hass, 1)
    assert result["ok"] is True and result["installed"] is False
    assert _status(home) == "approved" and hass.services.calls == []


def test_d7_panel_keeps_a_failed_suggestion_actionable():
    import contract_extract as ce
    js = (ce.COMP / "frontend" / "nova-panel.js").read_text(encoding="utf-8")
    wire = js[js.index("_wireSuggestions() {"):js.index("// ─── Settings")]
    assert "if (res && res.ok)" in wire
    assert "buttons.forEach(b => b.disabled = false)" in wire
    assert "res.reason" in wire


# ── D8: the panel names the automation the backend actually matched ─────────

def test_d8_panel_reads_the_matched_automation():
    import contract_extract as ce
    js = (ce.COMP / "frontend" / "nova-panel.js").read_text(encoding="utf-8")
    block = js[js.index("const match = s.automation_match"):js.index("return `", js.index("const match = s.automation_match"))]
    assert "(match.matches || [])[0]" in block
    assert "match.name" not in block and "match.entity_id" not in block


def test_d8_match_payload_carries_names_only_in_matches(load):
    matcher = load("automation.matching")
    existing = _record("automation.sunset", {
        "triggers": [{"trigger": "sun", "event": "sunset"}],
        "actions": [{"action": "light.turn_on", "entity_id": "light.porch"}]})
    out = matcher.classify({"triggers": [{"trigger": "time", "at": "20:00:00"}],
                            "actions": [{"action": "light.turn_on",
                                         "entity_id": "light.porch"}]}, [existing])
    assert set(out) == {"status", "matches", "reason"}
    assert out["matches"] == [{"entity_id": "automation.sunset",
                               "name": "automation.sunset"}]


# ── D9: agent approval carries the real request identity ────────────────────

async def test_d9_agent_approval_passes_the_real_user_and_device(load, monkeypatch):
    agent = load("agent")
    installation = load("automation.installation")
    seen = {}

    async def fake_install(hass, sid, **kw):
        seen.update(kw, sid=sid)
        return {"ok": True, "installed": False, "reason": "advisory"}

    monkeypatch.setattr(installation, "install_approved_suggestion", fake_install)
    user_input = types.SimpleNamespace(
        device_id="device-kitchen", context=types.SimpleNamespace(user_id="user-abi"))
    await agent._execute_tool(FakeHass(), "approve_suggestion",
                              {"suggestion_id": 5}, None, user_input)
    assert seen == {"sid": 5, "requested_by_user_id": "user-abi",
                    "request_device_id": "device-kitchen"}


async def test_d9_agent_approval_never_invents_a_user(load, monkeypatch):
    agent = load("agent")
    installation = load("automation.installation")
    seen = {}

    async def fake_install(hass, sid, **kw):
        seen.update(kw)
        return {"ok": True}

    monkeypatch.setattr(installation, "install_approved_suggestion", fake_install)
    user_input = types.SimpleNamespace(device_id=None,
                                       context=types.SimpleNamespace(user_id=None))
    await agent._execute_tool(FakeHass(), "approve_suggestion",
                              {"suggestion_id": 5}, None, user_input)
    assert seen == {"requested_by_user_id": None, "request_device_id": None}
    await agent._execute_tool(FakeHass(), "approve_suggestion",
                              {"suggestion_id": 5}, None, None)
    assert seen == {"requested_by_user_id": None, "request_device_id": None}


async def test_d9_device_reaches_the_audit_row(installation, home):
    hass = InstallHass(home.path, Services(home.inventory))
    await installation.install_approved_suggestion(
        hass, 1, requested_by_user_id="user-abi", request_device_id="device-kitchen")
    _args, kwargs = home.audit["start"][-1]
    assert kwargs["requested_by_user_id"] == "user-abi"
    assert kwargs["request_device_id"] == "device-kitchen"


# ── D10: templates and blueprints stay explicitly uncertain ─────────────────

def _record(entity_id, raw_config, refs=()):
    return types.SimpleNamespace(entity_id=entity_id, name=entity_id,
                                 raw_config=raw_config, referenced_entities=tuple(refs))


_PORCH = {"triggers": [{"trigger": "time", "at": "20:00:00"}],
          "actions": [{"action": "light.turn_on", "entity_id": "light.porch"}]}


def test_d10_templated_target_is_never_proof_of_new(load):
    matcher = load("automation.matching")
    templated = _record("automation.dynamic", {
        "triggers": [{"trigger": "sun", "event": "sunset"}],
        "actions": [{"action": "light.turn_on",
                     "target": {"entity_id": "{{ states.light | map(attribute='entity_id') | list }}"}}]})
    out = matcher.classify(_PORCH, [templated])
    assert out["status"] == "unknown_overlap"
    assert out["matches"][0]["entity_id"] == "automation.dynamic"


def test_d10_template_strings_are_not_entity_ids(load):
    matcher = load("automation.matching")
    effects = matcher.action_effects({
        "triggers": [{"trigger": "sun"}],
        "actions": [{"action": "light.turn_on",
                     "entity_id": "{{ 'light.porch' if is_state('sun.sun', 'below_horizon') else 'light.hall' }}"}]})
    assert effects == set()


def test_d10_opaque_blueprint_with_no_references_is_uncertain(load):
    matcher = load("automation.matching")
    blueprint = _record("automation.bp", {"use_blueprint": {"path": "x.yaml", "input": {}}})
    assert matcher.classify(_PORCH, [blueprint])["status"] == "unknown_overlap"
    metadata_only = _record("automation.meta", None)
    assert matcher.classify(_PORCH, [metadata_only])["status"] == "unknown_overlap"


def test_d10_templated_candidate_is_never_new_or_a_duplicate(load):
    matcher = load("automation.matching")
    candidate = {"triggers": [{"trigger": "template",
                               "value_template": "{{ is_state('sun.sun', 'below_horizon') }}"}],
                 "actions": [{"action": "light.turn_on", "entity_id": "light.porch"}]}
    similar = _record("automation.other", {
        "triggers": [{"trigger": "template",
                      "value_template": "{{ states('sun.sun') == 'below_horizon' }}"}],
        "actions": [{"action": "light.turn_off", "entity_id": "light.hall"}]})
    assert matcher.classify(candidate, [])["status"] == "unknown_overlap"
    assert matcher.classify(candidate, [similar])["status"] == "unknown_overlap"


def test_d10_unrelated_plain_automation_is_still_new(load):
    matcher = load("automation.matching")
    other = _record("automation.hall", {
        "triggers": [{"trigger": "sun", "event": "sunset"}],
        "actions": [{"action": "light.turn_on", "entity_id": "light.hall"}]},
        refs=("light.hall",))
    assert matcher.classify(_PORCH, [other])["status"] == "new"


def test_d10_blueprint_with_unrelated_references_stays_uncertain(load):
    matcher = load("automation.matching")
    blueprint = _record("automation.bp_hall",
                        {"use_blueprint": {"path": "motion_light.yaml",
                                           "input": {"light": "light.hall"}}},
                        refs=("light.hall", "binary_sensor.hall_motion"))
    out = matcher.classify(_PORCH, [blueprint])
    assert out["status"] == "unknown_overlap"
    assert out["matches"] == [{"entity_id": "automation.bp_hall",
                               "name": "automation.bp_hall"}]


def test_d10_blueprint_with_only_device_or_area_references_stays_uncertain(load):
    matcher = load("automation.matching")
    blueprint = types.SimpleNamespace(
        entity_id="automation.bp_area", name="Area lights", referenced_entities=(),
        referenced_devices=("device-porch-light",), referenced_areas=("porch",),
        raw_config={"use_blueprint": {"path": "area_lights.yaml",
                                      "input": {"area": "porch"}}})
    assert matcher.classify(_PORCH, [blueprint])["status"] == "unknown_overlap"


def test_d10_metadata_only_record_with_unrelated_references_stays_uncertain(load):
    matcher = load("automation.matching")
    meta = _record("automation.meta_hall", None, refs=("light.hall",))
    assert matcher.classify(_PORCH, [meta])["status"] == "unknown_overlap"


def test_d10_templated_record_with_literal_targets_stays_uncertain(load):
    matcher = load("automation.matching")
    templated = _record("automation.hall_if", {
        "triggers": [{"trigger": "template", "value_template": "{{ is_state('sun.sun', 'below_horizon') }}"}],
        "actions": [{"action": "light.turn_on", "entity_id": "light.hall"}]},
        refs=("light.hall",))
    assert matcher.classify(_PORCH, [templated])["status"] == "unknown_overlap"


def test_d10_identical_template_text_is_an_exact_duplicate(load):
    matcher = load("automation.matching")
    config = {"triggers": [{"trigger": "template",
                            "value_template": "{{ is_state('sun.sun', 'below_horizon') }}"}],
              "actions": [{"action": "light.turn_on", "entity_id": "light.porch"}]}
    existing = _record("automation.porch_dark", {
        "id": "x", "alias": "Porch after dark", "mode": "single", **config})
    out = matcher.classify(config, [existing])
    assert out["status"] == "already_automated"
    assert out["matches"][0]["entity_id"] == "automation.porch_dark"


async def test_d10_installation_never_blocks_on_template_similarity(
        installation, load, audit, tmp_path, monkeypatch):
    path = tmp_path / "automations.yaml"
    path.write_text(yaml.safe_dump([{
        "id": "templated", "alias": "Templated",
        "triggers": [{"trigger": "sun", "event": "sunset"}],
        "actions": [{"action": "light.turn_on",
                     "target": {"entity_id": "{{ 'light.porch' }}"}}]}]))
    inventory = FileInventory(path)
    _use_inventory(load, monkeypatch, inventory)
    hass = InstallHass(path, Services(inventory))
    result = await installation.create_automation(hass, alias="Porch", **_light())
    assert result["success"] is True


# ── D11: pattern analysis is single-flight ──────────────────────────────────

async def test_d11_manual_and_scheduled_analysis_never_overlap(load, monkeypatch):
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


async def test_d11_scheduled_tick_skips_while_an_analysis_runs(load):
    import contract_extract as ce
    src = (ce.COMP / "cognitive_core.py").read_text(encoding="utf-8")
    tick = src[src.index("# Run pattern analysis periodically"):]
    tick = tick[:tick.index("patterns = await analyzer.analyze(hass)")]
    assert "not analyzer.analysis_running" in tick
