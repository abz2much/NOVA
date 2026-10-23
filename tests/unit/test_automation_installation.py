"""What create_automation builds, writes and records, and what
install_approved_suggestion hands to it.

These tests check the shape of the automation that is written, the audit
rows, the replies and the learn log line. The last section covers ids,
locking, rollback and retry against a file backed inventory that behaves like
Home Assistant's loaded automation component. More fail closed paths are in
test_automation_creator_safety.
"""
from __future__ import annotations

import logging
import sys
import types

import pytest
import yaml

from fakes import FakeAutomationInventory
import asyncio
import sqlite3
from fakes import FakeAutomationInventory as FileInventory


class _Services:
    def __init__(self, inventory):
        self.inventory = inventory
        self.calls = []

    async def async_call(self, domain, service, data=None, blocking=False):
        self.calls.append((domain, service, blocking))
        self.inventory.reload()


class _Hass:
    def __init__(self, path):
        self.config = types.SimpleNamespace(path=lambda name: str(path))
        self.inventory = FakeAutomationInventory(path)
        self.services = _Services(self.inventory)
        self.data = {}
        self.states = types.SimpleNamespace(async_all=lambda domain=None: [])

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def audit(monkeypatch):
    rows = types.SimpleNamespace(minted=0, started=[], finished=[])
    module = types.ModuleType("jc.action_log")

    def new_request_id():
        rows.minted += 1
        return "minted-1"

    def start(*args, **kwargs):
        rows.started.append((args, kwargs))
        return 41

    module.new_request_id = new_request_id
    module.start = start
    module.set_execution = lambda *a, **k: rows.finished.append((a, k))
    monkeypatch.setitem(sys.modules, "jc.action_log", module)
    return rows


@pytest.fixture
def inst(load):
    return load("automation.installation")


@pytest.fixture
def home(load, monkeypatch, tmp_path):
    path = tmp_path / "automations.yaml"
    hass = _Hass(path)
    monkeypatch.setattr(load("automation.inventory"), "get_inventory",
                        lambda h: hass.inventory)
    hass.path = path
    return hass


TRIGGER = {"trigger": "time", "at": "18:00:00"}
ACTION = {"action": "light.turn_on", "entity_id": "light.porch"}


def _written(home):
    return yaml.safe_load(home.path.read_text())


async def test_a_minimal_automation_is_built_and_written(inst, audit, home):
    result = await inst.create_automation(
        home, alias="Porch on", trigger=TRIGGER, action=ACTION)
    config = {"alias": "Nova · Porch on", "description": "Created by Nova: Porch on",
              "mode": "single", "triggers": [TRIGGER], "actions": [ACTION]}
    auto_id = inst.automation_id_for("Porch on", config)
    assert result == {"success": True, "automation_id": auto_id,
                      "alias": "Nova · Porch on"}
    assert _written(home) == [{"id": auto_id, **config}]
    assert list(_written(home)[0]) == [
        "id", "alias", "description", "mode", "triggers", "actions"]
    assert home.services.calls == [("automation", "reload", True)]


async def test_every_part_given_is_kept_in_the_written_automation(inst, audit, home):
    condition = {"condition": "state", "entity_id": "person.alex", "state": "home"}
    result = await inst.create_automation(
        home, alias="Porch on", description="Evening light", trigger=[TRIGGER],
        condition=[condition], action=[ACTION, {"delay": "00:00:05"}],
        mode="restart")
    config = {"alias": "Nova · Porch on", "description": "Evening light",
              "mode": "restart", "triggers": [TRIGGER],
              "conditions": [condition], "actions": [ACTION, {"delay": "00:00:05"}]}
    assert result["automation_id"] == inst.automation_id_for("Porch on", config)
    assert _written(home) == [{"id": result["automation_id"], **config}]
    assert list(_written(home)[0]) == [
        "id", "alias", "description", "mode", "triggers", "actions", "conditions"]


async def test_single_items_are_wrapped_in_lists(inst, audit, home):
    condition = {"condition": "sun", "after": "sunset"}
    await inst.create_automation(
        home, alias="A", trigger=TRIGGER, condition=condition, action=ACTION)
    entry = _written(home)[0]
    assert entry["triggers"] == [TRIGGER]
    assert entry["conditions"] == [condition]
    assert entry["actions"] == [ACTION]


@pytest.mark.parametrize("condition", [None, [], {}])
async def test_an_empty_condition_leaves_no_conditions_key(inst, audit, home, condition):
    result = await inst.create_automation(
        home, alias="A", trigger=TRIGGER, condition=condition, action=ACTION)
    assert result["success"] is True
    assert "conditions" not in _written(home)[0]


