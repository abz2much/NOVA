"""What the agent's turn loop does when the model or the provider
misbehaves.

agent_runtime/loop.py turns model output into tool calls. The grant,
deferral and ambiguity paths are already pinned elsewhere
(test_agent_runtime_boundaries.py, test_agent_run_agent_ambiguity_dispatch.py).
This file pins the recovery paths that decide what runs and what the person
is told: the one-shot slim retry on a too-large request, the malformed tool
call retries, the provider fallback, and the reply after the iteration cap.
A scripted fake client stands in for the provider; nothing leaves the test.
The one 8.7.19 finding here (a false "completed" after the round limit) was
fixed in 8.7.20.
"""
from __future__ import annotations

import pytest

from fakes import FakeHass, FakeUserInput
import json


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
    hass.states.set("lock.front_door", "unlocked", friendly_name="Front Door")
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

async def test_after_the_cap_a_failed_summary_says_nova_could_not_finish(
        loop, load, monkeypatch):
    # 8.7.19 pinned the fallback persona.completed() line here: after ten
    # failed actions and a failed summary, Nova said it had completed them.
    # It now says it could not finish.
    attempt = {"text": "", "tool_calls": [_tc("control_device", {"entity_id": "lock.garden",
                                                                "action": "lock"})]}
    script = [attempt] * loop.MAX_TOOL_ITERATIONS + [RuntimeError("summary failed")]
    result, client, hass, _ = await _run(loop, monkeypatch, script)
    assert result == ("I couldn't finish that. I ran out of steps before I could check "
                      "what was done, so please check before relying on it.")
    assert "complete" not in result.lower()
    assert hass.service_calls == []
    tool_msgs = [m for m in client.calls[-1]["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == loop.MAX_TOOL_ITERATIONS


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


# ── long chats and the no provider reply (8.49.38) ──────────────────────────

async def test_a_full_conversation_history_is_not_summarised(loop, monkeypatch):
    # The conversation keeps 20 turns and adds the new one; that must reach
    # the model word for word, with no extra summarising call first.
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
               for i in range(21)]
    calls = []

    class _Providers:
        async def primary(self, *a):
            calls.append(a)
            raise AssertionError("no summary call expected")

    full = [{"role": "system", "content": "S"}] + history
    out = await loop._maybe_summarize(FakeHass(), full, "fake", "", "m", None,
                                      providers=_Providers())
    assert out == full and calls == []


async def test_the_no_provider_reply_names_the_reasoning_tier_not_gemini(loop):
    class _Providers:
        async def primary(self, *a):
            raise RuntimeError("down")

        async def tier(self, cfg, name):
            raise RuntimeError("down too")

    with pytest.raises(RuntimeError) as err:
        await loop._create_provider_with_fallback(
            FakeHass(), "groq", "", "m", None, {"x": 1}, providers=_Providers())
    assert "Gemini" not in str(err.value)
    assert "(tried groq + the reasoning tier)" in str(err.value)


# ── a request too large (413) and the slimmer retry ───────────────────────────

HOME = "## Current home state\n"


@pytest.fixture
def agent(load):
    return load("agent")


@pytest.mark.parametrize("text", [
    "Error code: 413 - {'error': {'message': 'Request too large for model "
    "`openai/gpt-oss-120b` in organization `org_x` service tier `on_demand`'}}",
    "maximum context length exceeded",
    "This prompt is too long",
    "HTTP 413 Payload Too Large",
])
def test_size_errors_are_recognised(agent, text):
    assert agent._is_too_large(Exception(text)) is True


@pytest.mark.parametrize("text", [
    "completed using 413 tokens",      # a number, not a status
    "Connection timed out",            # connectivity has its own path
    "model not found",
])
def test_other_errors_are_not_size_errors(agent, text):
    assert agent._is_too_large(Exception(text)) is False


def test_the_home_state_goes_and_the_other_sections_stay(agent):
    prompt = ("Persona line.\n\n" + HOME + "Areas: Hall, Study\nLights: p, q\n\n"
              "## Tools\nCall control_device.\n")
    trimmed = agent._strip_home_state(prompt)
    assert "Areas: Hall" not in trimmed and "Lights: p, q" not in trimmed
    assert trimmed.startswith("Persona line.\n\n" + HOME)
    assert trimmed.endswith("## Tools\nCall control_device.\n")
    assert "search_entities" in trimmed


def test_the_home_state_as_the_last_section_goes_too(agent):
    trimmed = agent._strip_home_state("Persona line.\n\n" + HOME + "Lights: p\n")
    assert "Lights: p" not in trimmed and trimmed.startswith("Persona line.")


def test_a_prompt_without_home_state_is_left_alone(agent):
    prompt = "Persona line.\n\n## Tools\nCall control_device.\n"
    assert agent._strip_home_state(prompt) == prompt


def test_a_missing_gemini_thought_signature_is_a_tool_format_error(agent):
    # Answered without tools, never taken as being offline (which would trip
    # the breaker).
    exc = Exception("400 - Function call is missing a thought_signature in "
                    "functionCall parts. This is required for tools to work correctly.")
    assert agent._is_tool_format_error(exc) is True
    assert agent._is_connectivity_error(exc) is False


def test_the_slim_tool_list_is_real_and_much_smaller(agent):
    every = {spec["function"]["name"] for spec in agent.NOVA_TOOLS}
    assert agent._SLIM_TOOLS <= every
    slim = agent._scoped_tool_list(agent._SLIM_TOOLS)
    assert len(slim) == len(agent._SLIM_TOOLS)
    # The retry only helps if it is far smaller than the full request.
    assert 3 * len(json.dumps(slim)) < len(json.dumps(agent._scoped_tool_list(None)))
