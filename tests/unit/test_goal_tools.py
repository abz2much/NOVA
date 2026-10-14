"""Agent tool wiring for goals: the create/update/cancel tools are registered
and round-trip through the agent. The goals module itself is covered by
test_goals.py."""
import sys
import types
from datetime import datetime

import pytest

if "homeassistant.helpers.llm" not in sys.modules:      # for the agent load
    _llm = types.ModuleType("homeassistant.helpers.llm")
    _llm.async_get_api = lambda *a, **k: None
    sys.modules["homeassistant.helpers.llm"] = _llm

NOW = datetime(2026, 7, 12, 9, 0, 0)


@pytest.fixture
def goals(load, tmp_path, monkeypatch):
    mod = load("goals")
    monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "patterns.db"))
    return mod


@pytest.fixture
def quiet_activity(load, monkeypatch):
    db = load("database")
    sink = []
    monkeypatch.setattr(db, "save_activity", lambda **kw: sink.append(kw))
    return sink


def _mk(goals, **kw):
    args = dict(title="Guest prep", outcome="house ready for guests",
                steps=["tidy living room", "stock fridge"], now=NOW)
    args.update(kw)
    return goals.create(args.pop("title"), args.pop("outcome"),
                        args.pop("steps"), **args)


# ── lifecycle ────────────────────────────────────────────────────────────────

def test_goal_tools_registered(load):
    agent = load("agent")
    names = {t["function"]["name"] for t in agent.NOVA_TOOLS}
    assert {"create_goal", "update_goal", "manage_goals"} <= names
    for n in ("create_goal", "update_goal", "manage_goals"):
        assert n in agent._TOOL_MAP


async def test_exec_roundtrip(load, goals, fake_hass):
    agent = load("agent")
    out = await agent._exec_create_goal(fake_hass, {
        "title": "Warm den", "outcome": "den at 72F",
        "steps": ["set thermostat", "confirm temperature"]})
    assert "Goal #1 opened" in out and "confirm temperature" in out
    listing = await agent._exec_manage_goals(fake_hass, {"action": "list"})
    assert "Warm den" in listing and "0/2" in listing
    upd = await agent._exec_update_goal(fake_hass, {
        "goal_id": 1, "step_updates": [{"n": 1, "status": "done"}],
        "progress_note": "thermostat set"})
    assert "recorded" in upd
    status = await agent._exec_manage_goals(fake_hass,
                                            {"action": "status", "goal_id": 1})
    assert "[done] 1." in status and "thermostat set" in status
    assert "cancelled" in await agent._exec_manage_goals(
        fake_hass, {"action": "cancel", "goal_id": 1})
