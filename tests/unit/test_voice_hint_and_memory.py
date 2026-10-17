"""Nova Voice ID 8.37.0: the AI's speaker hint and "remember" by voice.

The hint sits in the system prompt's situation block, inside a prompt fence,
never in the messages (the user's words and the history). A spoken
"remember I like..." is filed against the person the turn's identity named.
"""
import json

import pytest

from fakes import FakeHass, FakeUserInput

HINT = "Speaker (recognised by voice): Bob."


class _Client:
    def __init__(self):
        self.calls = []

    def chat(self, messages, tools, max_tokens, temperature):
        self.calls.append({"messages": messages})
        return {"text": "ok", "tool_calls": []}


@pytest.fixture
def agent(load):
    return load("agent")


async def _run(agent, monkeypatch, **kw):
    client = _Client()

    async def fake_create_provider(*a, **k):
        return client
    monkeypatch.setattr(agent, "_create_provider_with_fallback", fake_create_provider)
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})
    hass = FakeHass()
    history = [{"role": "user", "content": "what's on today"}]
    await agent.run_agent(hass, messages=history, persona="PERSONA", provider_name="ollama",
                          api_key="", model="m", hass_api=None, config={}, **kw)
    hass.close_pending()
    return client.calls[0]["messages"], history


async def test_the_hint_is_in_the_system_prompt_inside_the_fence(agent, monkeypatch):
    messages, history = await _run(agent, monkeypatch, user_input=FakeUserInput(),
                                   speaker_hint=HINT)
    system = messages[0]["content"]
    begin = system.rindex("BEGIN_SPEAKER_")
    end = system.rindex("END_SPEAKER_")
    assert begin < system.index(HINT) < end
    assert system.count(HINT) == 1
    # never in the user's words or the history
    assert messages[1:] == [{"role": "user", "content": "what's on today"}]
    assert history == [{"role": "user", "content": "what's on today"}]


async def test_no_hint_no_speaker_block(agent, monkeypatch):
    messages, _ = await _run(agent, monkeypatch, user_input=FakeUserInput())
    assert "SPEAKER" not in messages[0]["content"]


async def test_a_sub_agent_never_gets_the_hint(agent, monkeypatch):
    tools, _, _, directive = agent._resolve_profile("homer")
    messages, _ = await _run(agent, monkeypatch, allowed_tools=tools, depth=1, user_input=None,
                             profile_directive=directive, speaker_hint=HINT)
    assert HINT not in messages[0]["content"]


# ── remember ─────────────────────────────────────────────────────────────

@pytest.fixture
def filed(load, monkeypatch):
    knowledge = load("knowledge")
    identity = load("identity")
    got = []

    def remember(name, value, subject, kind, source, status):
        got.append(subject)
        return {"id": 1}
    monkeypatch.setattr(knowledge, "remember", remember)
    monkeypatch.setattr(identity, "resolve_subject", lambda hass, **kw: "primary")
    return got


async def _remember(agent, load, identification):
    models = load("agent_runtime.models")
    ctx = models.ToolExecutionContext(device_id="sat1", identification=identification)
    out = await agent._execute_tool(FakeHass(), "remember",
                                    {"key": "preference", "name": "tea", "value": "green"},
                                    ctx=ctx)
    return json.loads(out)


async def test_remember_by_voice_files_against_the_voice_identified_person(agent, load, filed):
    identity = load("identity")
    ident = identity.Identification("Bob Jones", 0.9, "voice", {"Bob Jones": 0.9})
    out = await _remember(agent, load, ident)
    assert out["success"] is True and filed == ["bob_jones"]


async def test_remember_without_a_voice_identity_is_as_before(agent, load, filed):
    await _remember(agent, load, None)
    identity = load("identity")
    await _remember(agent, load, identity.Identification())     # unknown
    assert filed == ["primary", "primary"]


def test_the_tool_context_takes_the_identity_only_for_the_top_level_turn(load):
    loop_src = (load("agent_runtime.loop").__file__)
    text = open(loop_src, encoding="utf-8").read()
    assert "identification=identification if depth == 0 else None" in text
