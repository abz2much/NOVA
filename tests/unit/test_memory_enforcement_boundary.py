"""Regression tests for the live-caught false-enforcement-claim incident:
Nova told the user "I've saved a rule... locked in... enforced at the
alerting layer itself" after only ever calling `remember` (a conversational
knowledge-store write) — never `ignore_entity`, the actual enforceable
mechanism (cognitive_core.IgnoreManager, which sentinel.py checks). The
memory panel confirmed only a plain fact under "About Me" existed; the
alert repeated regardless.

Fix: every tool result that could plausibly be mistaken for "this changed
runtime behaviour" now carries an explicit, structured `enforced` field —
False for remember/confirm_pending_fact, True for a successful
ignore_entity — so a claim of enforcement can be checked against real tool
output instead of the model's own prose.
"""
import json

import pytest


@pytest.fixture
def agent(load):
    return load("agent")


class _Hass:
    def __init__(self):
        pass

    class _States:
        def async_all(self, domain):
            return []

    @property
    def states(self):
        return self._States()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def hass():
    return _Hass()


@pytest.fixture(autouse=True)
def _isolate_learn_file(agent, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_LEARN_FILE", str(tmp_path / "learned.json"))


@pytest.fixture
def knowledge(load, tmp_path, monkeypatch):
    k = load("knowledge")
    monkeypatch.setattr(k, "DB_PATH", str(tmp_path / "knowledge.db"))
    return k


# ── Test 7 (required list): a stored preference alone is never "enforced" ──

async def test_remember_preference_result_is_explicitly_not_enforced(
    agent, hass, knowledge, load, monkeypatch,
):
    identity = load("identity")
    monkeypatch.setattr(identity, "resolve_subject", lambda hass: "primary")
    out = json.loads(await agent._exec_remember(
        hass, {"key": "preference", "name": "thermo lock alerts",
               "value": "never announce thermostat lock changes"}))
    assert out["success"] is True
    assert out["enforced"] is False
    assert "not" in out["message"].lower()  # explains it's non-enforcing


async def test_remember_routine_result_is_also_not_enforced(agent, hass, knowledge):
    out = json.loads(await agent._exec_remember(
        hass, {"key": "routine", "name": "movie night", "value": "dim lights"}))
    assert out["enforced"] is False


async def test_confirm_pending_fact_result_stays_not_enforced(agent, hass, knowledge):
    staged = json.loads(await agent._exec_remember(
        hass, {"key": "routine", "name": "movie night", "value": "dim lights"}))
    out = json.loads(await agent._exec_confirm_pending_fact(
        hass, {"fact_id": staged["fact_id"]}))
    assert out["success"] is True
    assert out["enforced"] is False


def test_remember_tool_description_disclaims_runtime_enforcement(agent):
    tool = next(t for t in agent.NOVA_TOOLS if t["function"]["name"] == "remember")
    desc = tool["function"]["description"].lower()
    assert "never changes" in desc or "does not change" in desc or "never" in desc
    assert "ignore_entity" in desc


# ── Test 8 (required list): cannot claim a rule without structured success ─

async def test_ignore_entity_result_is_explicitly_enforced(agent, load, monkeypatch):
    cognitive_core = load("cognitive_core")

    class _FakeIgnoreMgr:
        def add(self, entity_pattern, duration_minutes, reason):
            return type("R", (), {"entity_pattern": entity_pattern})()
    cognitive_core._CORE.ignore_mgr = _FakeIgnoreMgr()

    out = json.loads(await agent._exec_ignore(
        None, {"entity_pattern": "lock.upstairs_thermo_lock", "reason": "not a security lock"}))
    assert out["success"] is True
    assert out["enforced"] is True


async def test_ignore_entity_failure_is_not_enforced(agent, load):
    cognitive_core = load("cognitive_core")
    cognitive_core._CORE.ignore_mgr = None
    out = json.loads(await agent._exec_ignore(
        None, {"entity_pattern": "lock.upstairs_thermo_lock"}))
    assert out["success"] is False
    assert out["enforced"] is False


def test_ignore_entity_tool_description_says_it_actually_changes_behaviour(agent):
    tool = next(t for t in agent.NOVA_TOOLS if t["function"]["name"] == "ignore_entity")
    desc = tool["function"]["description"].lower()
    assert "enforced" in desc


def test_system_prompt_instructs_the_enforcement_response_boundary():
    """run_agent()'s persona/critical-rules text is an inline f-string, not
    a separately-callable helper -- read the module source directly rather
    than driving the whole (LLM-provider-dependent) run_agent() just to
    reach one string literal."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "agent.py"
    text = src.read_text(encoding="utf-8")
    assert "enforced: true" in text
    assert "enforced: false" in text
    assert "ignore_entity, not remember" in text


# ── Sentinel now actually enforces a genuine ignore_entity rule ────────────

def test_sentinel_ignore_check_reads_the_same_store_ignore_entity_writes(load, monkeypatch):
    """cognitive_core.is_ignored() is the public read side of the exact
    store cognitive_core.ignore() (the ignore_entity tool) writes to --
    confirms sentinel._is_ignored() and the tool are wired to the same
    mechanism, not two independent concepts that happen to share a name."""
    import sys, types
    ev = types.ModuleType("homeassistant.helpers.event")
    ev.async_track_state_change_event = lambda *a, **k: (lambda: None)
    ev.async_track_time_interval = lambda *a, **k: (lambda: None)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)

    cognitive_core = load("cognitive_core")
    sentinel = load("sentinel")

    class _FakeIgnoreMgr:
        def is_ignored(self, entity_id):
            return entity_id == "lock.upstairs_thermo_lock"
    cognitive_core._CORE.ignore_mgr = _FakeIgnoreMgr()

    assert sentinel._is_ignored(None, "lock.upstairs_thermo_lock") is True
    assert sentinel._is_ignored(None, "lock.front_door") is False
