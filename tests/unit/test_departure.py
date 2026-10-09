"""Tests for leave-time / departure anticipation (v6.84.0, reworked v6.89.0).

predict_departure is async and computes travel time from device-tracking origin
to the event's geocoded location via open-source routing (travel.py), with an
explicit sensor as an optional override and a fixed default lead as the fallback.
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
    """Controllable calendar + config; returns (events_holder, cfg)."""
    comms = load("comms")
    jc = load("nova_config")
    holder = {"list": []}
    monkeypatch.setattr(comms, "gather_events", lambda hass: holder["list"])
    cfg = {"departure_alerts_enabled": True, "departure_lead_minutes": 30}
    monkeypatch.setattr(jc, "get", lambda k, d=None: cfg.get(k, d))
    # Events have a real address by default (8.14.0: leave alerts need a real
    # place), so stub the route lookup: no test reaches the network.
    travel = load("travel")

    async def _no_route(*a, **k):
        return None
    monkeypatch.setattr(travel, "travel_minutes", _no_route)
    monkeypatch.setattr(travel, "failure_reason", lambda loc: None)
    return holder, cfg


def _ev(start_dt, title="Dentist", all_day=False, location="12 Main Street, Dundalk"):
    return {"calendar": "calendar.x", "title": title, "start": start_dt,
            "end": start_dt + datetime.timedelta(hours=1),
            "all_day": all_day, "location": location, "active": False}


def _now():
    now = time.time()
    return now, datetime.datetime.fromtimestamp(now)


async def test_alerts_when_time_to_leave(cog, cal, fake_hass):
    holder, cfg = cal
    now, now_dt = _now()
    # event in 20 min, default lead 30 → leave time was 10 min ago → alert
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=20))]
    preds = await cog.predict_departure(fake_hass, now)
    assert len(preds) == 1
    assert preds[0]["type"] == "anticipation_departure"


async def test_no_alert_before_leave_time(cog, cal, fake_hass):
    holder, cfg = cal
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=90))]
    assert await cog.predict_departure(fake_hass, now) == []


async def test_dedup_same_day(cog, cal, fake_hass):
    holder, cfg = cal
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=20))]
    assert len(await cog.predict_departure(fake_hass, now)) == 1
    assert await cog.predict_departure(fake_hass, now) == []


async def test_disabled(cog, cal, fake_hass):
    holder, cfg = cal
    cfg["departure_alerts_enabled"] = False
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=20))]
    assert await cog.predict_departure(fake_hass, now) == []


async def test_all_day_skipped(cog, cal, fake_hass):
    holder, cfg = cal
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=20), all_day=True)]
    assert await cog.predict_departure(fake_hass, now) == []


async def test_beyond_horizon_skipped(cog, cal, fake_hass):
    holder, cfg = cal
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(hours=5))]
    assert await cog.predict_departure(fake_hass, now) == []


async def test_no_events(cog, cal, fake_hass):
    assert await cog.predict_departure(fake_hass, time.time()) == []


async def test_travel_sensor_override_extends_lead(cog, cal, fake_hass, monkeypatch):
    holder, cfg = cal
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=40))]
    # default lead 30 → event in 40 → leave in 10 → NO alert
    assert await cog.predict_departure(fake_hass, now) == []

    cog._RECUR_ALERTED.clear()
    cfg["departure_travel_sensor"] = "sensor.commute"

    class _St:
        state = "45"
    monkeypatch.setattr(fake_hass.states, "get",
                        lambda eid: _St() if eid == "sensor.commute" else None)
    # sensor lead = 45 + 5 buffer = 50 → leave 10 min ago → alert (sensor override wins)
    assert len(await cog.predict_departure(fake_hass, now)) == 1


async def test_uses_oss_travel_for_located_event(cog, cal, fake_hass, load, monkeypatch):
    holder, cfg = cal
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=40), location="123 Main St")]
    monkeypatch.setattr(cog, "_current_origin", lambda hass: (40.0, -75.0))
    travel = load("travel")

    async def _tm(hass, origin, dest, osrm_url=None):
        return 45.0                                # OSS says 45 min drive
    monkeypatch.setattr(travel, "travel_minutes", _tm)
    # lead = 45 + 5 = 50 → event in 40 → leave 10 min ago → alert via OSS routing
    preds = await cog.predict_departure(fake_hass, now)
    assert len(preds) == 1 and "123 Main St" in preds[0]["message"]


async def test_no_oss_call_without_location(cog, cal, fake_hass, load, monkeypatch):
    holder, cfg = cal
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=40), location=None)]   # no location
    monkeypatch.setattr(cog, "_current_origin", lambda hass: (40.0, -75.0))
    travel = load("travel")
    called = {"n": 0}

    async def _tm(hass, origin, dest, osrm_url=None):
        called["n"] += 1
        return 45.0
    monkeypatch.setattr(travel, "travel_minutes", _tm)
    assert await cog.predict_departure(fake_hass, now) == []          # default lead, not yet
    assert called["n"] == 0                                           # no routing without a location


def _ev_on(start_dt, calendar, title="Ballet"):
    ev = _ev(start_dt, title=title)
    ev["calendar"] = calendar
    return ev


async def test_excluded_calendar_is_ignored(cog, cal, fake_hass):
    holder, cfg = cal
    cfg["departure_excluded_calendars"] = ["calendar.birthdays"]
    now, now_dt = _now()
    soon = now_dt + datetime.timedelta(minutes=20)
    holder["list"] = [_ev_on(soon, "calendar.birthdays")]
    assert await cog.predict_departure(fake_hass, now) == []
    holder["list"] = [_ev_on(soon, "calendar.family")]
    assert len(await cog.predict_departure(fake_hass, now)) == 1


async def test_excluded_calendars_accepts_a_json_string(cog, cal, fake_hass):
    holder, cfg = cal
    cfg["departure_excluded_calendars"] = '["calendar.birthdays"]'
    now, now_dt = _now()
    holder["list"] = [_ev_on(now_dt + datetime.timedelta(minutes=20), "calendar.birthdays")]
    assert await cog.predict_departure(fake_hass, now) == []


async def test_travel_time_is_looked_up_once_per_event(cog, cal, fake_hass, load, monkeypatch):
    holder, cfg = cal
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=120), location="School")]
    monkeypatch.setattr(cog, "_current_origin", lambda hass: (53.6, -6.2))
    travel = load("travel")
    calls = {"n": 0}

    async def _tm(hass, origin, dest, osrm_url=None):
        calls["n"] += 1
        return 8.0
    monkeypatch.setattr(travel, "travel_minutes", _tm)
    for i in range(5):                       # five ticks, 30 s apart
        assert await cog.predict_departure(fake_hass, now + 30 * i) == []
    assert calls["n"] == 1
    await cog.predict_departure(fake_hass, now + cog.DEPART_ROUTE_TTL + 1)
    assert calls["n"] == 2                   # reused for the TTL, then refreshed


async def test_failed_lookup_uses_the_default_and_says_so(cog, cal, fake_hass, load, monkeypatch):
    holder, cfg = cal
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=27), location="School")]
    monkeypatch.setattr(cog, "_current_origin", lambda hass: (53.6, -6.2))
    travel = load("travel")

    async def _tm(hass, origin, dest, osrm_url=None):
        return None
    monkeypatch.setattr(travel, "travel_minutes", _tm)
    monkeypatch.setattr(travel, "failure_reason", lambda loc: "the routing service did not answer")
    logged = {}
    monkeypatch.setattr(cog, "_log_decision", lambda *a, **k: logged.update(obs=a[1], why=a[4]) or 1)
    preds = await cog.predict_departure(fake_hass, now)
    assert len(preds) == 1
    assert "couldn't work out the travel time" in preds[0]["message"]
    assert "30 minutes" in preds[0]["message"]
    assert logged["obs"]["lead_from"] == "default"
    assert logged["obs"]["travel_lookup_failed"] == "the routing service did not answer"
    assert "default lead time" in logged["why"]


async def test_failed_lookup_is_not_repeated_every_tick(cog, cal, fake_hass, load, monkeypatch):
    holder, cfg = cal
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=120), location="School")]
    monkeypatch.setattr(cog, "_current_origin", lambda hass: (53.6, -6.2))
    travel = load("travel")
    calls = {"n": 0}

    async def _tm(hass, origin, dest, osrm_url=None):
        calls["n"] += 1
        return None
    monkeypatch.setattr(travel, "travel_minutes", _tm)
    for i in range(5):
        await cog.predict_departure(fake_hass, now + 30 * i)
    assert calls["n"] == 1
    await cog.predict_departure(fake_hass, now + cog.DEPART_RETRY_AFTER + 1)
    assert calls["n"] == 2


async def test_no_known_position_is_reported_not_hidden(cog, cal, fake_hass, monkeypatch):
    holder, cfg = cal
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=20), location="School")]
    monkeypatch.setattr(cog, "_current_origin", lambda hass: None)
    preds = await cog.predict_departure(fake_hass, now)
    assert len(preds) == 1 and "couldn't work out the travel time" in preds[0]["message"]


async def test_working_route_is_recorded_as_the_source(cog, cal, fake_hass, load, monkeypatch):
    holder, cfg = cal
    now, now_dt = _now()
    holder["list"] = [_ev(now_dt + datetime.timedelta(minutes=8), location="School")]
    monkeypatch.setattr(cog, "_current_origin", lambda hass: (53.6, -6.2))
    travel = load("travel")

    async def _tm(hass, origin, dest, osrm_url=None):
        return 4.0                                 # 4 + 5 buffer = 9 minute lead
    monkeypatch.setattr(travel, "travel_minutes", _tm)
    logged = {}
    monkeypatch.setattr(cog, "_log_decision", lambda *a, **k: logged.update(obs=a[1], why=a[4]) or 1)
    preds = await cog.predict_departure(fake_hass, now)
    assert len(preds) == 1 and "couldn't" not in preds[0]["message"]
    assert logged["obs"]["lead_from"] == "route" and logged["obs"]["lead_minutes"] == 9
    assert "estimated drive time" in logged["why"]
