"""Phase 3 — deterministic entity-ambiguity handling, agent path.

search_entities(require_unique=...) and run_agent's tool-dispatch loop
interception. Uses the `load("agent")` fixture (agent.py loads cleanly
without a real Home Assistant runtime, same as test_delegate_task.py) and
FakeHass for entity state.
"""
import json

import pytest

from fakes import FakeHass


@pytest.fixture
def agent(load):
    return load("agent")


@pytest.fixture
def no_aliases(agent, monkeypatch):
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})


# ── require_unique: discovery vs unique-target resolution ──────────────────

async def test_require_unique_false_preserves_discovery_list(agent, no_aliases):
    hass = FakeHass()
    hass.states.set("light.kitchen_main", "on", friendly_name="Kitchen Main")
    hass.states.set("light.kitchen_island", "on", friendly_name="Kitchen Island")
    out = json.loads(await agent._exec_search_entities(hass, {"query": "kitchen light"}))
    assert isinstance(out, list)
    ids = {r["entity_id"] for r in out}
    assert {"light.kitchen_main", "light.kitchen_island"} <= ids


async def test_require_unique_true_flags_real_near_tie(agent, no_aliases):
    # Empirically verified against the real scorer (see Phase 3 design notes):
    # "office light" vs Office/Office Desk scores 43.8/40.0 -> ratio 0.913,
    # above the 0.85 threshold -- a genuine near-tie, not an invented one.
    hass = FakeHass()
    hass.states.set("light.office", "off", friendly_name="Office")
    hass.states.set("light.office_desk", "off", friendly_name="Office Desk")
    out = json.loads(await agent._exec_search_entities(
        hass, {"query": "office light", "require_unique": True}))
    assert isinstance(out, dict)
    assert out["ambiguous"] is True
    assert len(out["candidates"]) >= 2


async def test_require_unique_true_clear_winner_returns_plain_list(agent, no_aliases):
    hass = FakeHass()
    hass.states.set("light.kitchen", "on", friendly_name="Kitchen")
    hass.states.set("sensor.kitchen_humidity", "40", friendly_name="Kitchen Humidity Sensor")
    out = json.loads(await agent._exec_search_entities(
        hass, {"query": "kitchen", "require_unique": True}))
    assert isinstance(out, list)


async def test_require_unique_true_single_result_is_not_ambiguous(agent, no_aliases):
    hass = FakeHass()
    hass.states.set("light.only_one", "on", friendly_name="Only One Light")
    out = json.loads(await agent._exec_search_entities(
        hass, {"query": "only one light", "require_unique": True}))
    assert isinstance(out, list)


# ── partial alias matching ───────────────────────────────────────────────────

@pytest.fixture
def two_partial_aliases(agent, monkeypatch):
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {
        "the chase lamp": "light.chase_lamp",
        "chase office light": "light.chase_office",
    }})


async def test_partial_alias_require_unique_false_returns_all_matches(agent, two_partial_aliases):
    hass = FakeHass()
    hass.states.set("light.chase_lamp", "on", friendly_name="Chase Lamp")
    hass.states.set("light.chase_office", "on", friendly_name="Chase Office")
    out = json.loads(await agent._exec_search_entities(hass, {"query": "chase"}))
    assert isinstance(out, list)
    ids = {r["entity_id"] for r in out}
    assert ids == {"light.chase_lamp", "light.chase_office"}


async def test_partial_alias_require_unique_true_is_ambiguous_for_distinct_entities(
    agent, two_partial_aliases,
):
    hass = FakeHass()
    hass.states.set("light.chase_lamp", "on", friendly_name="Chase Lamp")
    hass.states.set("light.chase_office", "on", friendly_name="Chase Office")
    out = json.loads(await agent._exec_search_entities(
        hass, {"query": "chase", "require_unique": True}))
    assert isinstance(out, dict)
    assert out["ambiguous"] is True
    assert {c["entity_id"] for c in out["candidates"]} == {"light.chase_lamp", "light.chase_office"}


async def test_partial_alias_same_entity_multiple_names_not_ambiguous(agent, monkeypatch):
    # Two DIFFERENT alias names both resolving to the SAME entity_id must
    # not be treated as ambiguous -- it's one entity, not two.
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {
        "chase": "light.chase_lamp",
        "chase light": "light.chase_lamp",
    }})
    hass = FakeHass()
    hass.states.set("light.chase_lamp", "on", friendly_name="Chase Lamp")
    out = json.loads(await agent._exec_search_entities(
        hass, {"query": "chase", "require_unique": True}))
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["entity_id"] == "light.chase_lamp"


async def test_exact_alias_always_unique_regardless_of_require_unique(agent, monkeypatch):
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {
        "chase": "light.chase_lamp",
        "chase office": "light.chase_office",  # a partial match against "chase" too
    }})
    hass = FakeHass()
    hass.states.set("light.chase_lamp", "on", friendly_name="Chase Lamp")
    hass.states.set("light.chase_office", "on", friendly_name="Chase Office")
    out = json.loads(await agent._exec_search_entities(
        hass, {"query": "chase", "require_unique": True}))
    # exact key match ("chase" -> light.chase_lamp) wins outright, before
    # partial-alias collection ever runs.
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["entity_id"] == "light.chase_lamp"
    assert "exact" in out[0]["matched_by"] or "learned alias" in out[0]["matched_by"]


# ── duplicate friendly names in a clarification ──────────────────────────────

def test_clarification_distinguishes_identical_friendly_names(agent):
    candidates = [
        {"entity_id": "light.kitchen_1", "friendly_name": "Kitchen Light"},
        {"entity_id": "light.kitchen_2", "friendly_name": "Kitchen Light"},
    ]
    text = agent._build_clarification(candidates)
    assert "light.kitchen_1" in text
    assert "light.kitchen_2" in text
    assert "Kitchen Light or Kitchen Light?" not in text


def test_clarification_no_distinguisher_needed_for_distinct_names(agent):
    candidates = [
        {"entity_id": "light.bedroom_2", "friendly_name": "Bedroom Light 2"},
        {"entity_id": "light.bedroom_lamp", "friendly_name": "Bedroom Lamp"},
    ]
    text = agent._build_clarification(candidates)
    assert "Bedroom Light 2" in text and "Bedroom Lamp" in text
    assert "light.bedroom_2" not in text  # no need to clutter distinct names


def test_dedupe_candidates_removes_duplicate_entity_ids(agent):
    items = [
        {"entity_id": "light.a", "friendly_name": "A"},
        {"entity_id": "light.a", "friendly_name": "A"},
        {"entity_id": "light.b", "friendly_name": "B"},
    ]
    out = agent._dedupe_candidates(items)
    assert len(out) == 2
    assert {c["entity_id"] for c in out} == {"light.a", "light.b"}
