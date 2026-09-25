"""A hard server-side gate on tool dispatch for scoped sub-agents (found
necessary while building HOMER, Phase 7).

Before this phase, `allowed_tools` only shaped which tool SCHEMAS a sub-agent
was offered (`_scoped_tool_list`) — the actual dispatch loop executed
whatever name `_TOOL_MAP` recognized, with no check that the name was one
the sub-agent was actually granted. That's fine as long as the provider
strictly refuses to emit a tool call outside the schemas it was sent, but
Nova's own task explicitly names "model output" as a possible way to add a
tool, so this can't be the only thing standing between a scoped sub-agent
and `control_device`. This closes that gap in run_agent's own dispatch loop
— a change that benefits every existing capability-based sub-agent, not
just HOMER, since `allowed_tools` already threads through all of them
identically.

Drives the REAL run_agent() with a scripted client (same technique as
test_agent_run_agent_ambiguity_dispatch.py / test_homer_prompt.py) so this
proves actual dispatch behavior, not a re-implementation of it.
"""
import json

import pytest

from fakes import FakeHass, FakeUserInput


@pytest.fixture
def agent(load):
    return load("agent")


class _ScriptedClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools, max_tokens, temperature):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.script:
            return {"text": "done", "tool_calls": []}
        return self.script.pop(0)


def _tc(name, args, call_id="c1"):
    return {"id": call_id, "name": name, "args": args}


async def _run_scoped(agent, monkeypatch, script, allowed_tools):
    client = _ScriptedClient(script)

    async def fake_create_provider(*a, **kw):
        return client

    monkeypatch.setattr(agent, "_create_provider_with_fallback", fake_create_provider)
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})

    hass = FakeHass()
    hass.states.set("light.hallway", "off", friendly_name="Hallway")
    hass.services.register("light", "turn_on")

    result = await agent.run_agent(
        hass, messages=[{"role": "user", "content": "investigate"}],
        persona="p", provider_name="ollama", api_key="", model="m",
        # allowed_tools=None is the main agent: a top-level conversation turn.
        hass_api=None,
        user_input=FakeUserInput() if allowed_tools is None else None, config={},
        allowed_tools=allowed_tools, depth=0 if allowed_tools is None else 1,
    )
    hass.close_pending()
    turn_on_calls = [c for c in hass.service_calls if c[:2] == ("light", "turn_on")]
    return result, client, turn_on_calls


HOMER_TOOLS = {
    "system_diagnostics", "cognitive_status", "connectivity_status",
    "energy_status", "activity_history", "get_entity_state",
    "search_entities", "root_cause",
}


async def test_scoped_subagent_calling_an_ungranted_tool_is_refused(agent, monkeypatch):
    """HOMER's own script tries control_device even though it was never
    offered that tool. The real HA service must never fire."""
    script = [
        {"text": "", "tool_calls": [
            _tc("control_device", {"entity_id": "light.hallway", "action": "turn_on"}),
        ]},
        {"text": "Cause identified.", "tool_calls": []},
    ]
    result, client, turn_on_calls = await _run_scoped(agent, monkeypatch, script, HOMER_TOOLS)

    assert turn_on_calls == []                       # the real HA service never ran
    # The refusal was fed back as a tool result, not silently dropped.
    tool_msgs = [m for m in client.calls[-1]["messages"] if m.get("role") == "tool"]
    assert any("not available" in m.get("content", "") for m in tool_msgs)
    assert result == "Cause identified."


@pytest.mark.parametrize("ungranted_tool,args", [
    ("remember", {"fact": "garage code is 1234"}),
    ("ignore_entity", {"entity_id": "alarm_control_panel.home"}),
    ("bulk_control", {"domain": "lock", "action": "unlock"}),
    ("manage_autonomy", {"action": "revoke"}),
])
async def test_every_ungranted_tool_is_refused_not_executed(agent, monkeypatch, ungranted_tool, args):
    script = [
        {"text": "", "tool_calls": [_tc(ungranted_tool, args)]},
        {"text": "done", "tool_calls": []},
    ]
    result, client, turn_on_calls = await _run_scoped(agent, monkeypatch, script, HOMER_TOOLS)
    tool_msgs = [m for m in client.calls[-1]["messages"] if m.get("role") == "tool"]
    assert any("not available" in m.get("content", "") for m in tool_msgs)
    assert turn_on_calls == []


