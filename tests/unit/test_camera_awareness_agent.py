"""Historical awareness enters the agent prompt through one safe boundary."""
from __future__ import annotations

import sys
import types

from fakes import FakeHass


class _Client:
    def __init__(self):
        self.calls = []

    def chat(self, messages, tools, max_tokens, temperature):
        self.calls.append(messages)
        return {"text": "done", "tool_calls": []}


async def _run(agent, awareness, monkeypatch, awareness_builder):
    client = _Client()

    async def provider(*_args, **_kwargs):
        return client

    monkeypatch.setattr(agent, "_create_provider_with_fallback", provider)
    monkeypatch.setattr(agent, "_load_learned", lambda: {})
    monkeypatch.setattr(awareness, "build_prompt", awareness_builder)
    situation = types.ModuleType("jc.situation")
    situation.snapshot = lambda _hass: "The front door is clear."
    monkeypatch.setitem(sys.modules, "jc.situation", situation)

    hass = FakeHass()
    executor_calls = []
    original_executor = hass.async_add_executor_job

    async def tracking_executor(func, *args):
        executor_calls.append(func)
        return await original_executor(func, *args)

    hass.async_add_executor_job = tracking_executor
    result = await agent.run_agent(
        hass,
        messages=[{"role": "user", "content": "What is new?"}],
        persona="You are Nova.",
        provider_name="ollama",
        api_key="",
        model="local",
        user_input=types.SimpleNamespace(text="What is new?"),
        config={
            "cognition_enabled": True,
            "camera_event_learning": True,
            "camera_historical_awareness": True,
        },
    )
    hass.close_pending()
    return result, client, executor_calls


async def test_awareness_is_read_off_loop_and_separate_from_current_situation(
    load, monkeypatch,
):
    agent = load("agent")
    awareness = load("camera_awareness")
    block = (
        "## What I've noticed lately\n"
        "BEGIN_CAMERA_AWARENESS_test\n"
        "• I've often seen a vehicle at the driveway.\n"
        "END_CAMERA_AWARENESS_test\n\n"
    )

    result, client, executor_calls = await _run(
        agent, awareness, monkeypatch, lambda _config: block,
    )

    system_prompt = client.calls[0][0]["content"]
    assert result == "done"
    assert len(client.calls) == 1
    assert awareness.build_prompt in executor_calls
    assert (
        "## Situation now\nThe front door is clear.\n\n"
        "## What I've noticed lately\n"
    ) in system_prompt


async def test_awareness_failure_cannot_fail_conversation(load, monkeypatch):
    agent = load("agent")
    awareness = load("camera_awareness")

    def fail(_config):
        raise OSError("database unavailable")

    result, client, _executor_calls = await _run(agent, awareness, monkeypatch, fail)

    assert result == "done"
    assert len(client.calls) == 1
    assert "What I've noticed lately" not in client.calls[0][0]["content"]
