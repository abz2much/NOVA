"""Stage D (8.24.0): world.py, one read-only snapshot of the house per tick.

* The snapshot table: each field read from the house.
* A field that cannot be read is "unknown", never empty or False, so it can
  never be taken as "away" or "secure".
* Every source sees the same snapshot within one tick: household.snapshot,
  alert_path.situation and the safety tick, even if the house changes
  mid-tick; between ticks they read live.

All state is fake. Nothing here calls a service.
"""
import pytest

from cognitive_safety_kit import _isolated_core, cc, clock  # noqa: F401
from test_cognitive_core_tick import _Safety, env  # noqa: F401


@pytest.fixture
def world(load):
    w = load("world")
    w.end_tick()
    yield w
    w.end_tick()


def _house(hass):
    hass.states.set("person.abi", "not_home")
    hass.states.set("binary_sensor.back_door", "on", device_class="door",
                    friendly_name="Back door")
    hass.states.set("binary_sensor.fridge_door", "on", device_class="door",
                    friendly_name="Fridge door")
    hass.states.set("binary_sensor.kitchen_window", "off", device_class="window")
    hass.states.set("cover.garage", "open", device_class="garage", friendly_name="Garage")
    hass.states.set("cover.living_blind", "open", device_class="blind")
    hass.states.set("lock.front", "unlocked")
    hass.states.set("lock.side", "locked")
    hass.states.set("lock.shed", "unavailable")
    hass.states.set("lock.gate", "jammed")
    hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    hass.states.set("binary_sensor.garage_car_occupancy", "on", device_class="occupancy")


def test_the_snapshot_table(world, fake_hass):
    _house(fake_hass)
    w = world.read(fake_hass, {}, asleep=True, quiet_hours=False)
    assert w.residents == "away"
    assert w.asleep is True and w.quiet_hours is False
    assert w.open_ways_in == ("binary_sensor.back_door", "cover.garage")    # not the fridge
    assert w.unlocked_locks == ("lock.front",)
    assert w.unreadable_locks == ("lock.gate", "lock.shed")
    assert w.open_covers == ("cover.garage", "cover.living_blind")
    assert w.person_motion == ("binary_sensor.hall_motion",)                # not the car
    assert w.lockdown_active is False
    assert w.secure() is False


def test_a_quiet_house_is_secure_only_when_everything_was_read(world, fake_hass):
    fake_hass.states.set("lock.front", "locked")
    w = world.read(fake_hass, {})
    assert w.secure() is True
    assert w.asleep == "unknown" and w.quiet_hours == "unknown"    # not given: unknown


@pytest.mark.parametrize("broken", ["is_way_in", "is_person_motion"])
def test_a_field_that_cannot_be_read_is_unknown(world, load, fake_hass, monkeypatch, broken):
    _house(fake_hass)

    def boom(*a, **k):
        raise RuntimeError("sensor layer down")
    monkeypatch.setattr(load("household"), broken, boom)
    w = world.read(fake_hass, {})
    field = "open_ways_in" if broken == "is_way_in" else "person_motion"
    assert getattr(w, field) == "unknown"
    if broken == "is_way_in":
        assert w.secure() == "unknown"          # never "secure" on a guess


def test_an_unreadable_household_is_unknown_never_away(world, load, fake_hass, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("state machine down")
    monkeypatch.setattr(load("household"), "_live_snapshot", boom)
    w = world.read(fake_hass, {})
    assert w.house is None and w.residents == "unknown"


def test_unreadable_locks_are_unknown_not_secure(world, fake_hass, monkeypatch):
    real = fake_hass.states.async_all

    def flaky(domain=None):
        if domain == "lock":
            raise RuntimeError("lock integration down")
        return real(domain)
    monkeypatch.setattr(fake_hass.states, "async_all", flaky)
    w = world.read(fake_hass, {})
    assert w.unlocked_locks == "unknown" and w.secure() == "unknown"


# ── one snapshot per tick ───────────────────────────────────────────────────

def test_every_reader_sees_the_same_snapshot_within_a_tick(world, load, fake_hass):
    hh, ap = load("household"), load("alert_path")
    fake_hass.states.set("person.abi", "home")
    snap = world.begin_tick(fake_hass, {}, asleep=False, quiet_hours=False)
    fake_hass.states.set("person.abi", "not_home")       # changes mid-tick
    assert hh.snapshot(fake_hass) is snap.house
    assert ap.situation(fake_hass).house is snap.house
    assert ap.situation(fake_hass).residents == "home"
    world.end_tick()
    assert hh.snapshot(fake_hass).residents == "away"     # live again between ticks
    assert world.current() is None


async def test_the_loop_hands_the_safety_tick_the_world_snapshot(world, cc, env, fake_hass):
    seen = []

    class _Capture(_Safety):
        async def tick(self, sleeping, anyone_home, house=None):
            seen.append((house, world.current()))
            return []
    cc._CORE.safety_mgr = _Capture()
    fake_hass.states.set("person.abi", "home")
    await cc._tick()
    (house, current), = seen
    assert current is not None and house is current.house
    assert house.residents == "home"
    assert world.current() is None                       # dropped when the tick ends


async def test_the_snapshot_is_dropped_even_if_the_tick_fails(world, cc, env, fake_hass):
    class _Boom(_Safety):
        async def tick(self, sleeping, anyone_home, house=None):
            raise RuntimeError("safety broke")
    cc._CORE.safety_mgr = _Boom()
    await cc._tick()
    assert world.current() is None


def test_open_situations_are_unknown_until_a_source_is_registered(world, fake_hass,
                                                                 monkeypatch):
    monkeypatch.setattr(world, "_SITUATION_SOURCE", None)
    assert world.read(fake_hass, {}).open_situations == "unknown"
    monkeypatch.setattr(world, "_SITUATION_SOURCE", lambda hass: ["intrusion"])
    assert world.read(fake_hass, {}).open_situations == ("intrusion",)
