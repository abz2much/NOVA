"""Leave alerts by travel mode (8.9.0): walking, public transport and driving
in one heads up, redundant modes left out, and later leave times reminded about
only while everyone is still home."""
import datetime
import time
import types

import pytest


@pytest.fixture
def env(load, monkeypatch, fake_hass):
    cog = load("cognition")
    cog._RECUR_ALERTED.clear()
    cog._DEPART_TRAVEL.clear()
    cog._DEPART_STAGES.clear()
    gt = load("google_travel")
    gt._USAGE.update(day=None, count=0)
    comms = load("comms")
    nc = load("nova_config")
    cfg = {"departure_alerts_enabled": True, "departure_lead_minutes": 30}
    monkeypatch.setattr(nc, "get", lambda k, d=None: cfg.get(k, d))
    holder = {"list": []}
    monkeypatch.setattr(comms, "gather_events", lambda hass: holder["list"])
    monkeypatch.setattr(cog, "_current_origin", lambda hass: (53.6, -6.2))
    fake_hass.states.set("person.abi", "home")
    fake_hass.states.set("person.sam", "home")

    e = types.SimpleNamespace(cog=cog, cfg=cfg, events=holder, hass=fake_hass, calls=[],
                              values={"walk": 12.0, "drive": 3.0, "transit": None},
                              logged=[], entry=True)
    entry = types.SimpleNamespace(entry_id="e1", state=types.SimpleNamespace(value="loaded"))
    fake_hass.config_entries = types.SimpleNamespace(
        async_entries=lambda domain: [entry] if e.entry and domain == "google_travel_time" else [])
    fake_hass.services.register("google_travel_time", "get_travel_times")
    fake_hass.services.register("google_travel_time", "get_transit_times")

    async def call(domain, service, data=None, blocking=False, return_response=False, **kw):
        e.calls.append((service, dict(data)))
        mode = {"walking": "walk", "driving": "drive"}.get(data.get("mode"), "transit")
        value = e.values.get(mode)
        if value is None:
            return {"routes": []}
        return {"routes": [{"duration": value * 60}]}
    monkeypatch.setattr(fake_hass.services, "async_call", call)
    monkeypatch.setattr(cog, "_log_decision",
                        lambda *a, **k: e.logged.append((a[1], a[2], a[4])) or len(e.logged))
    return e


def _at(minutes_from_now, location="The School, Balbriggan", title="Ballet"):
    now = time.time()
    start = datetime.datetime.fromtimestamp(now) + datetime.timedelta(minutes=minutes_from_now)
    return now, {"calendar": "calendar.family", "title": title, "start": start,
                 "end": start + datetime.timedelta(hours=1), "all_day": False,
                 "location": location, "active": False}


async def test_walking_and_driving_are_both_in_the_message(env):
    now, ev = _at(17)                          # walk 12+5 = 17: leave now. drive 3+5 = 8: in 9
    env.events["list"] = [ev]
    (action,) = await env.cog.predict_departure(env.hass, now)
    assert action["message"] == (
        "Heads up — Ballet at The School, Balbriggan begins in about 17 minutes. "
        "Head out now if you're walking, or leave in 9 minutes if you're driving.")
    assert "couldn't" not in action["message"]


async def test_nothing_is_said_before_the_earliest_leave_time(env):
    now, ev = _at(40)
    env.events["list"] = [ev]
    assert await env.cog.predict_departure(env.hass, now) == []


async def test_three_modes_are_listed_in_order(env):
    env.values.update(walk=40.0, transit=25.0, drive=5.0)
    now, ev = _at(45)                          # walk lead 45, transit 30, drive 10
    env.events["list"] = [ev]
    (action,) = await env.cog.predict_departure(env.hass, now)
    assert action["message"].endswith(
        "Head out now if you're walking, leave in 15 minutes if you're taking public transport, "
        "or leave in 35 minutes if you're driving.")


async def test_transit_that_is_just_walking_is_left_out(env):
    env.values.update(walk=12.0, transit=12.1, drive=3.0)    # Google gave a walk for transit
    now, ev = _at(17)
    env.events["list"] = [ev]
    (action,) = await env.cog.predict_departure(env.hass, now)
    assert "public transport" not in action["message"]


async def test_a_walk_that_is_too_far_is_left_out(env):
    env.values.update(walk=400.0, transit=64.0, drive=30.0)
    now, ev = _at(69)
    env.events["list"] = [ev]
    (action,) = await env.cog.predict_departure(env.hass, now)
    assert "walking" not in action["message"] and "public transport" in action["message"]


