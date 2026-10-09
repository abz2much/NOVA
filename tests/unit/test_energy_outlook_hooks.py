"""The energy outlook's chat tool and proactive hook.

The tool: offered, registered, classified read only, granted to the main and
headless agents but not HOMER, and it returns the outlook as JSON.

The hook (energy.evaluate_outlook_for_proactive): one offer at most, never
an action (auto_act False, no action_data, no service call at any agency
level), cooldowns per advice key, the output gate respected, and core_runtime
calls it only inside the same proactive gate as the energy shed offers."""
import ast
import json
import pathlib

import pytest

from fakes import FakeHass

ROOT = pathlib.Path(__file__).parents[2] / "custom_components" / "nova"


def _outlook(*advice, **kw):
    out = {"configured": True, "error": False, "status": "ok", "advice": list(advice)}
    out.update(kw)
    return out


TOPUP = {"kind": "cheap_topup", "key": "cheap_topup:2026-10-14", "level": "suggest",
         "message": "Tomorrow looks dull. Charging about 9.0 kWh between 02:00 and 06:00 "
                    "would save around 2.10."}
HOLD = {"kind": "battery_hold", "key": "battery_hold:2026-10-14", "level": "suggest",
        "message": "Power costs more from 17:00 to 19:00."}


# ── the chat tool ──────────────────────────────────────────────────────────

def test_tool_is_offered_registered_and_read_only(load):
    agent = load("agent")
    registry = load("agent_runtime.registry")
    spec = next(t for t in agent.NOVA_TOOLS if t["function"]["name"] == "energy_outlook")
    desc = spec["function"]["description"]
    for question in ("should I charge the battery tonight?",
                     "when is the cheapest time to run the dishwasher?",
                     "will I run out of battery today?"):
        assert question in desc
    assert set(spec["function"]["parameters"]["properties"]) == {"hours"}
    row = registry.TOOL_REGISTRY["energy_outlook"]
    assert row.capability == "environment"
    assert (row.mutates, row.persists, row.network) == (False, False, False)
    assert row.executor is agent._TOOL_MAP["energy_outlook"]


def test_tool_is_granted_like_energy_report(load):
    grants = load("agent_runtime.grants")
    assert "energy_outlook" in grants.HEADLESS_TOOLS
    assert grants.MAIN_GRANT.allows("energy_outlook")
    for name in ("energy_report", "energy_outlook"):
        assert not any(name in tools for tools in grants.CAPABILITY_GROUPS.values())


@pytest.mark.parametrize("args, hours", [({}, 2), ({"hours": 3}, 3), ({"hours": "x"}, 2),
                                         ({"hours": 99}, 12)])
async def test_tool_returns_the_outlook_as_json(load, monkeypatch, args, hours):
    eo = load("energy_outlook")
    env = load("agent_runtime.capabilities.environment")
    seen = []

    async def status(hass, hours=2):
        seen.append(hours)
        return _outlook(TOPUP, best_window={"start": "x", "end": "y"})
    monkeypatch.setattr(eo, "energy_outlook_status", status)
    out = json.loads(await env._exec_energy_outlook(FakeHass(), args))
    assert out["advice"][0]["kind"] == "cheap_topup" and seen == [hours]


async def test_tool_failure_is_json_not_a_raise(load, monkeypatch):
    eo = load("energy_outlook")
    env = load("agent_runtime.capabilities.environment")

    async def status(hass, hours=2):
        raise RuntimeError("boom")
    monkeypatch.setattr(eo, "energy_outlook_status", status)
    assert json.loads(await env._exec_energy_outlook(FakeHass(), {})) == {"error": "boom"}


# ── the proactive hook ─────────────────────────────────────────────────────

@pytest.fixture
def hook(load, monkeypatch):
    energy = load("energy")
    eo = load("energy_outlook")
    gate = load("output_gate")
    energy._outlook_offered.clear()
    state = {"outlook": _outlook(TOPUP), "allowed": True, "recorded": [], "clock": 1000.0}

    async def status(hass, hours=2):
        return state["outlook"]

    def can_announce(**kw):
        return state["allowed"], "muted"
    monkeypatch.setattr(eo, "energy_outlook_status", status)
    monkeypatch.setattr(gate, "can_announce", can_announce)
    monkeypatch.setattr(gate, "record_announcement", lambda **kw: state["recorded"].append(kw))
    import time
    monkeypatch.setattr(time, "time", lambda: state["clock"])
    yield energy, state
    energy._outlook_offered.clear()


