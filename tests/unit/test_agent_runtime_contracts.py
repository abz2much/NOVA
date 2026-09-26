"""Characterization of the agent's public and compatibility surface.

These pin what the agent package split must not change: the exact tool
definitions the model sees (order and descriptions included), every name
that production code and tests import from ``agent``, run_agent's signature,
and the static, authoritative part of the main and HOMER system prompts.
Regenerate a fixture only for a deliberate, reviewed change."""
import inspect
import json

import pytest

import contract_extract as ce
from fakes import FakeHass, FakeUserInput


@pytest.fixture
def agent(load):
    return load("agent")


def test_tool_specs_are_byte_identical(agent):
    pinned = ce.load_fixture("agent_tool_specs")
    current = json.loads(json.dumps(ce.agent_tool_specs(agent)))
    assert [t["function"]["name"] for t in current["tools"]] == \
        [t["function"]["name"] for t in pinned["tools"]], "tool order changed"
    for want, got in zip(pinned["tools"], current["tools"]):
        assert got == want, f"tool definition changed: {want['function']['name']}"
    assert current["slim_tools"] == pinned["slim_tools"]
    assert current["homer_directive"] == pinned["homer_directive"]


def test_tool_names_are_unique(agent):
    names = [t["function"]["name"] for t in agent.NOVA_TOOLS]
    assert len(names) == len(set(names)) == 49


def test_facade_symbols_still_importable(agent):
    pinned = ce.load_fixture("agent_facade")
    missing = [n for n in pinned["symbols"] if not hasattr(agent, n)]
    assert not missing, f"agent no longer exports: {missing}"


def test_run_agent_signature_is_frozen(agent):
    pinned = ce.load_fixture("agent_facade")
    assert str(inspect.signature(agent.run_agent)) == pinned["run_agent_signature"]
    assert inspect.iscoroutinefunction(agent.run_agent)


class _CapturingClient:
    def __init__(self):
        self.calls = []

    def chat(self, messages, tools, max_tokens, temperature):
        self.calls.append({"messages": messages, "tools": tools})
        return {"text": "ok", "tool_calls": []}


async def _system_prompt(agent, monkeypatch, **kw):
    client = _CapturingClient()

    async def fake_create_provider(*a, **k):
        return client

    monkeypatch.setattr(agent, "_create_provider_with_fallback", fake_create_provider)
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})
    hass = FakeHass()
    await agent.run_agent(
        hass, messages=[{"role": "user", "content": "hello"}],
        persona="PERSONA", provider_name="ollama", api_key="", model="m",
        hass_api=None, config={}, **kw)
    hass.close_pending()
    return client.calls[0]["messages"][0]["content"]


def _static_tail(prompt: str) -> str:
    # From the authoritative tools/rules section to the end: server-written
    # text only, independent of live home state or the clock.
    return prompt[prompt.index("## Tools\n"):]


async def test_main_prompt_static_sections_unchanged(agent, monkeypatch):
    prompt = await _system_prompt(agent, monkeypatch, user_input=FakeUserInput())
    assert prompt.startswith("PERSONA\n\n")
    assert _static_tail(prompt) == ce.load_fixture("agent_prompts")["main"]


async def test_homer_prompt_static_sections_unchanged(agent, monkeypatch):
    tools, _, _, directive = agent._resolve_profile("homer")
    prompt = await _system_prompt(agent, monkeypatch, allowed_tools=tools, depth=1, user_input=None,
                                  profile_directive=directive)
    assert prompt.startswith(directive)
    assert _static_tail(prompt) == ce.load_fixture("agent_prompts")["homer"]


# ── Façade: one object per name, and patches reach the implementation ─────

_OWNED = {
    "run_agent": "agent_runtime.loop",
    "_create_provider_with_fallback": "agent_runtime.loop",
    "_execute_tool": "agent_runtime.dispatcher",
    "_run_delegated": "agent_runtime.delegation",
    "_build_home_context": "agent_runtime.context",
    "_load_learned": "agent_runtime.capabilities.memory",
    "_LEARN_FILE": "agent_runtime.capabilities.memory",
    "_VERIFY_SLEEP": "agent_runtime.capabilities.control",
    "_state_ok": "agent_runtime.capabilities.control",
    "_verify_control": "agent_runtime.capabilities.control",
    "NOVA_TOOLS": "agent_runtime.tool_specs",
    "_TOOL_MAP": "agent_runtime.registry",
    "_SUBAGENT_DENY": "agent_runtime.grants",
}


@pytest.mark.parametrize("name,owner", sorted(_OWNED.items()))
def test_facade_exports_the_owning_object(agent, load, name, owner):
    assert getattr(agent, name) is getattr(load(owner), name)


@pytest.mark.parametrize("name,owner", sorted(_OWNED.items()))
def test_facade_patch_reaches_the_owner_and_is_restored(agent, load, monkeypatch, name, owner):
    impl = load(owner)
    original = getattr(impl, name)
    marker = object()
    with monkeypatch.context() as m:
        m.setattr(agent, name, marker)
        assert getattr(impl, name) is marker
    assert getattr(impl, name) is original
    assert getattr(agent, name) is original


async def test_patched_run_agent_is_what_delegation_calls(agent, monkeypatch):
    seen = {}

    async def fake(hass, **kw):
        seen.update(kw)
        return "sub-result"

    monkeypatch.setattr(agent, "run_agent", fake)
    out = await agent._run_delegated(
        FakeHass(), {"objective": "check", "capability": "home_state"},
        persona="p", provider_name="x", api_key="", model="m", base_url=None,
        config={}, depth=0)
    assert json.loads(out)["result"] == "sub-result"
    assert seen["depth"] == 1
