"""household.py: one answer to who is home and what the alarm says (8.21.0).

The table test runs every alarm state against four households (home, away,
unknown, and away with only a TV tracker reading home), awake and asleep.
All state here is fake. Nothing touches a real alarm or lock.
"""
import pytest

ALARM = "alarm_control_panel.home_security"

# Every alarm state: (posture, armed, triggered).
ALARM_TABLE = {
    "armed_away":          ("away",    True,  False),
    "armed_vacation":      ("away",    True,  False),
    "armed_home":          ("home",    True,  False),
    "armed_night":         ("home",    True,  False),
    "armed_custom_bypass": ("home",    True,  False),
    "disarmed":            ("unknown", False, False),
    "arming":              ("unknown", False, False),
    "pending":             ("unknown", False, False),
    "disarming":           ("unknown", False, False),
    "triggered":           ("unknown", False, True),
    "unavailable":         ("unknown", False, False),
    "unknown":             ("unknown", False, False),
}

# How each household is set up, and the residents answer it must give.
HOUSEHOLDS = {
    "home": ({"person.abi": ("home", {})}, "home"),
    "away": ({"person.abi": ("not_home", {})}, "away"),
    "unknown": ({"person.abi": ("not_home", {}), "person.rachel": ("unknown", {})}, "unknown"),
    "fixed_tracker_only": ({"person.abi": ("not_home", {}),
                            "device_tracker.living_room_tv": ("home", {})}, "away"),
}

# residents_away, by (residents, posture). A resident home always wins;
# unknown counts as away only when the alarm is armed away.
AWAY = {
    ("home", "away"): False, ("home", "home"): False, ("home", "unknown"): False,
    ("away", "away"): True, ("away", "home"): True, ("away", "unknown"): True,
    ("unknown", "away"): True, ("unknown", "home"): False, ("unknown", "unknown"): False,
}


def _guard(residents, posture, triggered, sleeping):
    """The residents home guard, written out: residents home, and the alarm
    armed for people at home, triggered, or the household asleep. Never when
    armed away."""
    if residents != "home" or posture == "away":
        return False
    return sleeping or posture == "home" or triggered


@pytest.fixture
def hh(load):
    return load("household")


def test_the_alarm_table_covers_every_state(hh):
    assert set(hh.ALARM_POSTURE) == set(ALARM_TABLE)


@pytest.mark.parametrize("alarm", sorted(ALARM_TABLE))
@pytest.mark.parametrize("household", sorted(HOUSEHOLDS))
@pytest.mark.parametrize("sleeping", [False, True])
def test_every_alarm_state_against_every_household(hh, fake_hass, alarm, household, sleeping):
    entities, residents = HOUSEHOLDS[household]
    for eid, (state, attrs) in entities.items():
        fake_hass.states.set(eid, state, **attrs)
    fake_hass.states.set(ALARM, alarm)
    house = hh.snapshot(fake_hass, {"security_alarm_entity": ALARM})
    posture, armed, triggered = ALARM_TABLE[alarm]
    assert house.residents == residents
    assert house.posture == posture
    assert house.armed is armed
    assert house.triggered is triggered
    assert house.anyone_home is (residents == "home")
    assert house.residents_away is AWAY[(residents, posture)]
    assert house.residents_home_guard(sleeping) is _guard(residents, posture, triggered, sleeping)


def test_no_alarm_selected_says_nothing(hh, fake_hass):
    fake_hass.states.set("person.abi", "home")
    house = hh.snapshot(fake_hass, {})
    assert house.posture == "unknown" and not house.armed and not house.triggered


# ── residents ───────────────────────────────────────────────────────────────

def test_a_tracker_linked_to_a_person_counts(hh, fake_hass):
    fake_hass.states.set("person.abi", "not_home", device_trackers=["device_tracker.abi_phone"])
    fake_hass.states.set("device_tracker.abi_phone", "home")
    assert hh.residents(fake_hass) == "home"


def test_a_tracker_linked_to_nobody_does_not_count(hh, fake_hass):
    fake_hass.states.set("person.abi", "not_home", device_trackers=["device_tracker.abi_phone"])
    fake_hass.states.set("device_tracker.abi_phone", "not_home")
    fake_hass.states.set("device_tracker.printer", "home")
    assert hh.residents(fake_hass) == "away"


@pytest.mark.parametrize("state", ["unknown", "unavailable"])
def test_an_unreadable_person_is_unknown_never_away(hh, fake_hass, state):
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set("person.rachel", state)
    assert hh.residents(fake_hass) == "unknown"


def test_a_person_in_another_zone_is_away(hh, fake_hass):
    fake_hass.states.set("person.abi", "Work")
    assert hh.residents(fake_hass) == "away"


def test_motion_never_counts(hh, fake_hass):
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    fake_hass.states.set("binary_sensor.lounge_presence", "on", device_class="presence")
    assert hh.residents(fake_hass) == "away"


@pytest.mark.parametrize("trackers,expected", [
    ({"device_tracker.phone": "home"}, "home"),
    ({"device_tracker.phone": "not_home"}, "away"),
    ({"device_tracker.phone": "unavailable"}, "unknown"),
    ({}, "unknown"),
])
def test_with_no_person_entities_the_device_trackers_decide(hh, fake_hass, trackers, expected):
    for eid, state in trackers.items():
        fake_hass.states.set(eid, state)
    assert hh.residents(fake_hass) == expected


# ── shared entity rules ─────────────────────────────────────────────────────

def _st(hass, eid, **attrs):
    hass.states.set(eid, "on", **attrs)
    return hass.states.get(eid)


def test_way_in(hh, load, fake_hass, monkeypatch):
    ef = load("entity_filter")
    monkeypatch.setattr(ef, "_exclusion_config",
                        lambda hass: ({"binary_sensor.spare_door"}, set(), set()))
    assert hh.is_way_in(fake_hass, _st(fake_hass, "binary_sensor.back_door", device_class="door"))
    assert hh.is_way_in(fake_hass, _st(fake_hass, "binary_sensor.garage_door", device_class="door"))
    assert not hh.is_way_in(fake_hass, _st(fake_hass, "binary_sensor.fridge_door",
                                           device_class="door", friendly_name="Fridge door"))
    assert not hh.is_way_in(fake_hass, _st(fake_hass, "binary_sensor.spare_door", device_class="door"))


def test_person_motion(hh, load, fake_hass, monkeypatch):
    ef = load("entity_filter")
    monkeypatch.setattr(ef, "_exclusion_config",
                        lambda hass: ({"binary_sensor.virtual_occupancy"}, set(), set()))
    assert hh.is_person_motion(fake_hass, _st(fake_hass, "binary_sensor.hall_motion",
                                              device_class="motion"))
    assert not hh.is_person_motion(fake_hass, _st(fake_hass, "binary_sensor.garage_car_occupancy",
                                                  device_class="occupancy"))
    assert not hh.is_person_motion(fake_hass, _st(fake_hass, "binary_sensor.virtual_occupancy",
                                                  device_class="occupancy"))


def test_outdoor_sensors_are_neither(hh, load, fake_hass, monkeypatch):
    outdoor = load("outdoor")
    monkeypatch.setattr(outdoor, "is_outdoor", lambda hass, eid, name="": "drive" in eid)
    assert not hh.is_way_in(fake_hass, _st(fake_hass, "binary_sensor.drive_gate", device_class="door"))
    assert not hh.is_person_motion(fake_hass, _st(fake_hass, "binary_sensor.drive_motion",
                                                  device_class="motion"))
