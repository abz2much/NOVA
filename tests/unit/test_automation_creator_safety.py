"""Fail-closed and rollback guarantees for Nova automation installation."""
from __future__ import annotations

import sys
import types

import yaml

from fakes import FakeAutomationInventory


class _Services:
    def __init__(self, inventory, *, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first
        self.inventory = inventory

    async def async_call(self, domain, service, data=None, blocking=False):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("reload failed")
        self.inventory.reload()


class _Hass:
    """Home Assistant with a loaded-automation list that follows reloads
    (Phase 5, D6: without one, installation fails closed)."""

    def __init__(self, path, *, fail_first=False, monkeypatch=None, load=None):
        self.config = types.SimpleNamespace(path=lambda name: str(path))
        self.inventory = FakeAutomationInventory(path)
        self.services = _Services(self.inventory, fail_first=fail_first)
        self.data = {}
        if monkeypatch is not None:
            monkeypatch.setattr(load("automation.inventory"), "get_inventory",
                                lambda hass: self.inventory)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _action_log(monkeypatch):
    events = []
    module = types.ModuleType("jc.action_log")
    module.new_request_id = lambda: "request-1"
    module.start = lambda *args, **kwargs: 17
    module.set_execution = lambda *args, **kwargs: events.append((args, kwargs))
    monkeypatch.setitem(sys.modules, "jc.action_log", module)
    return events


async def test_malformed_existing_yaml_is_never_overwritten(
        load, monkeypatch, tmp_path):
    creator = load("automation_creator")
    events = _action_log(monkeypatch)
    path = tmp_path / "automations.yaml"
    original = b"- id: okay\n  alias: [broken\n"
    path.write_bytes(original)
    hass = _Hass(path)

    result = await creator.create_automation(
        hass, alias="Test", trigger={"trigger": "state"},
        action={"action": "light.turn_on"})

    assert result["success"] is False
    assert "could not be parsed" in result["error"]
    assert path.read_bytes() == original
    assert hass.services.calls == 0
    assert events[-1][1]["reason_code"] == "write_or_reload_failed"


async def test_non_list_existing_yaml_is_never_overwritten(
        load, monkeypatch, tmp_path):
    creator = load("automation_creator")
    _action_log(monkeypatch)
    path = tmp_path / "automations.yaml"
    original = b"automation_one:\n  alias: wrong shape\n"
    path.write_bytes(original)

    result = await creator.create_automation(
        _Hass(path), alias="Test", trigger={"trigger": "state"},
        action={"action": "light.turn_on"})

    assert result["success"] is False
    assert "not a list" in result["error"]
    assert path.read_bytes() == original


async def test_reload_failure_restores_exact_original_bytes(
        load, monkeypatch, tmp_path):
    creator = load("automation_creator")
    _action_log(monkeypatch)
    path = tmp_path / "automations.yaml"
    original = b"# keep this comment\n- id: existing\n  alias: Existing\n"
    path.write_bytes(original)
    hass = _Hass(path, fail_first=True, monkeypatch=monkeypatch, load=load)

    result = await creator.create_automation(
        hass, alias="Test", trigger={"trigger": "state"},
        action={"action": "light.turn_on"})

    assert result["success"] is False
    assert "restored and reloaded" in result["error"]
    assert path.read_bytes() == original
    assert hass.services.calls == 2  # failed reload, then rollback reload


async def test_successful_write_preserves_existing_automations(
        load, monkeypatch, tmp_path):
    creator = load("automation_creator")
    _action_log(monkeypatch)
    path = tmp_path / "automations.yaml"
    path.write_text("- id: existing\n  alias: Existing\n")
    hass = _Hass(path, monkeypatch=monkeypatch, load=load)

    result = await creator.create_automation(
        hass, alias="Test Rule", trigger={"trigger": "state"},
        action={"action": "light.turn_on"})

    assert result["success"] is True
    data = yaml.safe_load(path.read_text())
    # Phase 5 (D2): the readable slug is kept and a behaviour digest makes
    # the id collision-resistant.
    assert [item["id"] for item in data][0] == "existing"
    assert data[1]["id"].startswith("nova_auto_test_rule_")
    assert data[1]["id"] == result["automation_id"]
    assert hass.services.calls == 1
    assert not list(tmp_path.glob(".nova-automations-*.tmp"))
