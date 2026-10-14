"""Fresh behaviour tests for situation.py, written from the spec."""
import datetime as dt
import sys
import types

import pytest


@pytest.fixture
def sit(load):
    return load("situation")


@pytest.fixture
def clock(monkeypatch):
    """Pin homeassistant.util.dt.now() to a given local datetime."""
    dt_mod = sys.modules["homeassistant.util.dt"]

    def _set(value):
        monkeypatch.setattr(dt_mod, "now", lambda tz=None: value)
    return _set


@pytest.fixture
def recent(monkeypatch):
    """Install a stand-in observer whose recent context is `text`."""
    seen = []

    def _set(text=None, exc=None):
        mod = types.ModuleType("jc.observer")

        def get_recent_context(seconds=600):
            seen.append(seconds)
            if exc:
                raise exc
            return text
        mod.get_recent_context = get_recent_context
        monkeypatch.setitem(sys.modules, "jc.observer", mod)
        return seen
    return _set


@pytest.fixture
def no_rooms(load, monkeypatch):
    presence = load("presence")
    monkeypatch.setattr(presence, "get_presence_summary", lambda hass: {"rooms": {}})
    return presence


def _only(hass, monkeypatch, domain, states):
    monkeypatch.setattr(hass.states, "async_all",
                        lambda d=None: list(states) if d == domain else [])


# ── _time ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("when, text", [
    (dt.datetime(2025, 6, 2, 9, 5), "Time: Monday 9:05 AM"),
    (dt.datetime(2025, 6, 6, 12, 30), "Time: Friday 12:30 PM"),
    (dt.datetime(2025, 6, 8, 22, 0), "Time: Sunday 10:00 PM"),
    (dt.datetime(2025, 6, 3, 0, 7), "Time: Tuesday 12:07 AM"),
])
def test_time_line(sit, fake_hass, clock, when, text):
    clock(when)
    assert sit._time(fake_hass) == text


def test_time_failure_is_empty(sit, fake_hass, monkeypatch):
    def boom(tz=None):
        raise RuntimeError("no clock")
    monkeypatch.setattr(sys.modules["homeassistant.util.dt"], "now", boom)
    assert sit._time(fake_hass) == ""


# ── _presence ─────────────────────────────────────────────────────────────────

def test_presence_none_is_empty(sit, fake_hass, no_rooms):
    assert sit._presence(fake_hass) == ""


def test_presence_single_home(sit, fake_hass, no_rooms):
    fake_hass.states.set("person.alex", "home", friendly_name="Alex")
    assert sit._presence(fake_hass) == "Presence: Alex is home."


def test_presence_phrases_follow_household_rules(sit, fake_hass, no_rooms):
    fake_hass.states.set("person.alex", "not_home", friendly_name="Alex",
                         device_trackers=["device_tracker.alex_phone"])
    fake_hass.states.set("device_tracker.alex_phone", "home")
    fake_hass.states.set("person.blake", "unknown", friendly_name="Blake")
    fake_hass.states.set("person.casey", "away", friendly_name="Casey")
    fake_hass.states.set("person.drew", "gym_downtown", friendly_name="Drew")
    assert sit._presence(fake_hass) == (
        "Presence: Alex is home. Blake's whereabouts are unknown. "
        "Casey is away. Drew is at gym downtown.")


def test_presence_name_falls_back_to_object_id(sit, fake_hass, no_rooms):
    fake_hass.states.set("person.sam_lee", "not_home")
    assert sit._presence(fake_hass) == "Presence: sam_lee is away."


def test_presence_appends_occupied_areas(sit, fake_hass, load, monkeypatch):
    presence = load("presence")
    monkeypatch.setattr(presence, "get_presence_summary",
                        lambda hass: {"rooms": {"Kitchen": ["Alex"], "Office": ["Blake"]}})
    fake_hass.states.set("person.alex", "home", friendly_name="Alex")
    assert sit._presence(fake_hass) == (
        "Presence: Alex is home. Occupied areas: Kitchen, Office.")


