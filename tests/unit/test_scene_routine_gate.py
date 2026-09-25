"""Regression tests: the confirmation gate is actually WIRED IN at the real
call sites, not just present in the classifier.

Context (Sept 2026 audit): policy.classify() and voice_confirm.action_is_protected()
correctly rated scene/script/automation activation as needing confirmation, but
neither agent._exec_run_scene_script nor routines.async_run_routine ever called
policy.confirm_gate() / policy.requires_confirmation() -- the classification had
no enforcement point, so the fix changed nothing for those two call sites in
practice. These tests exercise the call sites themselves (mocking policy's gate
functions), not the classifier logic (covered in tests/test_policy.py).
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def agent(load):
    return load("agent")


@pytest.fixture
def routines(load):
    return load("routines")


@pytest.fixture
def policy(load):
    return load("policy")


# -- agent._exec_run_scene_script --------------------------------------------

async def test_run_scene_script_blocked_when_gate_denies(agent, policy, monkeypatch, fake_hass):
    calls = []

    async def fake_confirm_gate(hass, domain, service, entity_id="", action_label="",
                                device_id="", target_name=""):
        calls.append((domain, service, entity_id))
        return False, "asked for spoken confirmation; not yet confirmed", "rejected"

    monkeypatch.setattr(policy, "confirm_gate", fake_confirm_gate)

    result = json.loads(await agent._exec_run_scene_script(
        fake_hass, {"entity_id": "script.unlock_garage_and_disarm"}))

    assert result["status"] == "awaiting_confirmation"
    assert calls == [("script", "turn_on", "script.unlock_garage_and_disarm")]
    # The load-bearing assertion: the gate being consulted must actually stop
    # the service call. Before the fix, this list would contain the activation
    # regardless of what confirm_gate said (because nothing called it).
    assert fake_hass.service_calls == []


async def test_run_scene_script_runs_when_gate_allows(agent, policy, monkeypatch, fake_hass):
    async def fake_confirm_gate(hass, domain, service, entity_id="", action_label="",
                                device_id="", target_name=""):
        return True, "", "not_required"

    monkeypatch.setattr(policy, "confirm_gate", fake_confirm_gate)

    result = json.loads(await agent._exec_run_scene_script(
        fake_hass, {"entity_id": "scene.morning"}))

    assert result["success"] is True
    assert fake_hass.service_calls == [("scene", "turn_on", {"entity_id": "scene.morning"})]


async def test_run_scene_script_uses_trigger_for_automation(agent, policy, monkeypatch, fake_hass):
    async def fake_confirm_gate(hass, domain, service, entity_id="", action_label="",
                                device_id="", target_name=""):
        return True, "", "not_required"

    monkeypatch.setattr(policy, "confirm_gate", fake_confirm_gate)

    await agent._exec_run_scene_script(fake_hass, {"entity_id": "automation.night_mode"})

    assert fake_hass.service_calls == [
        ("automation", "trigger", {"entity_id": "automation.night_mode"})]


# -- routines.async_run_routine -----------------------------------------------

class _Call:
    """Minimal ServiceCall stand-in -- async_run_routine only reads .data."""
    def __init__(self, data):
        self.data = data


async def test_routine_step_blocked_when_confirmation_required_and_denied(
        routines, policy, monkeypatch, fake_hass):
    monkeypatch.setattr(policy, "requires_confirmation", lambda hass, d, s, e="": True)

    async def fake_confirm_gate(hass, domain, service, entity_id="", action_label="",
                                device_id="", target_name=""):
        return False, "not yet confirmed", "rejected"

    monkeypatch.setattr(policy, "confirm_gate", fake_confirm_gate)

    monkeypatch.setattr(routines, "_load_routines", lambda: {
        "test_routine": [
            {"service": "scene.turn_on", "data": {"entity_id": "scene.welcome"}},
        ]
    })

    result = await routines.async_run_routine(
        fake_hass, _Call({"name": "test_routine"}), "sir", None, [])

    assert result["success"] is False
    assert result["steps_executed"] == 0
    assert fake_hass.service_calls == []  # step must not have actually run


async def test_routine_step_runs_when_not_protected(routines, policy, monkeypatch, fake_hass):
    monkeypatch.setattr(policy, "requires_confirmation", lambda hass, d, s, e="": False)

    async def fail_confirm_gate(*a, **k):  # must not even be called
        raise AssertionError("confirm_gate should be skipped when not protected")

    monkeypatch.setattr(policy, "confirm_gate", fail_confirm_gate)

    monkeypatch.setattr(routines, "_load_routines", lambda: {
        "test_routine": [
            {"service": "light.turn_off", "target": {"entity_id": "all"}},
        ]
    })

    result = await routines.async_run_routine(
        fake_hass, _Call({"name": "test_routine"}), "sir", None, [])

    assert result["success"] is True
    assert result["steps_executed"] == 1
    assert fake_hass.service_calls == [("light", "turn_off", {})]


async def test_routine_optional_protected_step_skipped_not_errored(
        routines, policy, monkeypatch, fake_hass):
    """An optional step (e.g. the default goodnight scene) that needs
    confirmation is skipped quietly, not counted as a routine failure."""
    monkeypatch.setattr(policy, "requires_confirmation", lambda hass, d, s, e="": True)

    async def fake_confirm_gate(hass, domain, service, entity_id="", action_label="",
                                device_id="", target_name=""):
        return False, "not yet confirmed", "rejected"

    monkeypatch.setattr(policy, "confirm_gate", fake_confirm_gate)

    monkeypatch.setattr(routines, "_load_routines", lambda: {
        "test_routine": [
            {"service": "scene.turn_on", "data": {"entity_id": "scene.nighttime"},
             "optional": True},
        ]
    })

    result = await routines.async_run_routine(
        fake_hass, _Call({"name": "test_routine"}), "sir", None, [])

    assert result["success"] is True   # optional block doesn't count as an error
    assert result["steps_executed"] == 0
    assert fake_hass.service_calls == []
