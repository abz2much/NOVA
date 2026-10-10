"""Locks left out of lockdown, set in the panel (8.29.0).

The Security Alarm card edits lockdown_exempt_locks. The panel saves it as a
JSON string; the v8 migration saved a list. Both are read the same way, a
change applies at once, and anything unreadable exempts nothing (every lock
is locked, never fewer). All state is fake; nothing touches a real lock.
"""
import pytest

from cognitive_safety_kit import _isolated_core, cc, clock  # noqa: F401


@pytest.fixture
def sc(load):
    return load("safety_config")


# ── what the panel may save ─────────────────────────────────────────────────

@pytest.mark.parametrize("value", ['[]', '["lock.keypad"]', '["lock.a", "lock.b_2"]'])
def test_a_json_list_of_locks_is_accepted(sc, value):
    assert sc.valid_panel_value("lockdown_exempt_locks", value) is True


@pytest.mark.parametrize("value", ['["light.hall"]', '["lock.Front"]', '["lock."]', '"lock.a"',
                                   '{not json', '[1]', ["lock.a"], None, 5, True])
def test_anything_else_is_refused(sc, value):
    assert sc.valid_panel_value("lockdown_exempt_locks", value) is False
    assert "lock entity ids" in sc.invalid_panel_value_message("lockdown_exempt_locks")


# ── reading the saved value ─────────────────────────────────────────────────

@pytest.mark.parametrize("value,want", [
    ('["lock.keypad", "lock.side"]', {"lock.keypad", "lock.side"}),   # saved by the panel
    (["lock.keypad"], {"lock.keypad"}),                                # saved by the v8 migration
    ("[]", set()), ("", set()), ([], set()),
    ("{not json", set()), (5, set()), (["light.hall", 3, "lock.ok"], {"lock.ok"}),
])
def test_the_saved_value_is_read_the_same_way(load, value, want):
    assert load("core_lockdown").exempt_lock_set(value) == want


async def test_a_list_saved_by_the_panel_is_honoured_by_lockdown(cc, fake_hass):
    fake_hass.states.set("lock.keypad", "unlocked")
    fake_hass.states.set("lock.front", "unlocked")
    mgr = cc.LockdownManager(fake_hass, {"lockdown_exempt_locks": '["lock.keypad"]'})
    assert mgr.exempt_locks == {"lock.keypad"}
    await mgr.engage("alarm armed")
    fake_hass.close_pending()
    assert [c[2]["entity_id"] for c in fake_hass.service_calls if c[:2] == ("lock", "lock")] == [
        "lock.front"]


def test_an_unreadable_value_exempts_nothing(cc, fake_hass):
    assert cc.LockdownManager(fake_hass, {"lockdown_exempt_locks": "{oops"}).exempt_locks == set()


def test_with_nothing_saved_nothing_is_exempt(cc, fake_hass):
    assert cc.LockdownManager(fake_hass, {}).exempt_locks == set()


# ── a change applies at once ────────────────────────────────────────────────

async def test_a_change_applies_to_the_running_manager(cc, fake_hass):
    cc._CORE.lockdown_mgr = cc.LockdownManager(fake_hass, {"lockdown_exempt_locks": ["lock.old"]})
    await cc.apply_runtime_config("lockdown_exempt_locks", '["lock.keypad"]')
    assert cc._CORE.lockdown_mgr.exempt_locks == {"lock.keypad"}
    assert cc._lockdown_exempt_locks() == {"lock.keypad"}            # the sweep and the lists
    await cc.apply_runtime_config("lockdown_exempt_locks", "[]")
    assert cc._lockdown_exempt_locks() == set()


async def test_the_night_sweep_follows_the_change(cc, fake_hass, clock):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "lockdown_auto_on_arm": True})
    safety.sweep_verify_delay = 0
    cc._CORE.lockdown_mgr = cc.LockdownManager(fake_hass, {})
    await cc.apply_runtime_config("lockdown_exempt_locks", '["lock.keypad"]')
    fake_hass.states.set("lock.keypad", "unlocked")
    fake_hass.states.set("lock.front", "unlocked")
    await safety._nighttime_lockdown(0)
    assert [c[2]["entity_id"] for c in fake_hass.service_calls if c[:2] == ("lock", "lock")] == [
        "lock.front"]


async def test_the_briefing_list_follows_the_change(cc, load, fake_hass):
    cc._CORE.lockdown_mgr = cc.LockdownManager(fake_hass, {})
    await cc.apply_runtime_config("lockdown_exempt_locks", '["lock.keypad"]')
    fake_hass.states.set("lock.keypad", "unlocked", friendly_name="Keypad")
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front Door")
    assert load("briefing")._gather_open_things(fake_hass) == ["Front Door is unlocked"]
