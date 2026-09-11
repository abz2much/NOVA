"""Regression test: Lockdown must never touch exempt locks (e.g. thermostat
child/keypad locks that happen to live in the `lock` domain but aren't
physical security). Covers both the engage-time sweep and the breach
re-lock path.
"""
import pytest


@pytest.fixture
def cc(load):
    return load("cognitive_core")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch, cc):
    monkeypatch.setattr(cc, "LOCKDOWN_STATE_PATH", str(tmp_path / "lockdown.json"))
    yield


def _mgr(cc, fake_hass, config=None):
    return cc.LockdownManager(fake_hass, config or {"honorific": "sir"})


def _calls(fake_hass, domain, service):
    return [c for c in fake_hass.service_calls if c[0] == domain and c[1] == service]


async def test_default_exempt_locks_are_not_locked_on_engage(cc, fake_hass):
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front Lock")
    fake_hass.states.set("lock.downstairs_thermo_lock", "unlocked",
                         friendly_name="Downstairs Thermo Lock")
    fake_hass.states.set("lock.upstairs_thermo_lock", "unlocked",
                         friendly_name="Upstairs Thermo Lock")
    mgr = _mgr(cc, fake_hass)

    await mgr.engage("test")
    fake_hass.close_pending()

    locked_calls = _calls(fake_hass, "lock", "lock")
    locked_ids = {c[2].get("entity_id") for c in locked_calls}
    assert locked_ids == {"lock.front"}, (
        "only the real lock should be locked; thermostat locks must be skipped")


async def test_exempt_lock_unlocked_during_lockdown_is_not_relocked(cc, fake_hass):
    fake_hass.states.set("lock.downstairs_thermo_lock", "locked",
                         friendly_name="Downstairs Thermo Lock")
    mgr = _mgr(cc, fake_hass)
    await mgr.engage("test")
    fake_hass.close_pending()

    old = fake_hass.states.get("lock.downstairs_thermo_lock")
    fake_hass.states.set("lock.downstairs_thermo_lock", "unlocked",
                         friendly_name="Downstairs Thermo Lock")
    new = fake_hass.states.get("lock.downstairs_thermo_lock")

    alert = await mgr.handle_state_change("lock.downstairs_thermo_lock", old, new)
    fake_hass.close_pending()

    assert alert is None, "exempt lock going unlocked must not raise a breach"
    assert not _calls(fake_hass, "lock", "lock"), "must not attempt to re-lock it"


async def test_config_overrides_default_exempt_list(cc, fake_hass):
    # Custom config with an EMPTY exempt list should restore old behaviour —
    # proves the default isn't hardcoded past the config layer.
    fake_hass.states.set("lock.downstairs_thermo_lock", "unlocked",
                         friendly_name="Downstairs Thermo Lock")
    mgr = _mgr(cc, fake_hass, {"honorific": "sir", "lockdown_exempt_locks": []})

    await mgr.engage("test")
    fake_hass.close_pending()

    assert _calls(fake_hass, "lock", "lock"), (
        "an explicit empty override should let it lock everything again")
