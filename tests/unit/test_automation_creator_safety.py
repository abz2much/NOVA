"""Fail-closed and rollback guarantees for Nova automation installation."""
from __future__ import annotations

import sys
import types

import yaml


class _Services:
    def __init__(self, *, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first

    async def async_call(self, domain, service, data=None, blocking=False):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("reload failed")


class _Hass:
    def __init__(self, path, *, fail_first=False):
        self.config = types.SimpleNamespace(path=lambda name: str(path))
        self.services = _Services(fail_first=fail_first)
        self.data = {}

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
    hass = _Hass(path, fail_first=True)

    result = await creator.create_automation(
        hass, alias="Test", trigger={"trigger": "state"},
        action={"action": "light.turn_on"})

    assert result["success"] is False
    assert path.read_bytes() == original
    assert hass.services.calls == 2  # failed reload, then rollback reload


async def test_successful_write_preserves_existing_automations(
        load, monkeypatch, tmp_path):
    creator = load("automation_creator")
    _action_log(monkeypatch)
    path = tmp_path / "automations.yaml"
    path.write_text("- id: existing\n  alias: Existing\n")
    hass = _Hass(path)

    result = await creator.create_automation(
        hass, alias="Test Rule", trigger={"trigger": "state"},
        action={"action": "light.turn_on"})

    assert result["success"] is True
    data = yaml.safe_load(path.read_text())
    assert [item["id"] for item in data] == [
        "existing", "nova_auto_test_rule"]
    assert hass.services.calls == 1
    assert not list(tmp_path.glob(".nova-automations-*.tmp"))