async def test_a_blank_description_gets_the_default_text(inst, audit, home):
    await inst.create_automation(
        home, alias="Hall light", description="", trigger=TRIGGER, action=ACTION)
    assert _written(home)[0]["description"] == "Created by Nova: Hall light"


async def test_the_id_follows_the_alias_and_the_behaviour(inst, audit, home):
    first = await inst.create_automation(
        home, alias="Same alias", trigger=TRIGGER, action=ACTION)
    second = await inst.create_automation(
        home, alias="Same alias", trigger={"trigger": "time", "at": "19:00:00"},
        action=ACTION)
    assert first["automation_id"].startswith("nova_auto_same_alias_")
    assert second["automation_id"].startswith("nova_auto_same_alias_")
    assert first["automation_id"] != second["automation_id"]
    assert [e["id"] for e in _written(home)] == [
        first["automation_id"], second["automation_id"]]


@pytest.mark.parametrize("trigger, action", [
    (None, ACTION), ({}, ACTION), ([], ACTION),
    (TRIGGER, None), (TRIGGER, {}), (TRIGGER, []), (None, None)])
async def test_trigger_and_action_are_both_required(inst, audit, home, trigger, action):
    result = await inst.create_automation(
        home, alias="A", trigger=trigger, action=action)
    assert result == {"success": False,
                      "error": "Both trigger and action are required"}
    assert audit.finished == [((41, "failed"),
                               {"reason_code": "missing_trigger_or_action"})]
    assert not home.path.exists() and home.services.calls == []


async def test_the_audit_row_opens_before_anything_is_checked(inst, audit, home):
    await inst.create_automation(
        home, alias="A", trigger=None, action=None, request_id="req-9",
        source="chat", requested_by_user_id="u1", requested_by_name="Alex",
        request_device_id="dev1")
    assert audit.started == [(("req-9", "create_automation", "chat"), {
        "requested_by_user_id": "u1", "requested_by_name": "Alex",
        "request_device_id": "dev1"})]
    assert audit.minted == 0


async def test_a_request_id_is_minted_when_the_caller_has_none(inst, audit, home):
    await inst.create_automation(home, alias="A", trigger=TRIGGER, action=ACTION)
    assert audit.minted == 1
    assert audit.started == [(("minted-1", "create_automation", "ha_service"), {
        "requested_by_user_id": None, "requested_by_name": None,
        "request_device_id": None})]


async def test_success_is_recorded_as_accepted(inst, audit, home):
    await inst.create_automation(home, alias="A", trigger=TRIGGER, action=ACTION)
    assert audit.finished == [((41, "accepted"), {})]


async def test_the_alias_is_logged_with_the_id(inst, audit, home, caplog):
    with caplog.at_level(logging.INFO, logger=inst.__name__):
        result = await inst.create_automation(
            home, alias="Porch on", trigger=TRIGGER, action=ACTION)
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert infos == [f"Nova created automation: Porch on (id={result['automation_id']})"]


async def test_a_rejected_configuration_is_reported_and_nothing_is_written(
        inst, audit, home, monkeypatch):
    async def reject(hass, automation_id, config):
        raise ValueError("bad trigger")
    monkeypatch.setattr(inst, "_validate_with_home_assistant", reject)
    result = await inst.create_automation(
        home, alias="A", trigger=TRIGGER, action=ACTION)
    assert result == {"success": False,
                      "error": "Automation validation failed: bad trigger"}
    assert audit.finished == [((41, "failed"), {"reason_code": "invalid_automation"})]
    assert not home.path.exists()


async def test_an_equivalent_automation_is_not_added_twice(inst, audit, home):
    first = await inst.create_automation(
        home, alias="A", trigger=TRIGGER, action=ACTION)
    again = await inst.create_automation(
        home, alias="A", trigger=TRIGGER, action=ACTION)
    assert first["success"] is True
    assert again["success"] is False
    assert again["error"].startswith("Equivalent automation already exists: ")
    assert audit.finished[-1] == ((41, "failed"), {"reason_code": "duplicate_automation"})
    assert len(_written(home)) == 1


# ── install_approved_suggestion ──────────────────────────────────────────────

class _Analyzer:
    def __init__(self, suggestion):
        self.suggestion = suggestion
        self.calls = []

    def get_suggestion(self, sid):
        self.calls.append(("get", sid))
        return self.suggestion

    def approve_suggestion(self, sid):
        self.calls.append(("approve", sid))

    def mark_installed(self, sid, auto_id):
        self.calls.append(("installed", sid, auto_id))

    def mark_covered(self, sid):
        self.calls.append(("covered", sid))


