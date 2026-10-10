"""Gap 1 (8.25.0): the night sweep stops fighting people.

Nova does not fight a person at the door (8.7.20), and now the night sweep
follows that rule: a device it could not secure, or one opened again after it
secured it, is left alone until morning with one phone alert, and an hourly
cap on commands is the backstop. Each scenario runs the real sweep every five
minutes through the real tick. All state is fake.
"""
import pytest

from cognitive_safety_kit import _isolated_core, cc, clock, run_sweeps  # noqa: F401


@pytest.fixture
def safety(cc, fake_hass):
    return cc.SafetyManager(fake_hass, {"honorific": "sir", "lockdown_auto_on_arm": True})


def obey(fake_hass, *jammed):
    """Devices change state when commanded, except the jammed ones."""
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        await real(domain, service, data, blocking=blocking, **kw)
        eid = (data or {}).get("entity_id")
        new = {("lock", "lock"): "locked", ("cover", "close_cover"): "closed"}.get(
            (domain, service))
        st = fake_hass.states.get(eid) if isinstance(eid, str) else None
        if st is not None and new and eid not in jammed:
            fake_hass.states.set(eid, new, **dict(st.attributes))
    fake_hass.services.async_call = call


async def night(safety, hass, clock, sweeps, between=None):
    """Run `sweeps` sweeps five minutes apart; `between` changes the house
    after each one. Returns every alert delivered, in order."""
    delivered = []
    for i in range(sweeps):
        await safety.tick(sleeping=True, anyone_home=True)
        delivered += await run_sweeps(safety, hass)
        clock["now"] += 301
        if between:
            between(i)
    return delivered


def commands(hass, service):
    return [c[2]["entity_id"] for c in hass.service_calls if c[1] == service]


async def test_a_reversing_garage_gets_one_attempt_and_one_alert(safety, fake_hass, clock):
    obey(fake_hass, "cover.garage")                       # an obstruction: it reverses
    fake_hass.states.set("cover.garage", "open", device_class="garage", friendly_name="Garage")
    alerts = await night(safety, fake_hass, clock, 6)
    assert commands(fake_hass, "close_cover") == ["cover.garage"]
    assert len(alerts) == 1 and alerts[0]["urgency"] == "high"
    assert "Garage" in alerts[0]["message"]


async def test_a_door_unlocked_by_hand_is_not_locked_again(safety, fake_hass, clock):
    obey(fake_hass)
    fake_hass.states.set("lock.back", "unlocked", friendly_name="Back")

    def someone_steps_out(i):
        if i == 0:                                        # after Nova locked it
            fake_hass.states.set("lock.back", "unlocked", friendly_name="Back")
    alerts = await night(safety, fake_hass, clock, 6, someone_steps_out)
    assert commands(fake_hass, "lock") == ["lock.back"]
    assert [a["urgency"] for a in alerts] == ["low", "high"]
    assert alerts[0]["message"].endswith("The house is secured.")
    assert "opened again after I secured it" in alerts[1]["message"]
    assert "The house is secured." not in alerts[1]["message"]


async def test_a_normal_night_is_unchanged(safety, fake_hass, clock):
    obey(fake_hass)
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("cover.garage", "open", device_class="garage", friendly_name="Garage")
    alerts = await night(safety, fake_hass, clock, 6)
    assert commands(fake_hass, "lock") == ["lock.front"]
    assert commands(fake_hass, "close_cover") == ["cover.garage"]
    assert len(alerts) == 1 and alerts[0]["urgency"] == "low"
    assert alerts[0]["message"].endswith("The house is secured.")


async def test_a_device_opened_again_later_by_something_else_is_still_secured(
        safety, fake_hass, clock):
    # A lock Nova never locked tonight is not "left alone": it is locked.
    obey(fake_hass)
    fake_hass.states.set("lock.front", "locked", friendly_name="Front")

    def late_unlock(i):
        if i == 1:
            fake_hass.states.set("lock.side", "unlocked", friendly_name="Side")
    await night(safety, fake_hass, clock, 4, late_unlock)
    assert commands(fake_hass, "lock") == ["lock.side"]


async def test_the_memory_starts_afresh_the_next_night(safety, fake_hass, clock):
    obey(fake_hass, "lock.back")
    fake_hass.states.set("lock.back", "unlocked", friendly_name="Back")
    await night(safety, fake_hass, clock, 2)
    assert commands(fake_hass, "lock") == ["lock.back"]
    clock["now"] += 24 * 3600                             # the next night
    await night(safety, fake_hass, clock, 2)
    assert commands(fake_hass, "lock") == ["lock.back", "lock.back"]


async def test_the_hourly_cap_is_the_backstop(safety, fake_hass, clock, monkeypatch):
    import sys
    monkeypatch.setattr(sys.modules["jc.core_safety"], "SWEEP_MAX_COMMANDS_PER_HOUR", 3)
    obey(fake_hass)
    for n in range(5):
        fake_hass.states.set(f"lock.l{n}", "unlocked", friendly_name=f"L{n}")
    await night(safety, fake_hass, clock, 1)
    assert len(commands(fake_hass, "lock")) == 3          # the rest wait
    clock["now"] += 3600                                  # an hour later
    await night(safety, fake_hass, clock, 1)
    assert len(commands(fake_hass, "lock")) == 5


def test_the_default_cap(load):
    assert load("core_safety").SWEEP_MAX_COMMANDS_PER_HOUR == 12
