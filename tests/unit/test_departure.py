"""Leave-time alerts for calendar events, driving only.

When the next timed event with a real place is close enough that it is time
to set off, predict_departure gives one heads-up. The lead is the drive time
plus a short buffer: from a travel sensor if one is set, else from the
open-source router (travel.py), else the fixed default lead, which the
message then admits to. Walking and public transport are covered in
test_departure_modes.py; here Google is always unavailable.

Every tick runs at a fixed clock so results never depend on the time of day.
"""
import datetime
import types

import pytest

# Midday on an ordinary weekday, local time.
NOON = datetime.datetime(2026, 3, 10, 12, 0)
T0 = NOON.timestamp()
PLACE = "1 Example Road"
ORIGIN = (10.0, 20.0)


class Trip:
    """One test's calendar, settings, route answers and recorded decisions."""

    def __init__(self, cog, hass):
        self.cog = cog
        self.hass = hass
        self.events = []
        self.settings = {"departure_alerts_enabled": True, "departure_lead_minutes": 30}
        self.route = None             # minutes the router answers, or None
        self.route_failure = None     # why the router gave nothing
        self.route_asks = []
        self.decisions = []

    def event_in(self, minutes, title="Dentist", **fields):
        start = NOON + datetime.timedelta(minutes=minutes)
        event = {"calendar": "calendar.family", "title": title, "start": start,
                 "end": start + datetime.timedelta(hours=1), "all_day": False,
                 "location": PLACE, "active": False}
        event.update(fields)
        self.events.append(event)
        return event

    def from_origin(self, monkeypatch, where=ORIGIN):
        monkeypatch.setattr(self.cog, "_current_origin", lambda _hass: where)

    async def tick(self, after=0.0):
        return await self.cog.predict_departure(self.hass, T0 + after)


@pytest.fixture
def trip(load, monkeypatch, fake_hass):
    cog = load("cognition")
    # Fresh per-test memory; the module's own dicts come back afterwards.
    for name in ("_RECUR_ALERTED", "_DEPART_TRAVEL", "_DEPART_STAGES"):
        monkeypatch.setattr(cog, name, {})
    state = Trip(cog, fake_hass)

    monkeypatch.setattr(load("comms"), "gather_events", lambda _hass: state.events)
    monkeypatch.setattr(load("nova_config"), "get",
                        lambda key, default=None: state.settings.get(key, default))
    monkeypatch.setattr(load("google_travel"), "available", lambda _hass: False)

    travel = load("travel")

    async def route(_hass, origin, dest, osrm_url=None):
        state.route_asks.append((origin, dest, osrm_url))
        return state.route
    monkeypatch.setattr(travel, "travel_minutes", route)
    monkeypatch.setattr(travel, "failure_reason", lambda _loc: state.route_failure)

    def log_decision(kind, observed, meaning, decision, reason, **extra):
        state.decisions.append(types.SimpleNamespace(
            kind=kind, observed=observed, meaning=meaning, decision=decision, reason=reason))
        return 500 + len(state.decisions)
    monkeypatch.setattr(cog, "_log_decision", log_decision)
    return state


# ── When it speaks ──────────────────────────────────────────────────────────

async def test_the_heads_up_once_it_is_time_to_leave(trip, monkeypatch):
    trip.from_origin(monkeypatch)
    trip.route = 25.0
    # Twenty minutes out with a 25 + 5 minute lead: leave time was ten ago.
    trip.event_in(20)
    (alert,) = await trip.tick()
    assert alert == {
        "type": "anticipation_departure", "urgency": "low",
        "message": f"Heads up — Dentist at {PLACE} begins in about 20 minutes; "
                   "you'll want to head out.",
        "pattern_key": "depart:Dentist:202603101220", "offer": False, "decision_id": 501,
    }
    (record,) = trip.decisions
    assert record.kind == "anticipation_departure"
    assert record.decision == "announce departure heads-up"
    assert record.observed["lead_from"] == "route"
    assert record.observed["lead_minutes"] == 30
    assert record.observed["minutes_until"] == 20
    assert record.observed["minutes_by_mode"] == {"drive": 25.0}


@pytest.mark.parametrize("minutes_out, alerts", [
    (90, False), (31, False), (30, True), (1, True),
])
async def test_the_leave_time_edge(trip, minutes_out, alerts):
    trip.event_in(minutes_out)
    assert bool(await trip.tick()) is alerts


async def test_one_alert_per_event_per_day(trip):
    trip.event_in(20)
    assert len(await trip.tick()) == 1
    assert await trip.tick() == []
    assert await trip.tick(after=60) == []
    assert len(trip.decisions) == 1


