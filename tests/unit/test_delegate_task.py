"""Ephemeral sub-agents (delegate_task, v6.83.0).

run_agent is reentrant by inspection: it builds its messages in locals,
dispatches through a stateless _TOOL_MAP and keeps no mutable module state.
These tests cover what surrounds it: resolving a capability or profile and the
deny list, narrowing the tool list, the depth cap, the exact replies, and how
the nested run is called, watched through a spy on run_agent.
"""
import json
import logging

import pytest


@pytest.fixture
def agent(load):
    return load("agent")


# ── capabilities and the deny list ───────────────────────────────────────────

def test_a_known_capability_resolves_to_its_tools(agent):
    tools = agent._resolve_capability("scheduling")
    assert {"calendar_agenda", "read_email"} <= tools


@pytest.mark.parametrize("name", ["nonsense", ""])
def test_an_unknown_capability_resolves_to_nothing(agent, name):
    assert agent._resolve_capability(name) == set()


def test_no_group_can_hand_out_a_denied_tool(agent):
    for name in agent.CAPABILITY_GROUPS:
        leaked = agent._resolve_capability(name) & agent._SUBAGENT_DENY
        assert not leaked, f"{name} leaks a denied tool"


def test_the_deny_list_wins_even_when_a_group_names_a_denied_tool(agent, monkeypatch):
    monkeypatch.setitem(agent.CAPABILITY_GROUPS, "rogue",
                        {"calendar_agenda", "control_device"})
    resolved = agent._resolve_capability("rogue")
    assert "calendar_agenda" in resolved and "control_device" not in resolved


# ── narrowing the tool list ──────────────────────────────────────────────────

def test_no_scope_offers_every_tool(agent):
    assert len(agent._scoped_tool_list(None)) == len(agent.NOVA_TOOLS)


def test_a_scope_offers_only_its_tools(agent):
    scoped = agent._scoped_tool_list({"read_email", "calendar_agenda"})
    assert {t["function"]["name"] for t in scoped} == {"read_email", "calendar_agenda"}


def test_a_sub_agent_can_never_be_offered_delegate_task(agent):
    # It sits in no group and on the deny list, so a sub-agent cannot hand work
    # on again. The depth cap is a second guard.
    for name in agent.CAPABILITY_GROUPS:
        offered = agent._scoped_tool_list(agent._resolve_capability(name))
        assert "delegate_task" not in {t["function"]["name"] for t in offered}


def test_the_tool_exists_but_is_handled_inline(agent):
    assert "delegate_task" in {t["function"]["name"] for t in agent.NOVA_TOOLS}
    assert "delegate_task" not in agent._TOOL_MAP


# ── running a delegated objective ────────────────────────────────────────────

@pytest.fixture
def nested(agent, monkeypatch):
    runs = []

    async def fake_run_agent(hass, **kw):
        runs.append(kw)
        return "sub-agent answer"

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    return runs


async def _delegate(agent, args, depth=0, **over):
    spec = dict(persona="p", provider_name="ollama", api_key="", model="gemma4:26b",
                base_url=None, config={}, depth=depth)
    spec.update(over)
    return await agent._run_delegated(None, args, **spec)


async def test_a_capability_run_hands_a_narrow_job_to_a_nested_agent(agent, nested):
    out = json.loads(await _delegate(
        agent, {"objective": "check my week", "capability": "scheduling"}))
    assert out == {"objective": "check my week", "result": "sub-agent answer",
                   "capability": "scheduling"}
    (kw,) = nested
    assert kw["depth"] == 1
    assert kw["allowed_tools"] == agent._resolve_capability("scheduling")
    assert kw["max_iterations"] == agent._DELEGATION_MAX_TURNS
    assert kw["messages"] == [{"role": "user", "content": "check my week"}]
    assert (kw["persona"], kw["provider_name"], kw["api_key"], kw["model"]) == (
        "p", "ollama", "", "gemma4:26b")
    assert kw["base_url"] is None and kw["config"] == {}
    assert kw["profile_directive"] is None


async def test_the_nested_depth_is_one_more_than_the_callers(agent, nested):
    await _delegate(agent, {"objective": "x", "capability": "inbox"}, depth=0)
    assert nested[0]["depth"] == 1


async def test_the_depth_cap_stops_a_sub_agent_delegating_further(agent, nested):
    out = json.loads(await _delegate(
        agent, {"objective": "x", "capability": "scheduling"},
        depth=agent.MAX_DELEGATION_DEPTH))
    assert out == {"error": "delegation depth limit reached — a sub-agent cannot "
                            "delegate further; handle this directly"}
    assert nested == []


@pytest.mark.parametrize("objective", ["  ", "", None])
async def test_an_objective_is_required(agent, nested, objective):
    args = {"capability": "scheduling"}
    if objective is not None:
        args["objective"] = objective
    out = json.loads(await _delegate(agent, args))
    assert out == {"error": "delegate_task needs an objective"}
    assert nested == []


