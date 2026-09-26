"""Deterministic entity resolution (Phase 8, ENT-002/ENT-003).

The resolver is pure: it takes candidates the caller read from Home
Assistant and returns entity ids. These tests pin its matching order,
margin and ambiguity rules and its determinism.

Focused run:
    python -m pytest tests/unit/test_entity_resolution.py -q
"""
import itertools
import random

import pytest


@pytest.fixture
def er(load):
    return load("agent_runtime.entity_resolution")


def _home(er):
    C = er.Candidate
    return [
        C("light.kitchen_ceiling", "light", "Kitchen Ceiling", "on", "Kitchen"),
        C("light.porch", "light", "Porch Light", "on", "Outside"),
        C("light.hall", "light", "Hall Light", "off", "Hall"),
        C("light.office", "light", "Office", "off", "Office"),
        C("light.office_desk", "light", "Office Desk", "off", "Office"),
        C("light.lamp_a", "light", "Lamp", "off", "Lounge"),
        C("light.lamp_b", "light", "Lamp", "off", "Bedroom"),
        C("sensor.kitchen_light_current", "sensor", "Kitchen Light Current", "0.4", "Kitchen"),
        C("switch.garden_lights", "switch", "Garden Lights", "off", "Outside"),
        C("switch.kettle", "switch", "Kettle", "on", "Kitchen"),
        C("lock.front_door", "lock", "Front Door", "locked", "Hall"),
        C("lock.back_door", "lock", "Back Door", "unlocked", "Kitchen"),
    ]


def _ids(res):
    return [m.entity_id for m in res.matches]


def test_entity_id_first(er):
    res = er.resolve("light.porch", _home(er), require_unique=True)
    assert _ids(res) == ["light.porch"] and res.stage == "entity_id"


def test_exact_friendly_name_resolves_one_target(er):
    res = er.resolve("porch light", _home(er), require_unique=True)
    assert _ids(res) == ["light.porch"] and not res.ambiguous
    assert res.stage == "friendly_name"


def test_exact_object_name(er):
    res = er.resolve("kitchen_ceiling", _home(er), require_unique=True)
    assert _ids(res) == ["light.kitchen_ceiling"]


def test_a_shared_exact_name_is_a_real_ambiguity(er):
    res = er.resolve("lamp", _home(er), require_unique=True)
    assert res.ambiguous and sorted(_ids(res)) == ["light.lamp_a", "light.lamp_b"]


def test_plural_domain_lists_the_whole_domain_and_nothing_else(er):
    res = er.resolve("lights", _home(er))
    assert set(_ids(res)) == {c.entity_id for c in _home(er) if c.domain == "light"}
    assert res.stage == "domain"


def test_plural_domain_with_a_state_filters_on_it(er):
    assert _ids(er.resolve("lights that are on", _home(er))) == [
        "light.kitchen_ceiling", "light.porch"]
    assert _ids(er.resolve("locks unlocked", _home(er))) == ["lock.back_door"]


def test_plural_domain_with_an_area(er):
    res = er.resolve("office lights", _home(er))
    assert _ids(res) == ["light.office", "light.office_desk"] and res.stage == "domain_area"


def test_explicit_domain_is_never_overridden(er):
    # A plural naming another domain is only a word inside the explicit one.
    assert all(i.startswith("switch.") for i in _ids(er.resolve("lights", _home(er),
                                                                domain="switch")))
    assert _ids(er.resolve("lights", _home(er), domain="lock")) == []
    assert er.domain_intent("locks", {"light"}) is None


def test_fuzzy_needs_a_margin_to_pick_one(er):
    # "office" vs "office desk" is exact on the name, so unique.
    assert _ids(er.resolve("office", _home(er), require_unique=True)) == ["light.office"]
    res = er.resolve("offce lite", _home(er), require_unique=True)
    assert res.ambiguous or len(res.matches) <= 1


def test_weak_fuzzy_matches_are_clarified_not_picked(er):
    C = er.Candidate
    pool = [C("light.a", "light", "Reading Nook", "off"), C("light.b", "light", "Reading Chair",
                                                            "off")]
    res = er.resolve("reading", pool, require_unique=True)
    assert res.ambiguous and sorted(_ids(res)) == ["light.a", "light.b"]


def test_nothing_below_the_minimum_score_is_returned(er):
    assert _ids(er.resolve("xyzzy", _home(er))) == []


def test_ordering_is_deterministic_under_shuffle(er):
    base = _home(er)
    queries = ["lights", "light", "kitchen", "door", "lamp", "office", "lights on", "current"]
    for q, uniq in itertools.product(queries, [False, True]):
        want = er.resolve(q, base, require_unique=uniq)
        for seed in range(5):
            pool = list(base)
            random.Random(seed).shuffle(pool)
            assert er.resolve(q, pool, require_unique=uniq) == want, (q, uniq, seed)


def test_payload_shapes(er):
    res = er.resolve("porch light", _home(er))
    payload = res.as_payload()
    assert isinstance(payload, list) and payload[0]["entity_id"] == "light.porch"
    amb = er.resolve("lamp", _home(er), require_unique=True).as_payload()
    assert amb["ambiguous"] is True
    assert {c["friendly_name"] for c in amb["candidates"]} == {"Lamp"}


def test_resolution_never_touches_home_assistant(er):
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(er))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "hass" not in names
    assert not attrs & {"async_call", "states", "services", "async_add_executor_job"}


def test_a_clarification_shows_the_area(er):
    amb = er.resolve("lamp", _home(er), require_unique=True).as_payload()
    assert sorted(c["area"] for c in amb["candidates"]) == ["Bedroom", "Lounge"]