async def test_one_alert_per_event_across_midnight(trip):
    # A 00:10 event alerts at 23:50; the date changing at midnight must not
    # make the same event alert again.
    midnight = datetime.datetime(2026, 3, 11, 0, 0)
    start = midnight + datetime.timedelta(minutes=10)
    trip.events.append({"calendar": "calendar.family", "title": "Pickup", "start": start,
                        "end": start + datetime.timedelta(hours=1), "all_day": False,
                        "location": PLACE, "active": False})
    before = (midnight - datetime.timedelta(minutes=10)).timestamp()
    assert len(await trip.cog.predict_departure(trip.hass, before)) == 1
    for after in (11 * 60, 12 * 60, 15 * 60):
        assert await trip.cog.predict_departure(trip.hass, before + after) == []
    assert len(trip.decisions) == 1


@pytest.mark.parametrize("settings", [
    {"departure_alerts_enabled": False},
    {"departure_alerts_enabled": None},
])
async def test_switched_off_says_nothing(trip, settings):
    trip.settings.update(settings)
    trip.event_in(20)
    assert await trip.tick() == []


async def test_switched_off_unless_set(trip):
    del trip.settings["departure_alerts_enabled"]
    trip.event_in(20)
    assert await trip.tick() == []


@pytest.mark.parametrize("fields", [
    {"all_day": True},
    {"location": None},
    {"location": ""},
    {"location": "Zoom"},
    {"start": None},
], ids=["all-day", "no-place", "blank-place", "a-call", "no-start"])
async def test_events_with_nowhere_to_go_are_skipped(trip, monkeypatch, fields):
    trip.from_origin(monkeypatch)
    trip.route = 45.0
    trip.event_in(20, **fields)
    assert await trip.tick() == []
    # No route is looked up for an event that was never a candidate.
    assert trip.route_asks == []


async def test_events_past_the_three_hour_horizon_are_skipped(trip):
    trip.event_in(5 * 60)
    assert await trip.tick() == []


async def test_started_events_give_way_to_the_next(trip):
    trip.event_in(-5, title="Meeting")
    trip.event_in(20, title="Dentist")
    (alert,) = await trip.tick()
    assert alert["pattern_key"].startswith("depart:Dentist:")


async def test_only_the_nearest_due_event_alerts_per_tick(trip):
    trip.event_in(25, title="Later")
    trip.event_in(15, title="Sooner")
    (alert,) = await trip.tick()
    assert "Sooner" in alert["message"]
    # The other one has its turn on the next tick.
    (second,) = await trip.tick()
    assert "Later" in second["message"]


async def test_an_empty_calendar_is_quiet(trip):
    assert await trip.tick() == []


async def test_a_broken_calendar_never_raises(trip, load, monkeypatch):
    def _broken(_hass):
        raise RuntimeError("calendar offline")
    monkeypatch.setattr(load("comms"), "gather_events", _broken)
    assert await trip.tick() == []


@pytest.mark.parametrize("lead, minutes_out, alerts", [
    (45, 40, True), (45, 50, False), ("abc", 25, True), ("abc", 35, False), (0, 25, True),
])
async def test_the_default_lead_setting(trip, lead, minutes_out, alerts):
    # A lead that will not read as a number, or 0, falls back to thirty minutes.
    trip.settings["departure_lead_minutes"] = lead
    trip.event_in(minutes_out)
    assert bool(await trip.tick()) is alerts


# ── Calendars left out ─────────────────────────────────────────────────────

@pytest.mark.parametrize("excluded", [
    ["calendar.birthdays"], '["calendar.birthdays"]',
], ids=["list", "json-text"])
async def test_an_excluded_calendar_is_ignored(trip, excluded):
    trip.settings["departure_excluded_calendars"] = excluded
    trip.event_in(20, title="Party", calendar="calendar.birthdays")
    assert await trip.tick() == []
    trip.event_in(20, title="Ballet", calendar="calendar.family")
    (alert,) = await trip.tick()
    assert "Ballet" in alert["message"]


@pytest.mark.parametrize("excluded", ["not json", "", 7])
async def test_an_unreadable_exclusion_list_excludes_nothing(trip, excluded):
    trip.settings["departure_excluded_calendars"] = excluded
    trip.event_in(20, calendar="calendar.birthdays")
    assert len(await trip.tick()) == 1


# ── Where the lead comes from ──────────────────────────────────────────────

