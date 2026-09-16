"""Phase 3 — run_agent's tool-dispatch loop: unique-target ambiguity
interception. Drives the REAL run_agent() loop with a scripted fake LLM
client (client.chat returns a pre-programmed {"text", "tool_calls"} per
call), injected via _create_provider_with_fallback -- the same seam
run_agent itself uses to obtain a client, so this exercises the real
dispatch/defer/clarification logic, not a re-implementation of it.
"""
import json

import pytest

from fakes import FakeHass


@pytest.fixture
def agent(load):
    return load("agent")


class FakeChatClient:
    """Each entry in `script` is the dict client.chat() returns for that
    call, in order. `calls` records every (messages, tools) pair seen, so
    tests can assert whether a second LLM call happened."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools, max_tokens, temperature):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.script:
            return {"text": "done", "tool_calls": []}
        return self.script.pop(0)


def _tool_call(name, args, call_id="c1"):
    return {"id": call_id, "name": name, "args": args}


async def _run(agent, monkeypatch, script, service_sink=None):
    client = FakeChatClient(script)

    async def fake_create_provider(*a, **kw):
        return client

    monkeypatch.setattr(agent, "_create_provider_with_fallback", fake_create_provider)
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})

    hass = FakeHass()
    hass.states.set("light.office", "off", friendly_name="Office")
    hass.states.set("light.office_desk", "off", friendly_name="Office Desk")
    hass.states.set("light.kitchen", "off", friendly_name="Kitchen")

    result = await agent.run_agent(
        hass, messages=[], persona="p", provider_name="ollama",
        api_key="", model="m", hass_api=None, user_input=None, config={},
    )
    hass.close_pending()  # any scheduled background verify task, unrelated to this test
    return result, client, hass


async def test_multi_result_discovery_unchanged(agent, monkeypatch):
    """A plain (non-require_unique) search_entities call, even alongside a
    mutating call in the same batch, is never deferred and never triggers
    ambiguity handling -- discovery mode is untouched."""
    script = [
        {"text": "", "tool_calls": [
            _tool_call("search_entities", {"query": "office light"}, "c1"),
        ]},
        {"text": "Found them.", "tool_calls": []},
    ]
    result, client, hass = await _run(agent, monkeypatch, script)
    assert result == "Found them."
    assert len(client.calls) == 2  # normal two-iteration flow, no early stop


async def test_ambiguous_unique_search_returns_clarification_without_second_llm_call(
    agent, monkeypatch,
):
    script = [
        {"text": "", "tool_calls": [
            _tool_call("search_entities", {"query": "office light", "require_unique": True}, "c1"),
        ]},
        {"text": "SHOULD NOT BE REACHED", "tool_calls": []},
    ]
    result, client, hass = await _run(agent, monkeypatch, script)
    assert "Office" in result or "office" in result.lower()
    assert "did you mean" in result.lower() or "more than one" in result.lower()
    assert len(client.calls) == 1  # no second LLM call


async def test_clear_unique_search_continues_normally(agent, monkeypatch):
    script = [
        {"text": "", "tool_calls": [
            _tool_call("search_entities", {"query": "kitchen", "require_unique": True}, "c1"),
        ]},
        {"text": "Turned it on.", "tool_calls": []},
    ]
    result, client, hass = await _run(agent, monkeypatch, script)
    assert result == "Turned it on."
    assert len(client.calls) == 2


async def test_ambiguous_search_followed_by_mutating_tool_in_same_batch_executes_no_service(
    agent, monkeypatch,
):
    script = [
        {"text": "", "tool_calls": [
            _tool_call("search_entities", {"query": "office light", "require_unique": True}, "c1"),
            _tool_call("control_device", {"entity_id": "light.office", "action": "turn_on"}, "c2"),
        ]},
        {"text": "SHOULD NOT BE REACHED", "tool_calls": []},
    ]
    result, client, hass = await _run(agent, monkeypatch, script)
    assert hass.service_calls == []  # control_device never ran
    assert len(client.calls) == 1
    assert "did you mean" in result.lower() or "more than one" in result.lower()


async def test_clear_search_then_action_in_same_batch_defers_action_to_next_iteration(
    agent, monkeypatch,
):
    """A NON-ambiguous unique search alongside a mutating call in the same
    batch: the action is still deferred this iteration (search-first
    ordering applies regardless of ambiguity), then executes normally once
    the model repeats it with a resolved entity_id."""
    script = [
        {"text": "", "tool_calls": [
            _tool_call("search_entities", {"query": "kitchen", "require_unique": True}, "c1"),
            _tool_call("control_device", {"entity_id": "light.kitchen", "action": "turn_on"}, "c2"),
        ]},
        {"text": "", "tool_calls": [
            _tool_call("control_device", {"entity_id": "light.kitchen", "action": "turn_on"}, "c3"),
        ]},
        {"text": "Turned it on.", "tool_calls": []},
    ]
    result, client, hass = await _run(agent, monkeypatch, script)
    assert len(client.calls) == 3  # search, deferred-action iteration, action-runs iteration
    assert len(hass.service_calls) == 1  # the action ran exactly once, on the second attempt
    assert hass.service_calls[0][0] == "light"
    assert result == "Turned it on."