async def test_a_granted_tool_still_dispatches_normally(agent, monkeypatch):
    """Regression: the new gate must not block a tool the sub-agent WAS
    granted — only ones it wasn't. Uses search_entities rather than
    get_entity_state: the latter reads state.last_changed/last_updated,
    which tests/fakes.py's FakeState doesn't carry (a pre-existing fixture
    gap, noted separately — not something this phase touches)."""
    script = [
        {"text": "", "tool_calls": [_tc("search_entities", {"query": "hallway"})]},
        {"text": "Found the hallway light.", "tool_calls": []},
    ]
    result, client, _ = await _run_scoped(agent, monkeypatch, script, HOMER_TOOLS)
    tool_msgs = [m for m in client.calls[-1]["messages"] if m.get("role") == "tool"]
    payload = json.loads(tool_msgs[-1]["content"])
    assert any(r.get("entity_id") == "light.hallway" for r in payload)
    assert result == "Found the hallway light."


async def test_top_level_agent_unaffected_allowed_tools_none(agent, monkeypatch):
    """The gate only applies when allowed_tools is an actual set (a scoped
    sub-agent). The main agent (allowed_tools=None) dispatches exactly as
    before — this phase changes nothing about the everyday control path."""
    script = [
        {"text": "", "tool_calls": [_tc("control_device", {"entity_id": "light.hallway", "action": "turn_on"})]},
        {"text": "Turned it on.", "tool_calls": []},
    ]
    result, client, turn_on_calls = await _run_scoped(agent, monkeypatch, script, None)
    assert len(turn_on_calls) == 1
    assert result == "Turned it on."


# ── the gate protects EVERY scoped sub-agent, not only HOMER ───────────────

async def test_gate_also_protects_a_generic_capability_subagent(agent, monkeypatch):
    """A plain capability-group sub-agent (e.g. 'scheduling', nothing to do
    with HOMER) gets the exact same execution-time protection — the fix
    lives in run_agent's dispatch loop, keyed only on allowed_tools, not on
    which profile or capability produced it."""
    scheduling_tools = agent._resolve_capability("scheduling")
    assert "control_device" not in scheduling_tools  # sanity: genuinely ungranted

    script = [
        {"text": "", "tool_calls": [
            _tc("control_device", {"entity_id": "light.hallway", "action": "turn_on"}),
        ]},
        {"text": "done", "tool_calls": []},
    ]
    result, client, turn_on_calls = await _run_scoped(agent, monkeypatch, script, scheduling_tools)
    assert turn_on_calls == []
    tool_msgs = [m for m in client.calls[-1]["messages"] if m.get("role") == "tool"]
    assert any("not available" in m.get("content", "") for m in tool_msgs)


async def test_gate_error_message_does_not_leak_internal_detail(agent, monkeypatch):
    """The refusal names the tool the model itself already asked for (so it
    can adjust), but nothing beyond that — no allowlist contents, no
    denylist, no internal set contents."""
    script = [
        {"text": "", "tool_calls": [_tc("remember", {"fact": "x"})]},
        {"text": "done", "tool_calls": []},
    ]
    _, client, _ = await _run_scoped(agent, monkeypatch, script, HOMER_TOOLS)
    tool_msgs = [m for m in client.calls[-1]["messages"] if m.get("role") == "tool"]
    refusal = next(m["content"] for m in tool_msgs if "not available" in m.get("content", ""))
    payload = json.loads(refusal)
    assert set(payload.keys()) == {"error"}
    assert "remember" in payload["error"]
    for leaked in HOMER_TOOLS:
        assert leaked not in payload["error"]
