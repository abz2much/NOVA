"""HOMER's profile-aware system prompt (Phase 7).

The concrete gap this phase closes: simply prepending a directive on top of
the standard system prompt would still unconditionally say "you have tools
to control devices..." and frame the agent as the household's steward —
contradictory claims for a strictly read-only sub-agent. Nova's HOMER
instead takes a genuinely separate prompt-building branch (run_agent's new
`profile_directive` param) that never emits those claims. These tests drive
the REAL run_agent() with a scripted client (same technique as
test_agent_run_agent_ambiguity_dispatch.py) and inspect the actual system
message sent to the LLM, not a re-implementation of the prompt logic.
"""
import pytest

from fakes import FakeHass, FakeUserInput


@pytest.fixture
def agent(load):
    return load("agent")


class _CapturingClient:
    def __init__(self):
        self.calls = []

    def chat(self, messages, tools, max_tokens, temperature):
        self.calls.append({"messages": messages, "tools": tools})
        return {"text": "no fault found", "tool_calls": []}


async def _capture_system_prompt(agent, monkeypatch, *, profile_directive=None,
                                  allowed_tools=None, depth=1, user_input=None):
    client = _CapturingClient()

    async def fake_create_provider(*a, **kw):
        return client

    monkeypatch.setattr(agent, "_create_provider_with_fallback", fake_create_provider)
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})

    hass = FakeHass()
    await agent.run_agent(
        hass, messages=[{"role": "user", "content": "why is the lock unavailable?"}],
        persona="You are Nova.", provider_name="ollama", api_key="", model="m",
        hass_api=None, user_input=user_input, config={},
        allowed_tools=allowed_tools, depth=depth, profile_directive=profile_directive,
    )
    hass.close_pending()
    system_msg = client.calls[0]["messages"][0]
    assert system_msg["role"] == "system"
    return system_msg["content"]


_CONTRADICTORY_PHRASES = [
    "You have tools to control devices",
    "this household's AI steward",
    "## Who you are",
    "When you act, confirm crisply",
]


async def test_homer_prompt_omits_device_control_claims(agent, monkeypatch):
    tools, _, _, directive = agent._resolve_profile("homer")
    content = await _capture_system_prompt(
        agent, monkeypatch, profile_directive=directive, allowed_tools=tools)
    for phrase in _CONTRADICTORY_PHRASES:
        assert phrase not in content, f"HOMER prompt still contains: {phrase!r}"


async def test_homer_prompt_contains_the_diagnostic_directive(agent, monkeypatch):
    tools, _, _, directive = agent._resolve_profile("homer")
    content = await _capture_system_prompt(
        agent, monkeypatch, profile_directive=directive, allowed_tools=tools)
    assert "HOMER" in content
    assert "read-only" in content.lower()
    assert "OBSERVE" in content
    assert "INFER" in content


async def test_homer_prompt_never_claims_repair(agent, monkeypatch):
    tools, _, _, directive = agent._resolve_profile("homer")
    content = await _capture_system_prompt(
        agent, monkeypatch, profile_directive=directive, allowed_tools=tools)
    assert "fixed, repaired, or changed" in content or "never claim to have fixed" in content.lower()


async def test_normal_agent_prompt_unaffected_when_no_profile(agent, monkeypatch):
    """Regression: the main agent (profile_directive=None) still gets the
    full standard prompt — this phase must not remove anything from the
    everyday conversational path."""
    content = await _capture_system_prompt(agent, monkeypatch, profile_directive=None,
                                           depth=0, user_input=FakeUserInput())
    assert "You have tools to control devices" in content
    assert "this household's AI steward" in content
    assert "## Who you are" in content


async def test_generic_capability_subagent_prompt_matches_its_grant(agent, monkeypatch):
    """A non-HOMER delegate (e.g. capability='scheduling') has no named
    profile, but its prompt still matches its read-only grant: it lists
    exactly the granted tools and never claims it can control devices."""
    content = await _capture_system_prompt(
        agent, monkeypatch, profile_directive=None,
        allowed_tools={"calendar_agenda", "read_email"})
    for phrase in _CONTRADICTORY_PHRASES:
        assert phrase not in content
    assert "- calendar_agenda:" in content and "- read_email:" in content
    assert "- control_device:" not in content
    assert "You cannot do any of those things" in content