def _suggestion(**extra):
    import json
    body = {"alias": "Nova Learned: light.porch on at 18:00",
            "trigger": {"platform": "time", "at": "18:00:00"},
            "action": {"service": "light.turn_on", "entity_id": "light.porch"}}
    body.update(extra)
    return {"id": 5, "description": "porch routine", "automation_yaml": json.dumps(body)}


@pytest.fixture
def installer(inst, audit, home, load, monkeypatch):
    analyzer = _Analyzer(_suggestion())
    monkeypatch.setattr(load("automation.patterns"), "get_analyzer", lambda: analyzer)
    sent = []
    ws = types.ModuleType("jc.websocket")
    ws.nova_log = lambda *a, **k: sent.append((a, k))
    monkeypatch.setitem(sys.modules, "jc.websocket", ws)
    return types.SimpleNamespace(analyzer=analyzer, sent=sent, hass=home, ws=ws)


async def test_the_suggestion_is_installed_and_marked(inst, installer, audit, home):
    result = await inst.install_approved_suggestion(home, 5)
    entry = _written(home)[0]
    assert result == {"ok": True, "installed": True, "automation_id": entry["id"],
                      "alias": "Nova · Nova Learned: light.porch on at 18:00",
                      "suggestion_id": 5}
    assert list(result) == ["ok", "installed", "automation_id", "alias", "suggestion_id"]
    assert installer.analyzer.calls == [("get", 5), ("installed", 5, entry["id"])]
    assert entry["description"] == "porch routine"
    assert entry["triggers"] == [{"trigger": "time", "at": "18:00:00"}]
    assert entry["actions"] == [{"action": "light.turn_on", "entity_id": "light.porch"}]
    assert entry["mode"] == "single" and "conditions" not in entry


async def test_the_audit_row_is_a_suggestion_request_with_the_callers_identity(
        inst, installer, audit, home):
    await inst.install_approved_suggestion(
        home, 5, requested_by_user_id="u1", requested_by_name="Alex",
        request_device_id="dev1")
    assert audit.minted == 1
    assert audit.started == [(("minted-1", "create_automation", "suggestion"), {
        "requested_by_user_id": "u1", "requested_by_name": "Alex",
        "request_device_id": "dev1"})]
    assert audit.finished == [((41, "accepted"), {})]


async def test_a_condition_and_mode_in_the_suggestion_reach_the_automation(
        inst, installer, audit, home):
    condition = [{"condition": "state", "entity_id": "person.alex", "state": "home"}]
    installer.analyzer.suggestion = _suggestion(condition=condition, mode="restart")
    result = await inst.install_approved_suggestion(home, 5)
    assert result["installed"] is True
    entry = _written(home)[0]
    assert entry["conditions"] == condition and entry["mode"] == "restart"


async def test_the_arguments_handed_to_create_automation(
        inst, installer, audit, home, monkeypatch):
    seen = {}

    async def fake_create(hass, **kwargs):
        seen["hass"] = hass
        seen.update(kwargs)
        return {"success": True, "automation_id": "nova_auto_x", "alias": "Nova · x"}

    monkeypatch.setattr(inst, "create_automation", fake_create)
    condition = [{"condition": "sun", "after": "sunset"}]
    installer.analyzer.suggestion = _suggestion(condition=condition, mode="queued")
    await inst.install_approved_suggestion(
        home, 5, requested_by_user_id="u1", requested_by_name="Alex",
        request_device_id="dev1")
    assert seen == {
        "hass": home,
        "alias": "Nova Learned: light.porch on at 18:00",
        "description": "porch routine",
        "trigger": [{"trigger": "time", "at": "18:00:00"}],
        "condition": condition,
        "action": [{"action": "light.turn_on", "entity_id": "light.porch"}],
        "mode": "queued",
        "request_id": "minted-1", "source": "suggestion",
        "requested_by_user_id": "u1", "requested_by_name": "Alex",
        "request_device_id": "dev1",
    }


async def test_a_suggestion_with_no_description_installs_with_the_default(
        inst, installer, audit, home, monkeypatch):
    seen = {}

    async def fake_create(hass, **kwargs):
        seen.update(kwargs)
        return {"success": True, "automation_id": "nova_auto_x", "alias": "Nova · x"}

    monkeypatch.setattr(inst, "create_automation", fake_create)
    sug = _suggestion()
    del sug["description"]
    installer.analyzer.suggestion = sug
    await inst.install_approved_suggestion(home, 5)
    assert seen["description"] == "" and seen["condition"] is None
    assert seen["mode"] == "single"


