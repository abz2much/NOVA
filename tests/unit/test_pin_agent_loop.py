"""Pin what the agent's turn loop does when the model or the provider
misbehaves (8.7.19, tests only).

agent_runtime/loop.py turns model output into tool calls. The grant,
deferral and ambiguity paths are already pinned elsewhere
(test_agent_runtime_boundaries.py, test_agent_run_agent_ambiguity_dispatch.py).
This file pins the recovery paths that decide what runs and what the person
is told: the one-shot slim retry on a too-large request, the malformed tool
call retries, the provider fallback, and the reply after the iteration cap.
A scripted fake client stands in for the provider; nothing leaves the test.
Tests named test_current_behaviour_* pin behaviour that looks wrong.
"""
from __future__ import annotations

import pytest

from fakes import FakeHass, FakeUserInput


class _Client:
    name = "fake"
    model = "m"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools=None, max_tokens=512, temperature=0.7):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        item = self.script.pop(0) if self.script else {"text": "done", "tool_calls": []}
        if isinstance(item, BaseException):
            raise item
        return item


def _tc(name, args, call_id="c1"):
    return {"id": call_id, "name": name, "args": args}


def _tool_names(call):
    return None if call["tools"] is None else {t["function"]["name"] for t in call["tools"]}


@pytest.fixture
def loop(load, monkeypatch):
    mod = load("agent_runtime.loop")
    monkeypatch.setattr(load("agent"), "_load_learned", lambda: {"alias": {}})
    return mod


async def _run(loop, monkeypatch, script, *, fallback=None, config=None, user_input=True):
    client = _Client(script)

    async def primary(*a, **k):
        return client
    monkeypatch.setattr(loop, "_create_provider_with_fallback", primary)
    tiers = []

    async def tier(self, cfg, name):
        tiers.append(name)
        if fallback is None:
            raise RuntimeError("no tier")
        return fallback
    monkeypatch.setattr(loop._TurnProviders, "tier", tier)

    hass = FakeHass()
    hass.states.set("lock.front_door", "locked", friendly_name="Front Door")
    result = await loop.run_agent(
        hass, messages=[{"role": "user", "content": "lock up"}], persona="P",
        provider_name="fake", api_key="", model="m",
        user_input=FakeUserInput("lock up") if user_input else None,
        config={} if config is None else config)
    hass.close_pending()
    return result, client, hass, tiers


# ── a request too large for the provider ────────────────────────────────────

async def test_a_too_large_request_is_retried_once_with_the_slim_tool_set(loop, monkeypatch):
    result, client, hass, _ = await _run(loop, monkeypatch, [
        RuntimeError("Error code: 413 - request too large for model"),
        {"text": "All locked.", "tool_calls": []},
    ])
    assert result == "All locked."
    assert len(_tool_names(client.calls[0])) > len(loop._SLIM_TOOLS)
    assert _tool_names(client.calls[1]) == set(loop._SLIM_TOOLS)
    retry_system = client.calls[1]["messages"][0]["content"]
    assert "## Tools available for this reply" in retry_system
    assert "control_device" in loop._SLIM_TOOLS          # the slim set can still act


async def test_a_second_too_large_error_is_not_retried_slim_again(loop, monkeypatch):
    result, client, _h, tiers = await _run(loop, monkeypatch, [
        RuntimeError("413 request too large"), RuntimeError("413 request too large")])
    assert "connectivity issues" in result
    assert len(client.calls) == 2 and tiers == []        # no config: no fallback tier tried


async def test_a_scheduled_run_is_never_slim_retried(loop, monkeypatch):
    # The slim retry is only for the main grant; a headless run goes straight
    # to the fallback path.
    result, client, _h, _t = await _run(loop, monkeypatch, [
        RuntimeError("413 request too large")], user_input=False)
    assert "connectivity issues" in result and len(client.calls) == 1


# ── malformed tool calls ────────────────────────────────────────────────────