async def test_modes_that_leave_together_read_as_one(env):
    env.values.update(walk=10.0, drive=9.0, transit=None)    # leave times a minute apart
    now, ev = _at(15)
    env.events["list"] = [ev]
    (action,) = await env.cog.predict_departure(env.hass, now)
    assert action["message"].endswith("Head out now if you're walking or driving.")


async def test_only_driving_keeps_the_original_wording(env):
    env.cfg.update(departure_mode_walk=False, departure_mode_transit=False)
    now, ev = _at(8)
    env.events["list"] = [ev]
    (action,) = await env.cog.predict_departure(env.hass, now)
    assert action["message"] == ("Heads up — Ballet at The School, Balbriggan begins in about 8 "
                                 "minutes; you'll want to head out.")
    assert [c[0] for c in env.calls] == ["get_travel_times"] and env.calls[0][1]["mode"] == "driving"


async def test_a_mode_turned_off_is_never_looked_up(env):
    env.cfg["departure_mode_walk"] = False
    now, ev = _at(8)
    env.events["list"] = [ev]
    await env.cog.predict_departure(env.hass, now)
    assert all(c[1].get("mode") != "walking" for c in env.calls)


async def test_google_can_be_switched_off(env, load, monkeypatch):
    env.cfg["departure_use_google"] = False
    travel = load("travel")

    async def _tm(hass, origin, dest, osrm_url=None):
        return 4.0
    monkeypatch.setattr(travel, "travel_minutes", _tm)
    now, ev = _at(8)
    env.events["list"] = [ev]
    (action,) = await env.cog.predict_departure(env.hass, now)
    assert env.calls == []                             # Google never asked
    assert "walking" not in action["message"]          # so only the drive time is used


async def test_without_the_integration_it_behaves_as_before(env, load, monkeypatch):
    env.entry = False
    travel = load("travel")

    async def _tm(hass, origin, dest, osrm_url=None):
        return 4.0
    monkeypatch.setattr(travel, "travel_minutes", _tm)
    now, ev = _at(8)                                   # 4 + 5 buffer = 9 minute lead
    env.events["list"] = [ev]
    (action,) = await env.cog.predict_departure(env.hass, now)
    assert action["message"].endswith("begins in about 8 minutes; you'll want to head out.")
    assert env.calls == []


async def test_a_failed_google_drive_falls_back_to_the_open_source_router(env, load, monkeypatch):
    env.values["drive"] = None
    env.cfg.update(departure_mode_walk=False, departure_mode_transit=False)
    travel = load("travel")

    async def _tm(hass, origin, dest, osrm_url=None):
        return 4.0
    monkeypatch.setattr(travel, "travel_minutes", _tm)
    now, ev = _at(8)
    env.events["list"] = [ev]
    (action,) = await env.cog.predict_departure(env.hass, now)
    assert "couldn't" not in action["message"]
    assert env.logged[0][0]["lead_from"] == "route"


async def test_when_every_lookup_fails_the_default_lead_is_used_and_said(env, load, monkeypatch):
    env.values.update(walk=None, drive=None, transit=None)
    travel = load("travel")

    async def _tm(hass, origin, dest, osrm_url=None):
        return None
    monkeypatch.setattr(travel, "travel_minutes", _tm)
    monkeypatch.setattr(travel, "failure_reason", lambda loc: "the routing service did not answer")
    now, ev = _at(27)
    env.events["list"] = [ev]
    (action,) = await env.cog.predict_departure(env.hass, now)
    assert action["message"].endswith("you'll want to head out. I couldn't work out the travel "
                                      "time, so I used the usual 30 minutes.")
    assert env.logged[0][0]["lead_from"] == "default"


async def test_lookups_are_cached_across_ticks(env):
    now, ev = _at(120)
    env.events["list"] = [ev]
    for i in range(6):
        await env.cog.predict_departure(env.hass, now + 30 * i)
    assert sorted(c[1].get("mode", "transit") for c in env.calls) == [
        "driving", "transit", "walking"]               # each mode once


async def test_walking_is_kept_longer_than_driving(env):
    now, ev = _at(120)
    env.events["list"] = [ev]
    await env.cog.predict_departure(env.hass, now)
    await env.cog.predict_departure(env.hass, now + env.cog.DEPART_ROUTE_TTL + 1)
    modes = [c[1].get("mode") for c in env.calls]
    assert modes.count("driving") == 2 and modes.count("walking") == 1


async def test_transit_asks_for_arrival_at_the_event_start(env):
    env.values["transit"] = 25.0
    now, ev = _at(120)
    env.events["list"] = [ev]
    await env.cog.predict_departure(env.hass, now)
    transit = [c for c in env.calls if c[0] == "get_transit_times"]
    assert len(transit) == 1
    assert transit[0][1]["arrival_time"] == ev["start"].strftime("%H:%M:%S")


