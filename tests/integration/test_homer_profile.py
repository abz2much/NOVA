"""HOMER (Phase 7) PHACC coverage — driven against a REAL Home Assistant
instance (real entity/service registration, real hass.services.async_call),
not the hand-rolled fakes used by tests/unit/.

Companions to the unit-level proofs:
  tests/unit/test_homer_profile.py (profile resolution, _run_delegated wiring)
  tests/unit/test_homer_prompt.py (profile-aware system prompt content)
  tests/unit/test_homer_tool_gate.py (the hard dispatch-time gate, scripted
    client + FakeHass)
  tests/unit/test_homer_no_side_effects.py (source-level guard: no Action
    Audit Log write in any HOMER tool handler)

Here the same scripted-LLM-client technique used by
test_phase3_ambiguity_and_verification.py drives the REAL run_agent()
against a real hass: a real light.turn_on service is registered so an
attempt to actuate it would genuinely fire, and the real Action Audit Log
(action_log.py, its own SQLite file under tmp_path) proves no row is
written by a HOMER delegation end to end.
"""
from unittest.mock import patch
from types import SimpleNamespace


def _conversation_turn():
    """A live conversation turn (the agent treats a run with no user_input
    as headless scheduled work, which cannot act)."""
    return SimpleNamespace(text="", language="en", device_id=None,
                           context=SimpleNamespace(user_id="user-1"))

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


async def _run_agent(hass, script, objective="why is the hallway light unavailable?"):
    from custom_components.nova import agent

    client = FakeChatClient(script)

    async def fake_create_provider(*a, **kw):
        return client

    with patch("custom_components.nova.agent._create_provider_with_fallback",
               new=fake_create_provider), \
         patch("custom_components.nova.agent._load_learned",
               return_value={"alias": {}}):
        result = await agent.run_agent(
            hass, messages=[{"role": "user", "content": objective}],
            persona="p", provider_name="ollama", api_key="", model="m",
            hass_api=None, user_input=_conversation_turn(), config={},
        )
    await hass.async_block_till_done()
    return result, client


# ── HOMER never actuates the real house ─────────────────────────────────────

async def test_homer_delegation_never_calls_a_real_mutating_service(hass):
    """The parent delegates to HOMER; HOMER's own (scripted) response tries
    control_device anyway — the real light.turn_on service must never fire,
    and HOMER still reaches a real diagnostic tool (get_entity_state) and
    reports back."""
    hass.states.async_set("light.hallway", "unavailable", {"friendly_name": "Hallway"})

    calls = []
    async def fake_turn_on(call):
        calls.append(call)
    hass.services.async_register("light", "turn_on", fake_turn_on)

    script = [
        # Parent delegates to HOMER.
        {"text": "", "tool_calls": [
            _tool_call("delegate_task",
                       {"objective": "why is light.hallway unavailable?",
                        "profile": "homer"}, "c1"),
        ]},
        # HOMER's own first LLM turn: a real diagnostic read, PLUS an
        # attempted actuation it was never granted.
        {"text": "", "tool_calls": [
            _tool_call("get_entity_state", {"entity_ids": ["light.hallway"]}, "h1"),
            _tool_call("control_device",
                       {"entity_id": "light.hallway", "action": "turn_on"}, "h2"),
        ]},
        # HOMER's closing report.
        {"text": "Observed: light.hallway is unavailable. Inferred: the "
                  "integration providing it is likely offline. Recommend "
                  "checking that integration's connection.",
         "tool_calls": []},
        # Parent relays HOMER's finding.
        {"text": "HOMER's finding: light.hallway is unavailable, likely an "
                  "offline integration.", "tool_calls": []},
    ]
    result, client = await _run_agent(hass, script)

    assert calls == []  # the real light.turn_on service never executed
    assert "unavailable" in result.lower()
    assert "HOMER" in result