async def test_a_malformed_tool_call_is_retried_with_tools_then_answered_without(loop, monkeypatch):
    bad = RuntimeError("Error code: 400 - tool_use_failed: Failed to call a function")
    result, client, hass, _ = await _run(loop, monkeypatch, [
        bad, bad, {"text": "Sorry, say that again?", "tool_calls": []}])
    assert result == "Sorry, say that again?"
    assert [c["tools"] is not None for c in client.calls] == [True, True, False]
    assert hass.service_calls == []


async def test_a_malformed_call_then_a_tool_call_runs_that_tool(loop, monkeypatch):
    bad = RuntimeError("tool_use_failed")
    result, client, hass, _ = await _run(loop, monkeypatch, [
        bad,
        {"text": "", "tool_calls": [_tc("control_device", {"entity_id": "lock.front_door",
                                                          "action": "lock"})]},
        {"text": "Locked.", "tool_calls": []}])
    assert result == "Locked."
    assert hass.service_calls == [("lock", "lock", {"entity_id": "lock.front_door"})]


async def test_a_malformed_call_then_a_connection_error_is_reported_as_connectivity(loop, monkeypatch):
    result, _c, hass, _ = await _run(loop, monkeypatch, [
        RuntimeError("tool_use_failed"), RuntimeError("connection timed out")])
    assert "connectivity issues" in result and hass.service_calls == []


# ── provider fallback ───────────────────────────────────────────────────────

async def test_a_failing_primary_hands_the_turn_to_the_reasoning_tier(loop, monkeypatch):
    fallback = _Client([{"text": "Fallback here.", "tool_calls": []}])
    result, client, _h, tiers = await _run(
        loop, monkeypatch, [RuntimeError("503 service unavailable")],
        fallback=fallback, config={"llm_provider": "fake"})
    assert result == "Fallback here." and tiers == ["reasoning"]
    # The fallback sees the same conversation and the same tools.
    assert fallback.calls[0]["messages"] == client.calls[0]["messages"]
    assert _tool_names(fallback.calls[0]) == _tool_names(client.calls[0])


async def test_with_no_fallback_the_person_is_told_and_nothing_runs(loop, monkeypatch):
    result, _c, hass, tiers = await _run(
        loop, monkeypatch, [RuntimeError("503 service unavailable")],
        config={"llm_provider": "fake"})
    assert "connectivity issues" in result and tiers == ["reasoning"]
    assert hass.service_calls == []


# ── the iteration cap ───────────────────────────────────────────────────────

async def test_current_behaviour_after_the_cap_a_failed_summary_reports_success(
        loop, load, monkeypatch):
    # Looks wrong: the model asks for an action ten times and every one fails
    # (the entity does not exist). The summary call then fails too, and the
    # loop falls back to persona.completed(), which tells the person the
    # requested actions were completed when none were.
    monkeypatch.setattr(load("persona"), "completed",
                        lambda honorific="sir", register="neutral": "COMPLETED-LINE")
    attempt = {"text": "", "tool_calls": [_tc("control_device", {"entity_id": "lock.garden",
                                                                "action": "lock"})]}
    script = [attempt] * loop.MAX_TOOL_ITERATIONS + [RuntimeError("summary failed")]
    result, client, hass, _ = await _run(loop, monkeypatch, script)
    assert result == "COMPLETED-LINE"
    assert hass.service_calls == []                       # nothing was locked
    tool_msgs = [m for m in client.calls[-1]["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == loop.MAX_TOOL_ITERATIONS
    assert all("not found" in m["content"] for m in tool_msgs)


async def test_after_the_cap_the_summary_is_asked_for_without_tools(loop, monkeypatch):
    attempt = {"text": "", "tool_calls": [_tc("get_entity_state",
                                              {"entity_id": "lock.front_door"})]}
    script = [attempt] * loop.MAX_TOOL_ITERATIONS + [{"text": "I checked the lock.",
                                                      "tool_calls": []}]
    result, client, _h, _ = await _run(loop, monkeypatch, script)
    assert result == "I checked the lock."
    assert client.calls[-1]["tools"] is None
    assert client.calls[-1]["messages"][-1] == {"role": "user",
                                                "content": "Summarize what you've done briefly."}
