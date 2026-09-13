"""Tests for the pending-approval workflow on agent.py's `remember` tool
(v7.88.0, memory write-path hardening).

Fencing (shipped earlier this session) stops a stored fact from being read
back as a live command. It does nothing about a false fact being written in
the first place: `remember` is called by the MODEL, mid-conversation, based
on everything it's seen -- including content Nova merely read aloud. So a
preference/routine now stages as a pending fact (invisible to prompt_block()
until a human confirms it) instead of taking effect immediately. Aliases are
a plain entity-name lookup, not a "fact" injected into any prompt, and are
unaffected -- still written immediately, as before.
"""
import json

import pytest


@pytest.fixture
def agent(load):
    return load("agent")


@pytest.fixture
def knowledge(load, tmp_path, monkeypatch):
    k = load("knowledge")
    monkeypatch.setattr(k, "DB_PATH", str(tmp_path / "knowledge.db"))
    return k


class _Hass:
    def __init__(self):
        self._states = {}

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


# ── remember: alias stays immediate, unfenced, unconfirmed (unaffected) ─────

async def test_remember_alias_writes_immediately(agent, hass, knowledge):
    out = json.loads(await agent._exec_remember(
        hass, {"key": "alias", "name": "reading light", "value": "light.living_room_lamp"}))
    assert out["success"] is True
    assert "status" not in out  # no pending concept for aliases
    learned = agent._load_learned()
    assert learned["alias"]["reading light"] == "light.living_room_lamp"
    # must NOT also land in the knowledge store
    assert knowledge.all_facts() == []


# ── remember: preference/routine stage as pending ───────────────────────────

async def test_remember_preference_stages_as_pending(agent, hass, knowledge, load, monkeypatch):
    identity = load("identity")
    monkeypatch.setattr(identity, "resolve_subject", lambda hass: "primary")
    out = json.loads(await agent._exec_remember(
        hass, {"key": "preference", "name": "bedtime", "value": "10pm"}))
    assert out["success"] is True
    assert out["status"] == "pending"
    assert "fact_id" in out

    facts = knowledge.all_facts()  # unfiltered -- sees it regardless of status
    assert len(facts) == 1
    assert facts[0]["status"] == "pending"
    assert facts[0]["value"] == "10pm"
    assert facts[0]["subject"] == "primary"

    # not yet trusted -- confirmed-only views must not see it
    assert knowledge.all_facts(status="confirmed") == []
    assert knowledge.prompt_block(now=facts[0]["created_at"]) == ""


async def test_remember_preference_does_not_touch_learn_file(agent, hass, knowledge, load, monkeypatch):
    """The old parallel write is gone -- preferences live in knowledge.py only now."""
    identity = load("identity")
    monkeypatch.setattr(identity, "resolve_subject", lambda hass: "primary")
    await agent._exec_remember(hass, {"key": "preference", "name": "bedtime", "value": "10pm"})
    learned = agent._load_learned()
    assert learned.get("preference", {}) == {}


async def test_remember_routine_stages_as_pending_household_fact(agent, hass, knowledge):
    out = json.loads(await agent._exec_remember(
        hass, {"key": "routine", "name": "movie night", "value": "dim lights, close blinds"}))
    assert out["status"] == "pending"
    facts = knowledge.all_facts()
    assert len(facts) == 1
    assert facts[0]["subject"] == "household"
    assert facts[0]["kind"] == "fact"


# ── confirm / reject ─────────────────────────────────────────────────────────

async def test_confirm_pending_fact_promotes_it(agent, hass, knowledge):
    staged = json.loads(await agent._exec_remember(
        hass, {"key": "routine", "name": "movie night", "value": "dim lights"}))
    fact_id = staged["fact_id"]

    out = json.loads(await agent._exec_confirm_pending_fact(hass, {"fact_id": fact_id}))
    assert out["success"] is True

    confirmed = knowledge.all_facts(status="confirmed")
    assert len(confirmed) == 1 and confirmed[0]["id"] == fact_id
    # now it actually reaches the model
    assert "movie night" in knowledge.prompt_block(now=confirmed[0]["created_at"])


async def test_confirm_pending_fact_rejects_unknown_id(agent, hass, knowledge):
    out = json.loads(await agent._exec_confirm_pending_fact(hass, {"fact_id": 999999}))
    assert "error" in out


async def test_reject_pending_fact_deletes_it(agent, hass, knowledge):
    staged = json.loads(await agent._exec_remember(
        hass, {"key": "routine", "name": "movie night", "value": "dim lights"}))
    fact_id = staged["fact_id"]

    out = json.loads(await agent._exec_reject_pending_fact(hass, {"fact_id": fact_id}))
    assert out["success"] is True
    assert knowledge.all_facts() == []


async def test_confirm_pending_fact_requires_valid_id(agent, hass):
    out = json.loads(await agent._exec_confirm_pending_fact(hass, {}))
    assert "error" in out


# ── tool registration ────────────────────────────────────────────────────────

def test_pending_fact_tools_registered(agent):
    names = {t["function"]["name"] for t in agent.NOVA_TOOLS}
    assert "confirm_pending_fact" in names
    assert "reject_pending_fact" in names
    assert "confirm_pending_fact" in agent._TOOL_MAP
    assert "reject_pending_fact" in agent._TOOL_MAP


# ── _build_home_context no longer leaks raw, unfenced preferences ──────────

def test_build_home_context_no_longer_dumps_preferences(agent, monkeypatch):
    """Even a stale _LEARN_FILE from before this fix (some old install that
    still has a 'preference' key on disk) must not be dumped here anymore --
    knowledge.py's prompt_block() is the sole, confirmed-only, fenced path
    now, injected separately by conversation.py."""
    monkeypatch.setattr(agent, "_load_learned", lambda: {
        "alias": {"reading light": "light.living_room_lamp"},
        "preference": {"bedtime": "10pm"},
    })
    hass = _Hass()
    ctx = agent._build_home_context(hass)
    assert "reading light" in ctx  # aliases still shown
    assert "bedtime" not in ctx and "10pm" not in ctx and "User preferences" not in ctx