async def test_the_objective_is_checked_before_the_depth(agent, nested):
    out = json.loads(await _delegate(agent, {"capability": "scheduling"},
                                     depth=agent.MAX_DELEGATION_DEPTH))
    assert out == {"error": "delegate_task needs an objective"}


async def test_an_unknown_capability_lists_the_real_ones(agent, nested):
    out = json.loads(await _delegate(agent, {"objective": "x", "capability": "bogus"}))
    assert out == {"error": "unknown capability 'bogus'. Options: "
                            + ", ".join(sorted(agent.CAPABILITY_GROUPS))}
    assert "diagnostics" not in out["error"]
    assert nested == []


async def test_no_capability_at_all_is_unknown(agent, nested):
    out = json.loads(await _delegate(agent, {"objective": "x"}))
    assert out["error"].startswith("unknown capability ''. Options: ")


@pytest.mark.parametrize("asked, expected", [
    (999, "cap"), (3, 3), ("2", 2), (0, "cap"), (-4, 1), ("junk", "cap"), (None, "cap")])
async def test_the_turn_limit_is_kept_between_one_and_the_cap(agent, nested, asked, expected):
    await _delegate(agent, {"objective": "x", "capability": "research", "max_turns": asked})
    want = agent._DELEGATION_MAX_TURNS if expected == "cap" else expected
    assert nested[0]["max_iterations"] == want


async def test_the_objective_and_capability_are_trimmed(agent, nested):
    out = json.loads(await _delegate(
        agent, {"objective": "  look  ", "capability": " research "}))
    assert out["objective"] == "look" and out["capability"] == "research"
    assert nested[0]["messages"][0]["content"] == "look"


# ── named profiles ───────────────────────────────────────────────────────────

async def test_a_profile_gets_its_own_tools_cap_and_directive(agent, nested):
    out = json.loads(await _delegate(agent, {"objective": "why is it down", "profile": "homer"}))
    assert out == {"objective": "why is it down", "result": "sub-agent answer",
                   "profile": "HOMER"}
    (kw,) = nested
    tools, cap, label, directive = agent._resolve_profile("homer")
    assert kw["allowed_tools"] == tools and kw["profile_directive"] == directive
    assert kw["max_iterations"] == cap == 4


async def test_a_profile_name_is_matched_whatever_the_case(agent, nested):
    out = json.loads(await _delegate(agent, {"objective": "x", "profile": " HOMER "}))
    assert out["profile"] == "HOMER"


async def test_a_profile_beats_a_capability_named_alongside_it(agent, nested):
    out = json.loads(await _delegate(
        agent, {"objective": "x", "profile": "homer", "capability": "scheduling"}))
    assert out["profile"] == "HOMER" and "capability" not in out
    assert nested[0]["allowed_tools"] == agent._resolve_profile("homer")[0]


async def test_an_unknown_profile_is_refused_and_never_falls_back(agent, nested):
    out = json.loads(await _delegate(
        agent, {"objective": "x", "profile": "marvin", "capability": "scheduling"}))
    assert out == {"error": "unknown profile 'marvin'. Options: "
                            + ", ".join(sorted(agent.AGENT_PROFILES))}
    assert nested == []


@pytest.mark.parametrize("asked, expected", [(2, 2), (50, 4), (0, 4), (-1, 1), ("x", 4)])
async def test_a_profile_turn_limit_never_passes_its_own_cap(agent, nested, asked, expected):
    await _delegate(agent, {"objective": "x", "profile": "homer", "max_turns": asked})
    assert nested[0]["max_iterations"] == expected


async def test_the_diagnostics_alias_gives_the_homer_grant_and_keeps_its_old_field(
        agent, nested):
    out = json.loads(await _delegate(agent, {"objective": "x", "capability": "diagnostics"}))
    assert out == {"objective": "x", "result": "sub-agent answer", "profile": "HOMER",
                   "capability": "diagnostics"}
    tools, cap, label, directive = agent._resolve_profile("homer")
    assert nested[0]["allowed_tools"] == tools
    assert nested[0]["profile_directive"] == directive and nested[0]["max_iterations"] == cap


# ── a failing nested run ─────────────────────────────────────────────────────

async def test_a_failing_sub_agent_gives_a_fixed_message_and_logs_the_detail(
        agent, monkeypatch, caplog):
    async def boom(hass, **kw):
        raise RuntimeError("provider said: key sk-secret rejected")

    monkeypatch.setattr(agent, "run_agent", boom)
    with caplog.at_level(logging.WARNING, logger="jc.agent"):
        out = await _delegate(agent, {"objective": "x", "capability": "research"})
    assert json.loads(out) == {
        "error": "sub-agent failed — could not complete the delegated objective"}
    assert "sk-secret" not in out
    assert [r.getMessage() for r in caplog.records] == [
        "delegate_task sub-agent failed: provider said: key sk-secret rejected"]
