"""Leave alerts only for events with a real place (8.14.0).

A "time to leave" alert only makes sense for somewhere you travel to. An event
with no location, a video call, a dial in, "online", or the home itself is not
a place to leave for. is_real_place() is the one small pure check; these tests
pin it and check predict_departure() uses it. No web service is called.
"""
import datetime
import time

import pytest


@pytest.fixture
def cog(load):
    c = load("cognition")
    c._RECUR_ALERTED.clear()
    c._DEPART_TRAVEL.clear()
    return c


@pytest.fixture
def cal(cog, load, monkeypatch):
    comms = load("comms")
    jc = load("nova_config")
    travel = load("travel")
    holder = {"list": []}
    monkeypatch.setattr(comms, "gather_events", lambda hass: holder["list"])
    cfg = {"departure_alerts_enabled": True, "departure_lead_minutes": 30,
           "departure_mode_walk": False, "departure_mode_transit": False,
           "departure_google": False}
    monkeypatch.setattr(jc, "get", lambda k, d=None: cfg.get(k, d))
    monkeypatch.setattr(cog, "_current_origin", lambda hass: (53.6, -6.2))

    async def _tm(*a, **k):
        return None
    monkeypatch.setattr(travel, "travel_minutes", _tm)
    monkeypatch.setattr(travel, "failure_reason", lambda loc: "no route")
    return holder


def _ev(location, minutes=20, all_day=False):
    start = datetime.datetime.fromtimestamp(time.time()) + datetime.timedelta(minutes=minutes)
    return {"calendar": "calendar.x", "title": "Appointment", "start": start,
            "end": start + datetime.timedelta(hours=1), "all_day": all_day,
            "location": location, "active": False}


# ── the pure check ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("loc", [
    "12 Main Street, Dundalk",
    "Beaumont Hospital, Dublin 9",
    "Baile Átha Cliath",
    "Leitir Ceanainn",
    "Gaelscoil Dhún Dealgan",
    "Café Central, Paris",
    "Dentist",
])
def test_real_places(cog, loc):
    assert cog.is_real_place(loc) is True


@pytest.mark.parametrize("loc", [
    None, "", "   ",
    "Zoom", "zoom meeting", "https://us02web.zoom.us/j/123456789",
    "Microsoft Teams Meeting", "Teams", "Google Meet", "meet.google.com/abc-defg-hij",
    "Webex", "Skype", "FaceTime", "Whereby", "Jitsi",
    "Online", "online", "Virtual", "Video call", "Phone call", "Phone",
    "Dial in: +353 1 234 5678", "+353 1 234 5678", "Conference call",
    "www.example.com/meeting", "To be confirmed", "TBC", "TBD", "N/A",
    "Home", "home", "At home", "My house",
])
def test_not_real_places(cog, loc):
    assert cog.is_real_place(loc) is False


def test_the_homes_own_name_is_not_a_place(cog):
    assert cog.is_real_place("Oak Lodge", home_names=("Oak Lodge",)) is False
    assert cog.is_real_place("oak lodge ", home_names=("Oak Lodge",)) is False
    assert cog.is_real_place("Oak Lodge Hotel", home_names=("Oak Lodge",)) is True


def test_never_raises(cog):
    assert cog.is_real_place(object()) is False
    assert cog.is_real_place(12345) is False


# ── predict_departure uses it ──────────────────────────────────────────────

async def test_an_event_with_an_address_alerts(cog, cal, fake_hass):
    cal["list"] = [_ev("12 Main Street, Dundalk")]
    preds = await cog.predict_departure(fake_hass)
    assert len(preds) == 1 and "12 Main Street" in preds[0]["message"]


@pytest.mark.parametrize("loc", [None, "", "Zoom", "https://teams.microsoft.com/l/meetup-join/x",
                                 "Online", "Home", "+353 1 234 5678"])
async def test_an_event_with_no_real_place_does_not_alert(cog, cal, fake_hass, loc):
    cal["list"] = [_ev(loc)]
    assert await cog.predict_departure(fake_hass) == []


async def test_the_homes_own_name_does_not_alert(cog, cal, fake_hass):
    fake_hass.config.location_name = "Oak Lodge"
    cal["list"] = [_ev("Oak Lodge")]
    assert await cog.predict_departure(fake_hass) == []


async def test_all_day_events_still_never_alert(cog, cal, fake_hass):
    cal["list"] = [_ev("12 Main Street, Dundalk", all_day=True)]
    assert await cog.predict_departure(fake_hass) == []


async def test_a_video_call_is_skipped_and_the_next_real_place_still_alerts(cog, cal, fake_hass):
    cal["list"] = [_ev("Zoom", minutes=10), _ev("Beaumont Hospital, Dublin 9", minutes=20)]
    preds = await cog.predict_departure(fake_hass)
    assert len(preds) == 1 and "Beaumont" in preds[0]["message"]
