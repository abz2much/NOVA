"""The conversational agent must record every provider round trip."""
from __future__ import annotations

import pytest

from fakes import FakeHass


@pytest.fixture
def modules(load):
    return load("agent"), load("providers.activity")


class _Client:
    name = "ollama"
    model = "qwen3:30b-a3b"
    base_url = "http://ollama.test:11434"

    def __init__(self):
        self.calls = []

    def chat(self, messages, tools=None, max_tokens=512, temperature=0.7,
             model_override=None):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })
        return {"text": "Local response", "tool_calls": [], "usage": {}}


async def test_run_agent_uses_activity_wrapper_for_main_call(
    modules, monkeypatch,
):
    agent, activity = modules
    client = _Client()
    activity_calls = []

    async def fake_create_provider(*args, **kwargs):
        return client

    async def fake_execute_chat(
        hass, provider, messages, *, role, data_category, tools=None,
        max_tokens=512, temperature=0.7, model_override=None,
    ):
        activity_calls.append({
            "provider": provider,
            "role": role,
            "data_category": data_category,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })
        return activity.ChatResponse.from_legacy(provider.chat(
            messages, tools=tools, max_tokens=max_tokens,
            temperature=temperature, model_override=model_override,
        ))

    monkeypatch.setattr(agent, "_create_provider_with_fallback", fake_create_provider)
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})
    monkeypatch.setattr(activity, "execute_chat", fake_execute_chat)

    hass = FakeHass()
    result = await agent.run_agent(
        hass,
        messages=[{"role": "user", "content": "Tell me a story."}],
        persona="You are Nova.",
        provider_name="ollama",
        api_key="",
        model="qwen3:30b-a3b",
        config={},
    )
    hass.close_pending()

    assert result == "Local response"
    assert len(client.calls) == 1
    assert len(activity_calls) == 1
    assert activity_calls[0]["provider"] is client
    assert activity_calls[0]["role"] == "llm"
    assert activity_calls[0]["data_category"] == "text"
    assert activity_calls[0]["max_tokens"] == 1024
    assert activity_calls[0]["temperature"] == 0.7
    assert activity_calls[0]["tools"]


class _ToolClient(_Client):
    """First reply asks for a tool; the second answers. Its legacy reply
    carries a provider object in ``raw`` that must never reach history."""

    def __init__(self):
        super().__init__()
        self.replies = [
            {"text": "", "tool_calls": [{"id": "call_1", "name": "get_time", "args": {}}],
             "raw": object(), "usage": {}},
            {"text": "It is noon.", "tool_calls": [], "usage": {}},
        ]

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages]})
        return self.replies.pop(0)


async def test_tool_history_is_built_from_normalized_tool_calls(modules, monkeypatch):
    """Phase 6 defect 9: the assistant turn in the agent's history comes
    from the normalized ToolCall values, never from raw provider objects."""
    import json

    agent, _activity = modules
    client = _ToolClient()

    async def fake_create_provider(*args, **kwargs):
        return client

    async def fake_tool(hass, name, args, *a, **kw):
        return "12:00"

    monkeypatch.setattr(agent, "_create_provider_with_fallback", fake_create_provider)
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})
    if hasattr(agent, "_execute_tool"):
        monkeypatch.setattr(agent, "_execute_tool", fake_tool)

    hass = FakeHass()
    result = await agent.run_agent(
        hass,
        messages=[{"role": "user", "content": "What time is it?"}],
        persona="You are Nova.",
        provider_name="ollama", api_key="", model="qwen3:30b-a3b", config={},
    )
    hass.close_pending()

    assert result == "It is noon."
    second_call = client.calls[1]["messages"]
    assistant = [m for m in second_call if m.get("role") == "assistant"][-1]
    assert assistant == {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "get_time", "arguments": json.dumps({})}}],
    }
    json.dumps(second_call)     # history is plain JSON data throughout