def test_presence_summary_failure_keeps_the_line(sit, fake_hass, load, monkeypatch):
    presence = load("presence")

    def boom(hass):
        raise RuntimeError("sensors gone")
    monkeypatch.setattr(presence, "get_presence_summary", boom)
    fake_hass.states.set("person.alex", "home", friendly_name="Alex")
    assert sit._presence(fake_hass) == "Presence: Alex is home."


def test_presence_summary_none_keeps_the_line(sit, fake_hass, load, monkeypatch):
    monkeypatch.setattr(load("presence"), "get_presence_summary", lambda hass: None)
    fake_hass.states.set("person.alex", "home", friendly_name="Alex")
    assert sit._presence(fake_hass) == "Presence: Alex is home."


def test_presence_reader_failure_is_empty(sit, fake_hass, load, monkeypatch, no_rooms):
    household = load("household")

    def boom(hass, person):
        raise RuntimeError("bad")
    monkeypatch.setattr(household, "_person_reading", boom)
    fake_hass.states.set("person.alex", "home", friendly_name="Alex")
    assert sit._presence(fake_hass) == ""


# ── _weather ──────────────────────────────────────────────────────────────────

def test_weather_with_temperature(sit, fake_hass, monkeypatch):
    st = fake_hass.states.set("weather.home", "partlycloudy", temperature=18,
                              temperature_unit="°C")
    _only(fake_hass, monkeypatch, "weather", [st])
    assert sit._weather(fake_hass) == "Weather: partlycloudy, 18°C"


def test_weather_underscores_and_zero_temperature(sit, fake_hass, monkeypatch):
    st = fake_hass.states.set("weather.home", "clear_night", temperature=0)
    _only(fake_hass, monkeypatch, "weather", [st])
    assert sit._weather(fake_hass) == "Weather: clear night, 0"


def test_weather_without_temperature(sit, fake_hass, monkeypatch):
    st = fake_hass.states.set("weather.home", "rainy")
    _only(fake_hass, monkeypatch, "weather", [st])
    assert sit._weather(fake_hass) == "Weather: rainy"


def test_weather_empty_condition_is_empty(sit, fake_hass, monkeypatch):
    st = fake_hass.states.set("weather.home", "", temperature=20, temperature_unit="F")
    _only(fake_hass, monkeypatch, "weather", [st])
    assert sit._weather(fake_hass) == ""


def test_weather_uses_first_entity_only(sit, fake_hass, monkeypatch):
    a = fake_hass.states.set("weather.one", "sunny", temperature=70, temperature_unit="F")
    b = fake_hass.states.set("weather.two", "snowy", temperature=20, temperature_unit="F")
    _only(fake_hass, monkeypatch, "weather", [a, b])
    assert sit._weather(fake_hass) == "Weather: sunny, 70F"


def test_weather_none_is_empty(sit, fake_hass):
    assert sit._weather(fake_hass) == ""


# ── _calendar ─────────────────────────────────────────────────────────────────

def test_calendar_two_events_and_first_conflict(sit, fake_hass, load, monkeypatch):
    asked = []
    monkeypatch.setattr(load("comms"), "agenda", lambda hass, horizon=24: asked.append(horizon) or {
        "events": ["Standup - 9:00 AM", "Gym - 6:00 PM", "Late call - 9 PM"],
        "conflicts": ["overlap: A and B", "second"]})
    assert sit._calendar(fake_hass) == (
        "Next up: Standup - 9:00 AM; Gym - 6:00 PM (overlap: A and B)")
    assert asked == [12]


def test_calendar_single_event_no_conflicts(sit, fake_hass, load, monkeypatch):
    monkeypatch.setattr(load("comms"), "agenda", lambda hass, h=24: {"events": ["Dentist - 2 PM"]})
    assert sit._calendar(fake_hass) == "Next up: Dentist - 2 PM"


@pytest.mark.parametrize("value", [None, {}, {"events": []}, {"events": None}])
def test_calendar_nothing_is_empty(sit, fake_hass, load, monkeypatch, value):
    monkeypatch.setattr(load("comms"), "agenda", lambda hass, h=24: value)
    assert sit._calendar(fake_hass) == ""


