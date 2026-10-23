"""alert_path.py: the household reading and the wording rule.

Since 8.45.0 every source's delivery decision is in delivery.py and tested in
test_delivery.py and test_alert_path_unchanged.py. All state here is fake.
"""
import pytest

SPEAKER = "media_player.hall"
RESIDENTS = {"home": "home", "away": "not_home", "unknown": "unknown"}


@pytest.fixture
def ap(load):
    return load("alert_path")


@pytest.fixture
def house(fake_hass):
    """Set the household: home, away or unknown. A speaker exists."""
    fake_hass.states.set(SPEAKER, "idle")

    def _set(residents):
        fake_hass.states.set("person.abi", RESIDENTS[residents])
        return fake_hass
    return _set


def _sit(ap, hass, sleeping=False, quiet=False):
    return ap.situation(hass, {}, sleeping=sleeping, quiet=quiet)


def test_the_situation_carries_the_household_reading(ap, house):
    for residents in RESIDENTS:
        hass = house(residents)
        assert _sit(ap, hass).residents == residents


def test_an_unreadable_household_is_unknown_and_never_raises(ap):
    class _Broken:
        @property
        def states(self):
            raise RuntimeError("state machine down")
    sit = ap.situation(_Broken(), {})
    assert sit.residents == "unknown" and sit.anyone_home is False


@pytest.mark.parametrize("residents,expected", [
    ("home", False), ("away", True), ("unknown", False)])
def test_the_wording_rule_says_no_one_is_home_only_when_known(ap, house, residents, expected):
    assert ap.nobody_home(_sit(ap, house(residents))) is expected


# ── cognition.py's "nobody home" now comes from household.py ────────────────

@pytest.fixture
def cog(load):
    return load("cognition")


def test_cognition_a_person_in_another_zone_counts_as_away(cog, fake_hass):
    # Changed in 8.22.0: before, only "not_home" and "away" counted.
    fake_hass.states.set("person.abi", "Work")
    assert cog._all_tracked_residents_away(fake_hass) is True


def test_cognition_a_linked_phone_reading_home_is_not_away(cog, fake_hass):
    fake_hass.states.set("person.abi", "not_home", device_trackers=["device_tracker.abi_phone"])
    fake_hass.states.set("device_tracker.abi_phone", "home")
    assert cog._all_tracked_residents_away(fake_hass) is False


def test_cognition_loose_trackers_never_prove_the_house_empty(cog, fake_hass):
    fake_hass.states.set("device_tracker.delivery_van", "not_home")
    assert cog._all_tracked_residents_away(fake_hass) is False


@pytest.mark.parametrize("state,armed", [
    ("armed_away", True), ("armed_vacation", True),
    ("armed_home", False), ("armed_night", False), ("armed_custom_bypass", False),
    ("disarmed", False), ("arming", False), ("pending", False), ("triggered", False)])
def test_cognition_armed_uses_the_household_table(cog, load, fake_hass, monkeypatch,
                                                  state, armed):
    fake_hass.states.set("alarm_control_panel.home", state)
    monkeypatch.setattr(load("runtime"), "domain_runtime_config",
                        lambda hass: {"security_alarm_entity": "alarm_control_panel.home"})
    assert cog._conflicting_armed_alarm(fake_hass) is armed