@pytest.mark.parametrize("agency", ["advisory", "opt_in", "autonomous"])
async def test_offer_never_acts_at_any_agency(hook, monkeypatch, agency):
    energy, state = hook
    monkeypatch.setattr(energy, "effective_agency", lambda: agency)
    hass = FakeHass()
    offer = await energy.evaluate_outlook_for_proactive(hass)
    assert offer["type"] == "energy_outlook_advice"
    assert offer["urgency"] == "low" and offer["auto_act"] is False
    assert "action_data" not in offer and "offer_key" not in offer and "pattern_key" not in offer
    assert "02:00 and 06:00" in offer["message"]
    assert hass.service_calls == []


async def test_topup_once_a_day_others_every_six_hours(hook):
    energy, state = hook
    state["outlook"] = _outlook(TOPUP, HOLD)
    first = await energy.evaluate_outlook_for_proactive(FakeHass())
    second = await energy.evaluate_outlook_for_proactive(FakeHass())
    assert "02:00" in first["message"] and "17:00" in second["message"]
    assert await energy.evaluate_outlook_for_proactive(FakeHass()) is None
    state["clock"] += energy.OUTLOOK_COOLDOWN_S - 1
    assert await energy.evaluate_outlook_for_proactive(FakeHass()) is None
    state["clock"] += 2
    again = await energy.evaluate_outlook_for_proactive(FakeHass())
    assert "17:00" in again["message"]                      # hold again, never top up
    assert await energy.evaluate_outlook_for_proactive(FakeHass()) is None


async def test_output_gate_suppresses_and_uses_the_cooldown(hook):
    energy, state = hook
    state["allowed"] = False
    assert await energy.evaluate_outlook_for_proactive(FakeHass()) is None
    assert state["recorded"][0]["was_spoken"] is False
    assert state["recorded"][0]["category"] == "energy"
    state["allowed"] = True
    assert await energy.evaluate_outlook_for_proactive(FakeHass()) is None   # not every tick


@pytest.mark.parametrize("outlook", [
    {"configured": True, "error": True, "advice": [TOPUP]},
    {"configured": False, "error": False, "advice": [TOPUP]},
    _outlook(),
    _outlook({"kind": "best_window", "key": "best_window:x", "message": "x"}),
])
async def test_no_offer_on_error_unconfigured_or_no_advice(hook, outlook):
    energy, state = hook
    state["outlook"] = outlook
    assert await energy.evaluate_outlook_for_proactive(FakeHass()) is None


async def test_hook_never_raises(hook, load, monkeypatch):
    energy, _ = hook
    eo = load("energy_outlook")

    async def boom(hass, hours=2):
        raise RuntimeError("boom")
    monkeypatch.setattr(eo, "energy_outlook_status", boom)
    assert await energy.evaluate_outlook_for_proactive(FakeHass()) is None


def test_core_runtime_calls_the_hook_inside_the_proactive_gate():
    """Gating off means no call: the hook sits inside `if proactive_enabled:`,
    the same block as the energy shed offers."""
    tree = ast.parse((ROOT / "core_runtime.py").read_text())
    tick = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_tick")
    gated = [n for n in ast.walk(tick) if isinstance(n, ast.If)
             and ast.unparse(n.test) == "proactive_enabled"]
    inside = [ast.unparse(n) for g in gated for n in ast.walk(g) if isinstance(n, ast.Call)]
    assert any("energy.evaluate_outlook_for_proactive" in c for c in inside)
    assert any("energy.evaluate_for_proactive" in c for c in inside)
    every = [ast.unparse(n) for n in ast.walk(tick) if isinstance(n, ast.Call)]
    assert sum("evaluate_outlook_for_proactive" in c for c in every) == 1
