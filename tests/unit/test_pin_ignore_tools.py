"""Pin what the agent's ignore_entity and unignore_entity tools do today
(8.7.19, tests only).

agent_runtime/capabilities/memory.py turns "ignore the garage door for an
hour" into a cognitive core ignore rule. An ignored entity drops out of
non-critical announcements (observer), Sentinel's checks and pattern
learning. The tools run here against the real cognitive core and its real
IgnoreManager, with files in tmp_path (cognitive_safety_kit). Tests named
test_current_behaviour_* pin behaviour that looks wrong.
"""
from __future__ import annotations

import json

import pytest

from cognitive_safety_kit import _isolated_core, cc, clock  # noqa: F401  (fixtures)
from fakes import FakeHass


@pytest.fixture
def mem(load):
    return load("agent_runtime.capabilities.memory")


@pytest.fixture
def running(cc):
    """The core as start() leaves it: an ignore manager on _CORE."""
    cc._CORE.ignore_mgr = cc.IgnoreManager()
    return cc._CORE


async def test_ignore_for_a_while_then_it_expires(mem, cc, running, clock):
    res = json.loads(await mem._exec_ignore(FakeHass(), {
        "entity_pattern": "binary_sensor.garage_door", "duration_minutes": 60,
        "reason": "door is being painted"}))
    assert res == {"success": True, "enforced": True, "pattern": "binary_sensor.garage_door",
                   "duration": 60, "reason": "door is being painted"}
    assert cc.is_ignored("binary_sensor.garage_door") is True
    assert cc.is_ignored("binary_sensor.front_door") is False
    clock["now"] += 60 * 60 + 1
    assert cc.is_ignored("binary_sensor.garage_door") is False


async def test_ignore_survives_a_restart_of_the_manager(mem, cc, running):
    await mem._exec_ignore(FakeHass(), {"entity_pattern": "sensor.pool_*"})
    running.ignore_mgr = cc.IgnoreManager()             # reloaded from the file
    assert cc.is_ignored("sensor.pool_temp") is True


async def test_current_behaviour_one_wildcard_ignores_every_entity_forever(mem, cc, running):
    # Looks wrong: the pattern is a glob with no limit, so "*" from any
    # conversation, with no confirmation and no expiry, mutes every
    # non-critical observer announcement in the house and switches off every
    # Sentinel rule (a door left open, a lock left unlocked): Sentinel skips
    # ignored entities with no critical bypass. The observer still lets a
    # critical hazard through.
    res = json.loads(await mem._exec_ignore(FakeHass(), {"entity_pattern": "*"}))
    assert res["success"] is True and res["duration"] == 0
    for eid in ("binary_sensor.front_door", "lock.front_door", "binary_sensor.kitchen_leak"):
        assert cc.is_ignored(eid) is True
    assert cc.list_ignores()[0]["remaining_min"] == "permanent"


async def test_a_bad_duration_adds_no_rule(mem, cc, running):
    res = json.loads(await mem._exec_ignore(FakeHass(), {"entity_pattern": "light.x",
                                                         "duration_minutes": "an hour"}))
    assert "error" in res and cc.is_ignored("light.x") is False


async def test_with_the_core_stopped_the_agent_is_told_nothing_is_enforced(mem, cc):
    cc._CORE.ignore_mgr = None
    res = json.loads(await mem._exec_ignore(FakeHass(), {"entity_pattern": "light.x"}))
    assert res == {"success": False, "enforced": False, "error": "Cognitive core not running"}


async def test_unignore_brings_the_entity_back(mem, cc, running):
    await mem._exec_ignore(FakeHass(), {"entity_pattern": "binary_sensor.garage_door"})
    res = json.loads(await mem._exec_unignore(FakeHass(), {
        "entity_pattern": "binary_sensor.garage_door"}))
    assert res["success"] is True and cc.is_ignored("binary_sensor.garage_door") is False


async def test_unignore_of_a_rule_that_does_not_exist_reports_failure(mem, cc, running):
    res = json.loads(await mem._exec_unignore(FakeHass(), {"entity_pattern": "light.nope"}))
    assert res["success"] is False and res["restored_notifications"] == 0


async def test_current_behaviour_unignore_matches_the_pattern_text_not_the_entity(mem, cc, running):
    # Looks wrong: unignore removes a rule only by its exact pattern text, so
    # after "ignore binary_sensor.garage_*", "stop ignoring the garage door"
    # (binary_sensor.garage_door) removes nothing and the door stays muted.
    await mem._exec_ignore(FakeHass(), {"entity_pattern": "binary_sensor.garage_*"})
    res = json.loads(await mem._exec_unignore(FakeHass(), {
        "entity_pattern": "binary_sensor.garage_door"}))
    assert res["success"] is False
    assert cc.is_ignored("binary_sensor.garage_door") is True
