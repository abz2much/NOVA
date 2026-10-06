"""Country-aware dates and the exact legacy US strings at four call sites."""
from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
import types

import pytest

from fakes import FakeHass


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 6, 16, 5)
        return value.replace(tzinfo=tz) if tz else value


def _hass(country=...):
    hass = FakeHass()
    config = types.SimpleNamespace(
        units=types.SimpleNamespace(temperature_unit="°C"),
        latitude=53.35, longitude=-6.26,
    )
    if country is not ...:
        config.country = country
    hass.config = config
    return hass


@pytest.mark.parametrize("country", ["IE", "GB", "DE"])
@pytest.mark.parametrize(("day", "expected"), [
    (6, "Tuesday 6 October"),
    (16, "Friday 16 October"),
])
def test_day_first_countries(load, country, day, expected):
    fmt = load("locale_format")
    assert fmt.format_date(datetime(2026, 10, day), _hass(country)) == expected


@pytest.mark.parametrize("country", ["US", "CA", "PH"])
def test_month_first_countries_keep_the_old_spoken_form(load, country):
    fmt = load("locale_format")
    assert fmt.format_date(FixedDateTime.now(), _hass(country)) == "Tuesday October 6"


@pytest.mark.parametrize("hass", [None, _hass(None), _hass()])
def test_missing_country_keeps_the_old_us_forms(load, hass):
    fmt = load("locale_format")
    now = FixedDateTime.now()
    assert fmt.format_date(now, hass) == "Tuesday October 6"
    assert fmt.format_date(now, hass, pad_month_first_day=True) == "Tuesday October 06"
    assert fmt.format_date(now, hass, include_year=True) == "Tuesday, October 06, 2026"


def test_day_first_year_has_no_comma_or_zero_padding(load):
    fmt = load("locale_format")
    assert fmt.format_date(FixedDateTime.now(), _hass("IE"), include_year=True) == (
        "Tuesday 6 October 2026")


async def test_briefing_keeps_its_exact_us_date_and_time(load, monkeypatch):
    br = load("briefing")
    activity = load("providers.activity")
    hass = _hass("US")
    monkeypatch.setattr(br, "datetime", FixedDateTime)
    monkeypatch.setattr(br, "save_message", lambda *args: None)
    monkeypatch.setattr(br, "_gather_open_things", lambda hass: [])
    monkeypatch.setattr(br, "_gather_weather", lambda hass: "")
    monkeypatch.setattr(br, "_gather_overnight_events", lambda *args: [])
    monkeypatch.setattr(br, "_gather_calendar", lambda hass: [])
    monkeypatch.setattr(br, "_gather_energy_anomalies", lambda hass: [])

    async def reply(*args, **kwargs):
        return types.SimpleNamespace(text="")

    monkeypatch.setattr(activity, "execute_chat", reply)
    call = types.SimpleNamespace(data={
        "announce": False, "include_weather": False, "include_calendar": False,
        "include_presence": False, "include_events": False, "include_energy": False,
        "include_hazards": False,
    })
    result = await br.async_briefing(hass, call, object(), "sir", None, [])
    assert result["context"].splitlines()[0] == "It is Tuesday October 6, 4:05 PM."


def test_conversation_context_keeps_its_exact_us_date_and_time(load):
    fmt = load("locale_format")
    hass = _hass("US")
    path = Path(__file__).resolve().parents[2] / "custom_components/nova/conversation.py"
    tree = ast.parse(path.read_text())
    fn = next(node for node in tree.body
              if isinstance(node, ast.FunctionDef) and node.name == "_current_time_context")
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"format_date": fmt.format_date}
    exec(compile(module, str(path), "exec"), namespace)
    assert namespace["_current_time_context"](hass, FixedDateTime.now()) == (
        "Current time: Tuesday October 6, 4:05 PM.")


def test_home_state_keeps_its_exact_us_date_and_time(load, monkeypatch):
    home = load("home_state")
    audio = load("audio_routing")
    core = load("cognitive_core")
    alarm = load("alarm_source")
    dt_util = __import__("homeassistant.util.dt", fromlist=["now"])
    hass = _hass("US")
    monkeypatch.setattr(dt_util, "now", FixedDateTime.now)
    monkeypatch.setattr(audio, "currently_occupied_areas", lambda hass: [])
    monkeypatch.setattr(audio, "anyone_home", lambda hass: False)
    monkeypatch.setattr(core, "_lockdown_exempt_locks", lambda: set())
    monkeypatch.setattr(alarm, "states", lambda hass: [])
    first = home._build_summary(hass).splitlines()[0]
    assert first == "Current time: 04:05 PM, Tuesday October 06"


def test_local_engine_keeps_its_exact_us_year_form(load, monkeypatch):
    engine = load("local_engine")
    dt_util = __import__("homeassistant.util.dt", fromlist=["now"])
    monkeypatch.setattr(dt_util, "now", FixedDateTime.now)
    assert engine._query_resp(_hass("US"), "query_date", "", "", "sir") == (
        "Today is Tuesday, October 06, 2026, sir.")