async def test_a_travel_sensor_stretches_the_lead(trip):
    trip.event_in(40)
    # Default thirty minute lead: leave in ten, so nothing yet.
    assert await trip.tick() == []

    trip.settings["departure_travel_sensor"] = "sensor.commute"
    trip.hass.states.set("sensor.commute", "45")
    # 45 + 5 buffer = 50: the leave time was ten minutes ago.
    (alert,) = await trip.tick()
    assert "couldn't" not in alert["message"]
    assert trip.decisions[-1].observed["lead_from"] == "sensor"
    assert trip.decisions[-1].observed["lead_minutes"] == 50
    assert "travel sensor reading" in trip.decisions[-1].reason


@pytest.mark.parametrize("reading", ["unavailable", "unknown", "soon", None])
async def test_a_dead_travel_sensor_is_ignored(trip, reading):
    trip.settings["departure_travel_sensor"] = "sensor.commute"
    if reading is not None:
        trip.hass.states.set("sensor.commute", reading)
    trip.event_in(40)
    assert await trip.tick() == []


async def test_the_router_drive_time_sets_the_lead(trip, monkeypatch):
    trip.from_origin(monkeypatch)
    trip.route = 45.0
    trip.event_in(40, location="123 Main St")
    # 45 + 5 = 50 minutes before an event forty minutes away.
    (alert,) = await trip.tick()
    assert "123 Main St" in alert["message"]
    assert trip.route_asks == [(ORIGIN, "123 Main St", None)]


async def test_the_router_url_setting_is_passed_on(trip, monkeypatch):
    trip.from_origin(monkeypatch)
    trip.settings["departure_osrm_url"] = "  http://router.invalid  "
    trip.route = 4.0
    trip.event_in(8)
    await trip.tick()
    assert trip.route_asks[0][2] == "http://router.invalid"


async def test_a_working_route_is_named_as_the_source(trip, monkeypatch):
    trip.from_origin(monkeypatch)
    trip.route = 4.0                 # 4 + 5 buffer = 9 minute lead
    trip.event_in(8, location="School")
    (alert,) = await trip.tick()
    assert "couldn't" not in alert["message"]
    observed = trip.decisions[0].observed
    assert (observed["lead_from"], observed["lead_minutes"]) == ("route", 9)
    assert observed["travel_lookup_failed"] is None
    assert "estimated drive time" in trip.decisions[0].reason


async def test_a_failed_route_falls_back_and_admits_it(trip, monkeypatch):
    trip.from_origin(monkeypatch)
    trip.route_failure = "the routing service did not answer"
    trip.event_in(27, location="School")
    (alert,) = await trip.tick()
    assert alert["message"].endswith(
        "I couldn't work out the travel time, so I used the usual 30 minutes.")
    observed = trip.decisions[0].observed
    assert observed["lead_from"] == "default"
    assert observed["travel_lookup_failed"] == "the routing service did not answer"
    assert "default lead time" in trip.decisions[0].reason


async def test_an_unknown_position_is_admitted_not_hidden(trip, monkeypatch):
    trip.from_origin(monkeypatch, where=None)
    trip.event_in(20, location="School")
    (alert,) = await trip.tick()
    assert "couldn't work out the travel time" in alert["message"]
    assert trip.decisions[0].observed["travel_lookup_failed"] == "Nova does not know where you are"
    # With no starting point there is nothing to ask the router.
    assert trip.route_asks == []


# ── Lookups are cached between ticks ───────────────────────────────────────

@pytest.mark.parametrize("answer, ttl_name", [
    (8.0, "DEPART_ROUTE_TTL"),
    (None, "DEPART_RETRY_AFTER"),
], ids=["success", "failure"])
async def test_a_lookup_is_reused_until_it_goes_stale(trip, monkeypatch, answer, ttl_name):
    trip.from_origin(monkeypatch)
    trip.route = answer
    trip.event_in(120, location="School")
    ttl = getattr(trip.cog, ttl_name)

    # Five ticks thirty seconds apart ask only once.
    for step in range(5):
        assert await trip.tick(after=30 * step) == []
    assert len(trip.route_asks) == 1
    # Just inside the window still reuses it; just past it asks again.
    await trip.tick(after=ttl - 1)
    assert len(trip.route_asks) == 1
    await trip.tick(after=ttl + 1)
    assert len(trip.route_asks) == 2


async def test_constants_hold_their_documented_values(trip):
    cog = trip.cog
    assert (cog.DEFAULT_DEPART_LEAD_MIN, cog.DEPART_PREP_BUFFER_MIN,
            cog.DEPART_LOOKAHEAD_H) == (30, 5, 3)
    assert (cog.DEPART_ROUTE_TTL, cog.DEPART_RETRY_AFTER) == (1800, 300)
