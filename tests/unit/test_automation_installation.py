"""What create_automation builds, writes and records, and what
install_approved_suggestion hands to it.

The fail-closed and rollback paths are pinned in test_automation_creator_safety
and test_automation_phase5_fixes. These tests pin the shape of the automation
that is written, the audit rows, the replies, and the learn log line.
"""
from __future__ import annotations

import logging
import sys
import types

import pytest
import yaml

from fakes import FakeAutomationInventory


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