async def test_the_decision_record_has_the_times_by_mode(env):
    now, ev = _at(17)
    env.events["list"] = [ev]
    await env.cog.predict_departure(env.hass, now)
    obs = env.logged[0][0]
    assert obs["minutes_by_mode"] == {"walk": 12.0, "drive": 3.0}
    assert obs["lead_from"] == "modes" and obs["people_home"] == 2


# ── later leave times ───────────────────────────────────────────────────────

async def test_a_later_leave_time_is_reminded_while_everyone_is_home(env):
    now, ev = _at(17)
    env.events["list"] = [ev]
    assert len(await env.cog.predict_departure(env.hass, now)) == 1
    assert await env.cog.predict_departure(env.hass, now + 60) == []        # not yet
    (later,) = await env.cog.predict_departure(env.hass, now + 9 * 60)
    assert later["message"] == ("Ballet begins in about 8 minutes and everyone is still home. "
                                "If you're driving, it's time to leave.")
    assert await env.cog.predict_departure(env.hass, now + 10 * 60) == []   # only once


async def test_no_reminder_if_someone_has_already_left(env):
    now, ev = _at(17)
    env.events["list"] = [ev]
    await env.cog.predict_departure(env.hass, now)
    env.hass.states.set("person.sam", "not_home")
    assert await env.cog.predict_departure(env.hass, now + 9 * 60) == []
    env.hass.states.set("person.sam", "home")
    assert await env.cog.predict_departure(env.hass, now + 10 * 60) == []   # not asked again


async def test_someone_arriving_does_not_stop_the_reminder(env):
    env.hass.states.set("person.sam", "not_home")
    now, ev = _at(17)
    env.events["list"] = [ev]
    await env.cog.predict_departure(env.hass, now)
    env.hass.states.set("person.sam", "home")                               # arrived since
    assert len(await env.cog.predict_departure(env.hass, now + 9 * 60)) == 1


async def test_no_reminder_if_nobody_was_home_at_the_first_alert(env):
    env.hass.states.set("person.abi", "not_home")
    env.hass.states.set("person.sam", "not_home")
    now, ev = _at(17)
    env.events["list"] = [ev]
    await env.cog.predict_departure(env.hass, now)
    env.hass.states.set("person.abi", "home")
    assert await env.cog.predict_departure(env.hass, now + 9 * 60) == []


async def test_a_leave_time_the_first_message_already_gave_is_not_repeated(env):
    now, ev = _at(8)                               # walking 17 is overdue, driving 8 is due too
    env.events["list"] = [ev]
    (first,) = await env.cog.predict_departure(env.hass, now)
    assert "head out now if you're walking, or head out now if you're driving" in first[
        "message"].lower()
    assert await env.cog.predict_departure(env.hass, now + 30) == []


async def test_no_reminder_once_the_event_has_started(env):
    now, ev = _at(17)
    env.events["list"] = [ev]
    await env.cog.predict_departure(env.hass, now)
    assert await env.cog.predict_departure(env.hass, now + 18 * 60) == []
    assert env.cog._DEPART_STAGES == {}


async def test_a_restart_between_alerts_does_not_repeat_the_first_alert(env):
    now, ev = _at(17)
    env.events["list"] = [ev]
    await env.cog.predict_departure(env.hass, now)
    env.cog._DEPART_STAGES.clear()                 # as after a restart
    env.cog._DEPART_TRAVEL.clear()
    assert await env.cog.predict_departure(env.hass, now + 9 * 60) == []


async def test_no_transit_route_is_asked_once_not_every_five_minutes(env):
    now, ev = _at(120)                                 # transit has no route (values["transit"] None)
    env.events["list"] = [ev]
    for minute in range(0, 20, 5):
        await env.cog.predict_departure(env.hass, now + minute * 60)
    transit = [c for c in env.calls if c[0] == "get_transit_times"]
    assert len(transit) == 1


async def test_a_switched_off_routes_api_is_retried_slowly(env):
    env.cog._DEPART_TRAVEL.clear()
    assert env.cog._retry_after("the Routes API is not enabled for your Google key", 1800) == 1800
    assert env.cog._retry_after("Google did not answer in time", 1800) == env.cog.DEPART_RETRY_AFTER
    assert env.cog._retry_after("Google found no walk route", 21600) == 21600
    assert env.cog._retry_after(None, 1800) == env.cog.DEPART_RETRY_AFTER