def test_calendar_failure_is_empty(sit, fake_hass, load, monkeypatch):
    def boom(hass, h=24):
        raise RuntimeError("calendar down")
    monkeypatch.setattr(load("comms"), "agenda", boom)
    assert sit._calendar(fake_hass) == ""


# ── _energy ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status, text", [
    ({"kw": 1.24, "over_peak": False}, "Power: 1.2 kW"),
    ({"kw": 0}, "Power: 0.0 kW"),
    ({"kw": 8.56, "over_peak": True}, "Power: 8.6 kW — over peak"),
])
def test_energy_line(sit, fake_hass, load, monkeypatch, status, text):
    monkeypatch.setattr(load("energy"), "power_status", lambda hass: status)
    assert sit._energy(fake_hass) == text


@pytest.mark.parametrize("status", [None, {}, {"kw": None, "over_peak": True}, {"kw": "lots"}])
def test_energy_missing_or_bad_is_empty(sit, fake_hass, load, monkeypatch, status):
    monkeypatch.setattr(load("energy"), "power_status", lambda hass: status)
    assert sit._energy(fake_hass) == ""


# ── _activity ─────────────────────────────────────────────────────────────────

def test_activity_last_three_lines(sit, fake_hass, recent):
    seen = recent("  [5m ago] Hall light: off → on\n\n"
                  "  [4m ago] Back door: closed → open\n"
                  "  [2m ago] Garage: closed → open\n"
                  "  [1m ago] Kettle: off → on\n")
    assert sit._activity(fake_hass) == (
        "Recent activity: [4m ago] Back door: closed → open; "
        "[2m ago] Garage: closed → open; [1m ago] Kettle: off → on")
    assert seen == [600]


@pytest.mark.parametrize("text", ["quiet — no notable recent activity", "  quiet", "", None, "   "])
def test_activity_quiet_or_empty(sit, fake_hass, recent, text):
    recent(text)
    assert sit._activity(fake_hass) == ""


def test_activity_failure_is_empty(sit, fake_hass, recent):
    recent(exc=RuntimeError("observer off"))
    assert sit._activity(fake_hass) == ""


# ── snapshot ──────────────────────────────────────────────────────────────────

def test_snapshot_order_and_join(sit, fake_hass, load, monkeypatch, clock, recent, no_rooms):
    clock(dt.datetime(2025, 6, 2, 7, 45))
    person = fake_hass.states.set("person.alex", "home", friendly_name="Alex")
    weather = fake_hass.states.set("weather.home", "sunny", temperature=21,
                                   temperature_unit="°C")
    monkeypatch.setattr(fake_hass.states, "async_all", lambda d=None: (
        [person] if d == "person" else [weather] if d == "weather" else []))
    monkeypatch.setattr(load("comms"), "agenda", lambda hass, h=24: {"events": ["School run - 8 AM"]})
    monkeypatch.setattr(load("energy"), "power_status", lambda hass: {"kw": 2.0})
    recent("  [1m ago] Front door: closed → open")
    assert sit.snapshot(fake_hass) == "\n".join([
        "Time: Monday 7:45 AM",
        "Presence: Alex is home.",
        "Weather: sunny, 21°C",
        "Next up: School run - 8 AM",
        "Power: 2.0 kW",
        "Recent activity: [1m ago] Front door: closed → open",
    ])


def test_snapshot_skips_empty_and_raising_lines(sit, fake_hass, load, monkeypatch, clock, recent):
    clock(dt.datetime(2025, 6, 2, 7, 45))
    monkeypatch.setattr(load("comms"), "agenda", lambda hass, h=24: {"events": []})
    monkeypatch.setattr(load("energy"), "power_status", lambda hass: {"kw": 0.5})
    recent("quiet")
    assert sit.snapshot(fake_hass) == "Time: Monday 7:45 AM\nPower: 0.5 kW"


def test_snapshot_everything_missing_is_empty(sit, fake_hass, load, monkeypatch, recent):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(sys.modules["homeassistant.util.dt"], "now", boom)
    monkeypatch.setattr(load("comms"), "agenda", boom)
    monkeypatch.setattr(load("energy"), "power_status", boom)
    recent(exc=RuntimeError("x"))
    assert sit.snapshot(fake_hass) == ""
