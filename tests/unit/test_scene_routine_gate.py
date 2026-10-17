"""Regression tests: the confirmation gate is actually WIRED IN at the real
call sites, not just present in the classifier.

Context (Sept 2026 audit): policy.classify() and voice_confirm.action_is_protected()
correctly rated scene/script/automation activation as needing confirmation, but
agent._exec_run_scene_script never called policy.confirm_gate() /
policy.requires_confirmation() -- the classification had no enforcement point.
(routines.async_run_routine had the same gap; that service was removed in 8.59.0.)
These tests exercise the call site itself (mocking policy's gate functions),
not the classifier logic (covered in tests/test_policy.py).
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def agent(load):
    return load("agent")


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