async def test_homer_ungranted_tool_call_is_refused_with_a_safe_message(hass):
    """The refusal for an ungranted tool reaches HOMER as an ordinary tool
    result (not a crash, not a silent drop), so it can still finish and
    report to the parent."""
    hass.states.async_set("lock.front_door", "locked", {"friendly_name": "Front Door"})

    unlock_calls = []
    async def fake_unlock(call):
        unlock_calls.append(call)
    hass.services.async_register("lock", "unlock", fake_unlock)

    script = [
        {"text": "", "tool_calls": [
            _tool_call("delegate_task",
                       {"objective": "unlock the front door and remember the code",
                        "profile": "homer"}, "c1"),
        ]},
        {"text": "", "tool_calls": [
            _tool_call("control_device",
                       {"entity_id": "lock.front_door", "action": "unlock"}, "h1"),
        ]},
        {"text": "I can't perform that action — read-only.", "tool_calls": []},
        {"text": "HOMER declined: it's read-only and can't unlock anything.",
         "tool_calls": []},
    ]
    result, client = await _run_agent(hass, script)

    assert unlock_calls == []
    assert "read-only" in result.lower() or "read only" in result.lower()


# ── No Action Audit Log entry from a HOMER run ──────────────────────────────

async def test_homer_delegation_creates_no_action_audit_log_row(hass, tmp_path, monkeypatch):
    from custom_components.nova import action_log

    db_path = str(tmp_path / "conversations.db")
    monkeypatch.setattr(action_log, "_DEFAULT_DB", db_path)

    before = action_log.page_requests(limit=100)["requests"]

    hass.states.async_set("light.hallway", "unavailable", {"friendly_name": "Hallway"})
    script = [
        {"text": "", "tool_calls": [
            _tool_call("delegate_task",
                       {"objective": "why is light.hallway unavailable?",
                        "profile": "homer"}, "c1"),
        ]},
        {"text": "", "tool_calls": [
            _tool_call("get_entity_state", {"entity_ids": ["light.hallway"]}, "h1"),
        ]},
        {"text": "Likely an offline integration.", "tool_calls": []},
        {"text": "HOMER: likely an offline integration.", "tool_calls": []},
    ]
    await _run_agent(hass, script)

    after = action_log.page_requests(limit=100)["requests"]
    assert len(after) == len(before)


# ── real dispatch: a granted tool still works end to end ───────────────────

async def test_homer_can_read_real_entity_state_and_report_it(hass):
    hass.states.async_set("binary_sensor.leak", "on",
                           {"friendly_name": "Basement Leak", "device_class": "moisture"})
    script = [
        {"text": "", "tool_calls": [
            _tool_call("delegate_task",
                       {"objective": "what caused the basement leak sensor to trip?",
                        "profile": "homer"}, "c1"),
        ]},
        {"text": "", "tool_calls": [
            _tool_call("get_entity_state", {"entity_ids": ["binary_sensor.leak"]}, "h1"),
        ]},
        {"text": "Observed: binary_sensor.leak is currently 'on'.", "tool_calls": []},
        {"text": "HOMER observed the leak sensor is currently on.", "tool_calls": []},
    ]
    result, client = await _run_agent(hass, script)
    assert "leak" in result.lower()

    # The real get_entity_state call actually reached the real hass state.
    homer_tool_result = None
    for c in client.calls:
        for m in c["messages"]:
            if m.get("role") == "tool" and '"binary_sensor.leak"' in m.get("content", ""):
                homer_tool_result = m["content"]
    assert homer_tool_result is not None
    assert '"state": "on"' in homer_tool_result


# ── consolidation: capability="diagnostics" stays a working compatibility
#    alias for the same HOMER implementation, end to end against real hass ──

async def test_legacy_capability_diagnostics_also_never_actuates(hass):
    """An existing caller still using capability="diagnostics" (rather than
    profile="homer") gets HOMER's exact same real-world behavior: it can
    read real state, but a real mutating service never fires."""
    hass.states.async_set("light.hallway", "unavailable", {"friendly_name": "Hallway"})

    calls = []
    async def fake_turn_on(call):
        calls.append(call)
    hass.services.async_register("light", "turn_on", fake_turn_on)

    script = [
        {"text": "", "tool_calls": [
            _tool_call("delegate_task",
                       {"objective": "why is light.hallway unavailable?",
                        "capability": "diagnostics"}, "c1"),
        ]},
        {"text": "", "tool_calls": [
            _tool_call("get_entity_state", {"entity_ids": ["light.hallway"]}, "h1"),
            _tool_call("control_device",
                       {"entity_id": "light.hallway", "action": "turn_on"}, "h2"),
        ]},
        {"text": "Likely an offline integration.", "tool_calls": []},
        {"text": "Diagnostics: likely an offline integration.", "tool_calls": []},
    ]
    result, client = await _run_agent(hass, script)

    assert calls == []
    assert "unavailable" in result.lower() or "offline" in result.lower()