async def test_the_install_is_written_to_the_learn_log(inst, installer, audit, home):
    result = await inst.install_approved_suggestion(home, 5)
    assert installer.sent == [(("LEARN", f"Installed learned automation "
                               f"'{result['alias']}' from suggestion #5"), {})]


async def test_a_failing_learn_log_does_not_change_the_result(
        inst, installer, audit, home):
    def boom(*a, **k):
        raise RuntimeError("log full")
    installer.ws.nova_log = boom
    result = await inst.install_approved_suggestion(home, 5)
    assert result["ok"] is True and result["installed"] is True
    assert installer.analyzer.calls[-1][0] == "installed"


async def test_a_failed_write_leaves_the_suggestion_pending(
        inst, installer, audit, home, monkeypatch):
    async def fail(hass, **kwargs):
        return {"success": False, "error": "disk full"}
    monkeypatch.setattr(inst, "create_automation", fail)
    result = await inst.install_approved_suggestion(home, 5)
    assert result == {"ok": False, "installed": False,
                      "error": "automation write failed: disk full",
                      "reason": "automation write failed: disk full",
                      "suggestion_id": 5}
    assert installer.analyzer.calls == [("get", 5)]
    assert installer.sent == []


async def test_an_unexpected_failure_is_reported_in_safe_words_and_logged(
        inst, installer, audit, home, monkeypatch, caplog):
    def boom(sid):
        raise RuntimeError("secret detail")
    installer.analyzer.get_suggestion = boom
    with caplog.at_level(logging.ERROR, logger=inst.__name__):
        result = await inst.install_approved_suggestion(home, 5)
    assert result["ok"] is False and "secret detail" not in result["error"]
    assert [r.getMessage() for r in caplog.records] == [
        "install_approved_suggestion failed: secret detail"]


# ── ids, locking, rollback and retry ──────────────────────────────────────────

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
def audit_p5(monkeypatch):
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


async def test_long_shared_alias_prefix_gets_distinct_ids(
        installation, load, audit_p5, tmp_path, monkeypatch):
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


def test_ids_are_deterministic_and_keep_the_prefix(installation):
    config = {"triggers": [{"trigger": "time", "at": "18:00:00"}],
              "actions": [{"action": "light.turn_on", "entity_id": "light.p"}]}
    one = installation.automation_id_for("Porch on", config)
    assert one == installation.automation_id_for("Porch on", dict(config))
    assert one.startswith("nova_auto_porch_on_")
    other = installation.automation_id_for(
        "Porch on", {**config, "mode": "restart"})
    assert other != one


async def test_existing_entry_with_the_same_id_is_never_replaced(
        installation, load, audit_p5, tmp_path, monkeypatch):
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


@pytest.fixture
def home_p5(load, audit_p5, store_db, tmp_path, monkeypatch):
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
                                 original=path.read_bytes(), audit=audit_p5)


def _status(home_p5, sid=1):
    return home_p5.store.get(sid)["status"]


async def test_concurrent_approvals_install_once(installation, home_p5):
    hass = InstallHass(home_p5.path, Services(home_p5.inventory, hang_on=1))
    first, second = await asyncio.gather(
        installation.install_approved_suggestion(hass, 1),
        installation.install_approved_suggestion(hass, 1))
    assert sorted([first["installed"], second["installed"]]) == [False, True]
    ids = [i["id"] for i in yaml.safe_load(home_p5.path.read_text())]
    assert len(ids) == 2 and ids[0] == "legacy"
    assert _status(home_p5) == "installed"
    loser = first if not first["installed"] else second
    assert "installed" in loser["reason"]


async def test_dismissed_suggestion_is_never_installed(installation, home_p5):
    home_p5.store.dismiss(1)
    hass = InstallHass(home_p5.path, Services(home_p5.inventory))
    result = await installation.install_approved_suggestion(hass, 1)
    assert result["ok"] is False and result["installed"] is False
    assert "dismissed" in result["reason"]
    assert home_p5.path.read_bytes() == home_p5.original and hass.services.calls == []


async def test_cancellation_restores_bytes_reloads_and_reraises(installation, home_p5):
    hass = InstallHass(home_p5.path, Services(home_p5.inventory, hang_on=1))
    task = asyncio.ensure_future(installation.install_approved_suggestion(hass, 1))
    await hass.services.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert home_p5.path.read_bytes() == home_p5.original
    assert hass.services.calls == [("automation", "reload")] * 2
    assert [r.unique_id for r in home_p5.inventory.records()] == ["legacy"]
    assert _status(home_p5) == "pending"
    assert home_p5.audit["execution"][-1][1]["reason_code"] == "installation_cancelled"


