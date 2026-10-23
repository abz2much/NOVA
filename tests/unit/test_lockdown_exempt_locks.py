"""Exempt locks: the lock domain entities lockdown must never touch.

Some "locks" are not door security at all, such as a thermostat's child or
keypad lock. Lockdown leaves any lock on the saved exempt list alone, both
when it engages and when one is unlocked while it is on.

Since 8.28.0 nothing is exempt by default; the list used here is the kind an
older install keeps after its one time migration.
"""
import pytest

DOWN = "lock.downstairs_thermo_lock"
UP = "lock.upstairs_thermo_lock"
EXEMPTING = {"honorific": "sir", "lockdown_exempt_locks": [DOWN, UP]}


@pytest.fixture
def cc(load, tmp_path, monkeypatch):
    core = load("cognitive_core")
    monkeypatch.setattr(core, "LOCKDOWN_STATE_PATH", str(tmp_path / "lockdown.json"))
    return core


@pytest.fixture
def make(cc, fake_hass):
    def build(config):
        return cc.LockdownManager(fake_hass, dict(config))
    return build


def locked_ids(hass):
    return [data["entity_id"] for dom, svc, data in hass.service_calls
            if (dom, svc) == ("lock", "lock")]


def put(hass, entity_id, state):
    title = entity_id.split(".", 1)[1].replace("_", " ").title()
    return hass.states.set(entity_id, state, friendly_name=title)


async def test_engage_locks_only_the_real_lock(make, fake_hass):
    put(fake_hass, "lock.front", "unlocked")
    put(fake_hass, DOWN, "unlocked")
    put(fake_hass, UP, "unlocked")
    manager = make(EXEMPTING)
    action = await manager.engage("test")
    fake_hass.close_pending()
    assert locked_ids(fake_hass) == ["lock.front"], \
        "thermostat locks must be skipped; only the door lock is locked"
    # An exempt lock is not reported as failed or left open either.
    assert "Thermo" not in action["message"]


@pytest.mark.parametrize("unlocked_one", [DOWN, UP])
async def test_an_exempt_lock_unlocked_during_lockdown_is_left(make, fake_hass, unlocked_one):
    before = put(fake_hass, unlocked_one, "locked")
    manager = make(EXEMPTING)
    await manager.engage("test")
    fake_hass.close_pending()
    assert manager.active is True

    after = put(fake_hass, unlocked_one, "unlocked")
    alert = await manager.handle_state_change(unlocked_one, before, after)
    fake_hass.close_pending()

    assert alert is None, "an exempt lock going unlocked is not a breach"
    assert locked_ids(fake_hass) == [], "and it is never locked again"


async def test_a_real_lock_unlocked_during_lockdown_is_locked_again(make, fake_hass):
    # The same path with a lock that is not exempt, so the test above can only
    # pass because of the exemption.
    before = put(fake_hass, "lock.front", "locked")
    manager = make(EXEMPTING)
    await manager.engage("test")
    after = put(fake_hass, "lock.front", "unlocked")
    await manager.handle_state_change("lock.front", before, after)
    fake_hass.close_pending()
    assert locked_ids(fake_hass) == ["lock.front"]


async def test_an_empty_saved_list_exempts_nothing(make, fake_hass):
    # An explicit [] must win over the default, so every lock is locked.
    put(fake_hass, DOWN, "unlocked")
    manager = make({"honorific": "sir", "lockdown_exempt_locks": []})
    assert manager.exempt_locks == set()
    await manager.engage("test")
    fake_hass.close_pending()
    assert locked_ids(fake_hass) == [DOWN]


@pytest.mark.parametrize("saved,exempt", [
    ('["lock.a", "lock.b"]', {"lock.a", "lock.b"}),       # the panel saves JSON text
    (["lock.a"], {"lock.a"}),                             # the migration saves a list
    ("", set()),
    ("not json", set()),                                  # unreadable: exempt nothing
    ('{"lock.a": 1}', set()),
    (["lock.a", "switch.b", 3], {"lock.a"}),              # only lock ids count
    (42, set()),
])
def test_the_saved_list_is_read_safely(load, saved, exempt):
    assert load("core_lockdown").exempt_lock_set(saved) == exempt


def test_with_no_setting_the_default_applies(make, cc):
    manager = make({"honorific": "sir"})
    assert manager.exempt_locks == set(cc.LOCKDOWN_EXEMPT_LOCKS_DEFAULT)
