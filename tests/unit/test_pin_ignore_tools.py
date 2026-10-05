"""Pin what the agent's ignore_entity and unignore_entity tools do today
(8.7.19, tests only).

agent_runtime/capabilities/memory.py turns "ignore the garage door for an
hour" into a cognitive core ignore rule. An ignored entity drops out of
non-critical announcements (observer), Sentinel's checks and pattern
learning. The tools run here against the real cognitive core and its real
IgnoreManager, with files in tmp_path (cognitive_safety_kit). Tests named
test_current_behaviour_* pin behaviour that looks wrong. The "*" finding
from 8.7.19 was fixed in 8.7.20: a pattern that matches everything is
refused, every ignore expires, and Sentinel no longer reads ignore rules.
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


@pytest.mark.parametrize("pattern", ["*", "**", "*.*", "?*", " * ", "", "*_*", "*.*_*"])
async def test_a_pattern_that_matches_everything_is_refused(mem, cc, running, pattern):
    # 8.7.19 pinned "*" muting every non-critical announcement and every
    # Sentinel rule, forever. A pattern that would match every entity is now
    # refused, and nothing is ignored.
    res = json.loads(await mem._exec_ignore(FakeHass(), {"entity_pattern": pattern}))
    assert res["success"] is False and res["enforced"] is False
    assert "would ignore every entity" in res["error"]
    assert cc.list_ignores() == []
    assert cc.is_ignored("lock.front_door") is False


async def test_an_ignore_with_no_duration_expires_after_the_default(mem, cc, running, clock):
    # 8.7.19: no duration meant "until manually cleared". Every ignore now
    # expires; with none given it uses nova.nap's 30 minute default.
    res = json.loads(await mem._exec_ignore(FakeHass(), {"entity_pattern": "light.porch"}))
    assert res["success"] is True and res["duration"] == 30
    assert cc.list_ignores()[0]["remaining_min"] == 30
    clock["now"] += 30 * 60 + 1
    assert cc.is_ignored("light.porch") is False


async def test_a_plain_entity_and_a_domain_pattern_still_work(mem, cc, running):
    await mem._exec_ignore(FakeHass(), {"entity_pattern": "binary_sensor.garage_door",
                                        "duration_minutes": 15})
    await mem._exec_ignore(FakeHass(), {"entity_pattern": "binary_sensor.*"})
    assert cc.is_ignored("binary_sensor.garage_door") is True
    assert cc.is_ignored("binary_sensor.hall_motion") is True     # a whole domain is allowed
    assert cc.is_ignored("lock.front_door") is False


async def test_old_rules_from_disk_lose_wildcards_and_gain_an_expiry(cc, clock):
    # A rule file written by 8.7.19 or earlier: a "*" rule and a permanent one.
    import json as _json
    _json.dump([
        {"entity_pattern": "*", "reason": "", "expires_at": 0, "created_at": 1.0},
        {"entity_pattern": "light.porch", "reason": "", "expires_at": 0, "created_at": 1.0},
    ], open(cc._ignore_file(), "w"))
    cc._CORE.ignore_mgr = cc.IgnoreManager()
    assert [r["pattern"] for r in cc.list_ignores()] == ["light.porch"]
    assert cc.is_ignored("lock.front_door") is False
    clock["now"] += 30 * 60 + 1
    assert cc.is_ignored("light.porch") is False


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