async def test_a_second_cancel_cannot_skip_the_rollback(installation, home_p5):
    hass = InstallHass(home_p5.path, Services(home_p5.inventory, hang_on=1))
    task = asyncio.ensure_future(installation.install_approved_suggestion(hass, 1))
    await hass.services.started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert home_p5.path.read_bytes() == home_p5.original
    assert len(hass.services.calls) == 2


async def test_unavailable_inventory_fails_closed(installation, home_p5, load, monkeypatch):
    _use_inventory(load, monkeypatch, None)
    hass = InstallHass(home_p5.path, Services())
    result = await installation.install_approved_suggestion(hass, 1)
    assert result["ok"] is False and "unavailable" in result["reason"]
    assert home_p5.path.read_bytes() == home_p5.original and hass.services.calls == []
    assert _status(home_p5) == "pending"
    assert home_p5.audit["execution"][-1][1]["reason_code"] == "inventory_unavailable"


async def test_unconfirmed_load_rolls_back_and_says_so(installation, home_p5):
    home_p5.inventory.load_new = False
    hass = InstallHass(home_p5.path, Services(home_p5.inventory))
    result = await installation.install_approved_suggestion(hass, 1)
    assert result["ok"] is False
    assert "did not load" in result["reason"]
    assert "restored and reloaded" in result["reason"]
    assert home_p5.path.read_bytes() == home_p5.original
    assert len(hass.services.calls) == 2
    assert _status(home_p5) == "pending"


async def test_unconfirmable_load_fails_closed(installation, home_p5, load, monkeypatch):
    hass = InstallHass(home_p5.path, Services(home_p5.inventory))
    real_refresh = home_p5.inventory.refresh
    calls = {"n": 0}

    def flaky_refresh():
        calls["n"] += 1
        if calls["n"] == 2:        # the post-write confirmation
            raise RuntimeError("automation component gone")
        return real_refresh()

    home_p5.inventory.refresh = flaky_refresh
    result = await installation.install_approved_suggestion(hass, 1)
    assert result["ok"] is False and "could not confirm" in result["reason"]
    assert home_p5.path.read_bytes() == home_p5.original


async def test_failed_rollback_reload_is_reported(installation, home_p5):
    hass = InstallHass(home_p5.path, Services(home_p5.inventory, fail={1, 2}))
    result = await installation.install_approved_suggestion(hass, 1)
    assert result["ok"] is False
    assert "rollback failed" in result["reason"]
    assert home_p5.path.read_bytes() == home_p5.original
    assert home_p5.audit["execution"][-1][1]["reason_code"] == "rollback_failed"


async def test_failed_install_stays_retryable(installation, home_p5):
    hass = InstallHass(home_p5.path, Services(home_p5.inventory, fail={1}))
    failed = await installation.install_approved_suggestion(hass, 1)
    assert failed["ok"] is False and _status(home_p5) == "pending"
    assert [s["id"] for s in home_p5.analyzer.get_pending_suggestions()] == [1]
    retried = await installation.install_approved_suggestion(hass, 1)
    assert retried["installed"] is True and _status(home_p5) == "installed"


async def test_advisory_approval_is_unchanged(installation, home_p5, load):
    conn = sqlite3.connect(home_p5.store._db)
    conn.execute("UPDATE suggestions SET automation_yaml = ? WHERE id = 1",
                 ('{"note": "x", "type": "manual_review"}',))
    conn.commit()
    conn.close()
    hass = InstallHass(home_p5.path, Services(home_p5.inventory))
    result = await installation.install_approved_suggestion(hass, 1)
    assert result["ok"] is True and result["installed"] is False
    assert _status(home_p5) == "approved" and hass.services.calls == []


async def test_device_reaches_the_audit_row(installation, home_p5):
    hass = InstallHass(home_p5.path, Services(home_p5.inventory))
    await installation.install_approved_suggestion(
        hass, 1, requested_by_user_id="user-abi", request_device_id="device-kitchen")
    _args, kwargs = home_p5.audit["start"][-1]
    assert kwargs["requested_by_user_id"] == "user-abi"
    assert kwargs["request_device_id"] == "device-kitchen"


async def test_installation_never_blocks_on_template_similarity(
        installation, load, audit_p5, tmp_path, monkeypatch):
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
