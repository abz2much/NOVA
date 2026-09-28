"""Heading-home alerts name the person, never their phone.

A person entity carries the GPS coordinates of its phone tracker, so both
cross the approach line on the same tick. Only the person is announced.
"""
import pytest

HOME = (10.0000, 20.0000)


@pytest.fixture
def cog(load):
    c = load("cognition")
    c._LAST_DIST.clear()
    c._APPROACH_ALERTED.clear()
    return c


def _place(hass, eid, name, lat):
    hass.states.set(eid, "not_home", friendly_name=name,
                    latitude=lat, longitude=HOME[1], source_type="gps")


def _approach(cog, hass, entities):
    """Two readings, ~3 km then ~1.5 km out, for every entity."""
    for lat in (HOME[0] + 0.027, HOME[0] + 0.0135):
        for eid, name in entities:
            _place(hass, eid, name, lat)
        out = cog.predict_proximity(hass)
    return out


def test_person_is_announced_not_their_phone(cog, fake_hass):
    fake_hass.states.set("zone.home", "0", latitude=HOME[0], longitude=HOME[1])
    out = _approach(cog, fake_hass, [
        ("person.alex", "Alex"),
        ("device_tracker.alex_phone", "Alex Phone"),
    ])
    assert [a["message"] for a in out] == ["Alex is heading home — about 1.5 km out."]
    assert out[0]["pattern_key"] == "arriving:person.alex"


def test_trackers_are_used_when_there_are_no_people(cog, fake_hass):
    fake_hass.states.set("zone.home", "0", latitude=HOME[0], longitude=HOME[1])
    out = _approach(cog, fake_hass, [("device_tracker.phone", "Phone")])
    assert [a["pattern_key"] for a in out] == ["arriving:device_tracker.phone"]


def test_announced_once_per_trip(cog, fake_hass):
    fake_hass.states.set("zone.home", "0", latitude=HOME[0], longitude=HOME[1])
    assert _approach(cog, fake_hass, [("person.alex", "Alex")])
    _place(fake_hass, "person.alex", "Alex", HOME[0] + 0.005)
    assert cog.predict_proximity(fake_hass) == []
