"""Phase 3 PHACC coverage — deterministic ambiguity handling and honest
action verification, driven against a REAL Home Assistant instance (real
entity/service registration, real hass.services.async_call), not the
hand-rolled fakes used by tests/unit/.

Companions to the unit-level proofs:
  tests/unit/test_agent_run_agent_ambiguity_dispatch.py (dispatch logic,
    scripted client, FakeHass)
  tests/unit/test_verify_control.py (per-action status rules, FakeHass)

Here the same scripted-LLM-client technique drives the REAL run_agent()
against a real hass, and _exec_control_device's fast-domain verification is
exercised against a REAL registered light service, so a wiring mistake that
only shows up against genuine HA state/service machinery (not a fake) would
be caught here.
"""
from unittest.mock import patch

import pytest

DOMAIN = "nova"


class FakeChatClient:
    """Same scripted-response client as the unit-level dispatch tests --
    each entry in `script` is what client.chat() returns for that call."""
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


async def _run_agent(hass, script):
    from custom_components.nova import agent

    client = FakeChatClient(script)

    async def fake_create_provider(*a, **kw):
        return client

    with patch("custom_components.nova.agent._create_provider_with_fallback",
               new=fake_create_provider), \
         patch("custom_components.nova.agent._load_learned",
               return_value={"alias": {}}):
        result = await agent.run_agent(
            hass, messages=[], persona="p", provider_name="ollama",
            api_key="", model="m", hass_api=None, user_input=None, config={},
        )
    await hass.async_block_till_done()
    return result, client


# ── Ambiguous unique-target flow ─────────────────────────────────────────────

async def test_ambiguous_unique_search_blocks_mutating_call_real_hass(hass):
    """Two genuinely near-tie real entities, a require_unique search plus a
    mutating control_device call in the SAME model batch: the deterministic
    clarification must come back, the real light.turn_on service must never
    fire, and no second LLM round-trip is needed."""
    hass.states.async_set("light.office", "off", {"friendly_name": "Office"})
    hass.states.async_set("light.office_desk", "off", {"friendly_name": "Office Desk"})

    calls = []
    async def fake_turn_on(call):
        calls.append(call)
    hass.services.async_register("light", "turn_on", fake_turn_on)

    script = [
        {"text": "", "tool_calls": [
            _tool_call("search_entities",
                       {"query": "office light", "require_unique": True}, "c1"),
            _tool_call("control_device",
                       {"entity_id": "light.office", "action": "turn_on"}, "c2"),
        ]},
        {"text": "SHOULD NOT BE REACHED", "tool_calls": []},
    ]
    result, client = await _run_agent(hass, script)

    assert calls == []  # the real HA service never executed
    assert len(client.calls) == 1  # no second LLM call
    assert "did you mean" in result.lower() or "more than one" in result.lower()
    assert "office" in result.lower()


# ── Clear unique-target flow ─────────────────────────────────────────────────

async def test_clear_unique_search_defers_then_executes_on_next_iteration_real_hass(hass):
    """A clear (non-ambiguous) unique-target search alongside a mutating call
    in the same batch: the action is deferred THIS iteration regardless (the
    search-first ordering rule applies even without ambiguity), then runs for
    real, against the real HA service, once the model repeats it."""
    hass.states.async_set("light.kitchen", "off", {"friendly_name": "Kitchen"})

    calls = []
    async def fake_turn_on(call):
        calls.append(call)
        hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen"})
    hass.services.async_register("light", "turn_on", fake_turn_on)

    script = [
        {"text": "", "tool_calls": [
            _tool_call("search_entities",
                       {"query": "kitchen", "require_unique": True}, "c1"),
            _tool_call("control_device",
                       {"entity_id": "light.kitchen", "action": "turn_on"}, "c2"),
        ]},
        {"text": "", "tool_calls": [
            _tool_call("control_device",
                       {"entity_id": "light.kitchen", "action": "turn_on"}, "c3"),
        ]},
        {"text": "Turned it on.", "tool_calls": []},
    ]
    result, client = await _run_agent(hass, script)

    assert len(client.calls) == 3  # search, deferred-action iteration, action-runs iteration
    assert len(calls) == 1         # the real service ran exactly once, on the second attempt
    assert result == "Turned it on."


