"""Tests for execute_plan's domain allowlist (v7.87.0, backlog #3).

Unlike control_device/bulk_control -- which can only ever reach a small
hardcoded action_map and so are structurally incapable of calling anything
but turn_on/lock/open/etc. -- execute_plan takes domain/service straight from
the LLM's own plan JSON with nothing constraining it. The old "does the
entity_id exist" check caught almost nothing: a domain-level service like
homeassistant.restart or shell_command.* ignores its entity_id entirely, so
any real entity_id in the house satisfied that check while doing something
the tool was never meant to do. These tests prove the allowlist actually
blocks that class of step, before entity/confirmation checks even run.
"""
import json

import pytest


@pytest.fixture
def agent(load):
    return load("agent")


@pytest.fixture
def pol(load):
    return load("policy")


class _Hass:
    def __init__(self, known_entities=None):
        self._known = set(known_entities or ["light.kitchen", "lock.front_door"])
        self.service_calls = []

        class _States:
            def __init__(self, outer):
                self._outer = outer

            def get(self, eid):
                return object() if eid in self._outer._known else None

        class _Services:
            def __init__(self, outer):
                self._outer = outer

            async def async_call(self, domain, service, data=None, blocking=False, **kw):
                self._outer.service_calls.append((domain, service, dict(data or {})))

        self.states = _States(self)
        self.services = _Services(self)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _step(domain, service, entity_id, **extra):
    return {"domain": domain, "service": service, "entity_id": entity_id, **extra}


async def _allow_gate(hass, domain, service, entity_id="", action_label="", device_id="",
                        target_name=""):
    return True, "", "not_required"


async def test_allowed_domain_runs_normally(agent, pol, monkeypatch):
    monkeypatch.setattr(pol, "confirm_gate", _allow_gate)

    hass = _Hass()
    out = json.loads(await agent._exec_execute_plan(
        hass, {"goal": "evening", "steps": [_step("light", "turn_on", "light.kitchen")]}))
    assert out["results"][0]["ok"] is True
    assert hass.service_calls == [("light", "turn_on", {"entity_id": "light.kitchen"})]


async def test_disallowed_domain_rejected_before_entity_check(agent, pol, monkeypatch):
    """The attack this closes: a domain-level service that ignores its
    entity_id, paired with a real, existing entity_id purely to satisfy the
    old (meaningless, for this case) entity-existence check."""
    monkeypatch.setattr(pol, "confirm_gate", _allow_gate)

    hass = _Hass(known_entities=["light.kitchen"])
    out = json.loads(await agent._exec_execute_plan(
        hass,
        {"goal": "do something", "steps": [
            _step("homeassistant", "restart", "light.kitchen"),  # real, existing entity
        ]},
    ))
    step = out["results"][0]
    assert step["ok"] is False
    assert "not allowed" in step["error"].lower()
    assert hass.service_calls == []  # never even attempted


async def test_shell_command_domain_rejected(agent, pol, monkeypatch):
    monkeypatch.setattr(pol, "confirm_gate", _allow_gate)

    hass = _Hass(known_entities=["light.kitchen"])
    out = json.loads(await agent._exec_execute_plan(
        hass,
        {"goal": "x", "steps": [_step("shell_command", "wipe_disk", "light.kitchen")]},
    ))
    assert out["results"][0]["ok"] is False
    assert hass.service_calls == []


async def test_nonexistent_entity_in_disallowed_domain_still_reports_domain_error(agent, pol, monkeypatch):
    """The domain check runs BEFORE the entity-existence check -- confirms
    ordering, not just outcome (a step with both problems should report the
    domain issue, the more fundamental one)."""
    monkeypatch.setattr(pol, "confirm_gate", _allow_gate)

    hass = _Hass(known_entities=[])
    out = json.loads(await agent._exec_execute_plan(
        hass,
        {"goal": "x", "steps": [_step("backup", "create", "backup.nonexistent")]},
    ))
    step = out["results"][0]
    assert step["ok"] is False
    assert "not allowed" in step["error"].lower()  # domain error, not "not found"


async def test_mixed_plan_allowed_step_still_runs(agent, pol, monkeypatch):
    monkeypatch.setattr(pol, "confirm_gate", _allow_gate)

    hass = _Hass(known_entities=["light.kitchen", "lock.front_door"])
    out = json.loads(await agent._exec_execute_plan(
        hass,
        {"goal": "mixed", "steps": [
            _step("light", "turn_on", "light.kitchen"),
            _step("homeassistant", "restart", "lock.front_door"),
        ]},
    ))
    assert out["results"][0]["ok"] is True
    assert out["results"][1]["ok"] is False
    assert hass.service_calls == [("light", "turn_on", {"entity_id": "light.kitchen"})]


def test_allowlist_covers_common_device_domains(agent):
    for d in ("light", "switch", "climate", "cover", "lock", "media_player",
             "fan", "alarm_control_panel", "scene", "script", "automation"):
        assert d in agent._EXECUTE_PLAN_ALLOWED_DOMAINS


def test_allowlist_excludes_system_domains(agent):
    for d in ("homeassistant", "shell_command", "python_script", "backup",
             "hassio", "supervisor", "config", "recorder"):
        assert d not in agent._EXECUTE_PLAN_ALLOWED_DOMAINS