# ── Fast verification against real HA service registration ─────────────────

@pytest.fixture
def no_entity_verify_sleep():
    """Mocks BOTH sleep seams: entity_verify's own fast-domain bounded poll,
    and the existing retrying background _verify_control's agent._VERIFY_
    SLEEP -- an unverified turn_on/turn_off schedules the latter too (real
    hass.async_block_till_done() actually runs it, unlike FakeHass), and its
    real 4s-per-step delay would otherwise make these tests take ~8s each."""
    async def _no_sleep(_secs):
        pass
    with patch("custom_components.nova.entity_verify._SLEEP", side_effect=_no_sleep), \
         patch("custom_components.nova.agent._VERIFY_SLEEP", side_effect=_no_sleep):
        yield


async def test_control_device_verified_when_real_service_flips_state(
    hass, no_entity_verify_sleep,
):
    from custom_components.nova import agent

    hass.states.async_set("light.den", "off", {"friendly_name": "Den"})

    async def turn_on(call):
        hass.states.async_set("light.den", "on", {"friendly_name": "Den"})
    hass.services.async_register("light", "turn_on", turn_on)

    out = await agent._exec_control_device(
        hass, {"entity_id": "light.den", "action": "turn_on"})
    assert '"status": "verified"' in out


async def test_control_device_unverified_when_real_service_never_flips_state(
    hass, no_entity_verify_sleep,
):
    from custom_components.nova import agent

    hass.states.async_set("light.den", "off", {"friendly_name": "Den"})

    async def turn_on_noop(call):
        pass   # registered, real service call succeeds, but never flips state
    hass.services.async_register("light", "turn_on", turn_on_noop)

    out = await agent._exec_control_device(
        hass, {"entity_id": "light.den", "action": "turn_on"})
    assert '"status": "unverified"' in out
    await hass.async_block_till_done()


async def test_control_device_toggle_unverified_does_not_retry_real_service(
    hass, no_entity_verify_sleep,
):
    """The idempotency-risk rule (never automatically retry a toggle) proven
    against a real, counted HA service registration, not a fake sink."""
    from custom_components.nova import agent

    hass.states.async_set("light.den", "off", {"friendly_name": "Den"})

    calls = []
    async def toggle_noop(call):
        calls.append(call)   # real call succeeds, but never flips state

    hass.services.async_register("light", "toggle", toggle_noop)

    out = await agent._exec_control_device(
        hass, {"entity_id": "light.den", "action": "toggle"})
    assert '"status": "unverified"' in out
    await hass.async_block_till_done()
    assert len(calls) == 1   # exactly the original call -- no automatic retry


async def test_confirmation_policy_still_gates_before_any_verification_real_hass(hass):
    """Phase 3's fast verification must never bypass the existing
    authorization gate -- confirm_gate still runs first, and when it denies,
    _exec_control_device must return awaiting_confirmation without ever
    calling the real HA service or reaching the new verification code."""
    from custom_components.nova import agent

    hass.states.async_set("lock.front", "unlocked", {"friendly_name": "Front Door"})

    calls = []
    async def unlock(call):
        calls.append(call)
    hass.services.async_register("lock", "unlock", unlock)

    async def deny(*a, **k):
        return False, "Confirmation required before unlocking Front Door."
    with patch("custom_components.nova.policy.confirm_gate", new=deny):
        out = await agent._exec_control_device(
            hass, {"entity_id": "lock.front", "action": "unlock"})

    assert '"status": "awaiting_confirmation"' in out
    assert calls == []   # the real unlock service never executed
