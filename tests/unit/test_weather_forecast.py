"""The weather_forecast tool (v6.75.0).

Before it, Nova saw only the current conditions, so "what time is it meant to
rain?" had no real answer and the model fell back to Home Assistant's clock
intent. The tool calls weather.get_forecasts; here that service is a scripted
stand-in so each case controls exactly what comes back.
"""
import json

import pytest

from fakes import FakeHass


@pytest.fixture
def agent(load):
    return load("agent")


_SCRIPTED = object()


class _ForecastService:
    """weather.get_forecasts, scripted per forecast type. A type listed in
    `fail` raises the way an entity that lacks it does in Home Assistant."""

    def __init__(self, periods=None, fail=(), reply=_SCRIPTED):
        self.periods = periods or []
        self.fail = set(fail)
        self.reply = reply          # a raw reply (None included) for odd shapes
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False,
                         return_response=False):
        self.calls.append({"domain": domain, "service": service, "data": dict(data),
                           "blocking": blocking, "return_response": return_response})
        if data["type"] in self.fail:
            raise RuntimeError(f"entity does not support {data['type']} forecasts")
        if self.reply is not _SCRIPTED:
            return self.reply
        return {data["entity_id"]: {"forecast": self.periods}}


class _WeatherHome(FakeHass):
    def __init__(self, service, **entities):
        super().__init__()
        self._services = service
        for eid, (state, attrs) in entities.items():
            self.states.set(eid.replace("__", "."), state, **attrs)


def _home(service=None, state="sunny", **attrs):
    service = service or _ForecastService()
    return _WeatherHome(service, weather__home=(state, attrs)), service


async def _forecast(agent, hass, **args):
    return json.loads(await agent._exec_weather_forecast(hass, args))


def _hour(hour, condition="sunny", **extra):
    return {"datetime": f"2026-08-08T{hour:02d}:00:00+00:00", "condition": condition,
            "temperature": 70, **extra}


# ── the happy path ──────────────────────────────────────────────────────────

async def test_hourly_answer_says_when_it_rains(agent):
    periods = [
        {"datetime": "2026-08-08T03:00:00+00:00", "condition": "cloudy",
         "temperature": 71, "precipitation_probability": 20},
        {"datetime": "2026-08-08T05:00:00+00:00", "condition": "rainy",
         "temperature": 68, "precipitation": 0.3, "precipitation_probability": 80},
    ]
    hass, _svc = _home(_ForecastService(periods), state="cloudy", temperature=72)
    out = await _forecast(agent, hass, kind="hourly")
    assert out["entity_id"] == "weather.home"
    assert out["type"] == "hourly"
    assert out["forecast"] == [
        {"datetime": "2026-08-08T03:00:00+00:00", "condition": "cloudy",
         "temperature": 71, "precipitation_probability": 20},
        {"datetime": "2026-08-08T05:00:00+00:00", "condition": "rainy",
         "temperature": 68, "precipitation": 0.3, "precipitation_probability": 80},
    ]
    assert "datetime" in out["note"]                  # tells the model how to read it


async def test_current_conditions_come_along(agent):
    hass, _svc = _home(state="sunny", temperature=80)
    out = await _forecast(agent, hass)
    assert out["current"] == {"condition": "sunny", "temperature": 80}


async def test_service_is_asked_properly(agent):
    hass, svc = _home()
    await _forecast(agent, hass)
    (call,) = svc.calls
    assert (call["domain"], call["service"]) == ("weather", "get_forecasts")
    assert call["data"] == {"entity_id": "weather.home", "type": "hourly"}
    assert call["blocking"] is True and call["return_response"] is True


# ── choosing the type ───────────────────────────────────────────────────────

@pytest.mark.parametrize("kind, sent", [
    (None, "hourly"),          # not given: hourly
    ("", "hourly"),
    ("daily", "daily"),
    ("DAILY", "daily"),
    ("twice_daily", "twice_daily"),
    ("weekly", "hourly"),      # unknown: hourly
])
async def test_kind_mapping(agent, kind, sent):
    hass, svc = _home()
    args = {} if kind is None else {"kind": kind}
    out = await _forecast(agent, hass, **args)
    assert svc.calls[0]["data"]["type"] == sent
    assert out["type"] == sent


# ── which entity ────────────────────────────────────────────────────────────

async def test_named_entity_is_used(agent):
    svc = _ForecastService([_hour(9)])
    hass = _WeatherHome(svc, weather__home=("sunny", {}),
                        weather__cabin=("snowy", {"temperature": 20}))
    out = await _forecast(agent, hass, entity_id="weather.cabin")
    assert svc.calls[0]["data"]["entity_id"] == "weather.cabin"
    assert out["entity_id"] == "weather.cabin"
    assert out["current"] == {"condition": "snowy", "temperature": 20}


async def test_no_weather_entity_is_a_clean_error(agent):
    svc = _ForecastService()
    hass = _WeatherHome(svc, sensor__outside=("12", {}))
    out = await _forecast(agent, hass)
    assert "no weather entity" in out["error"].lower()
    assert svc.calls == []


# ── when the entity cannot give that type ───────────────────────────────────

async def test_unsupported_hourly_falls_back_to_daily(agent):
    svc = _ForecastService([_hour(12, "rainy")], fail={"hourly"})
    hass, _svc = _home(svc)
    out = await _forecast(agent, hass, kind="hourly")
    assert [c["data"]["type"] for c in svc.calls] == ["hourly", "daily"]
    assert out["type"] == "daily"                     # says what it really got
    assert out["forecast"][0]["condition"] == "rainy"


async def test_both_types_failing_is_a_clean_error(agent):
    svc = _ForecastService(fail={"hourly", "daily"})
    hass, _svc = _home(svc)
    out = await _forecast(agent, hass, kind="hourly")
    assert set(out) == {"error"}
    assert out["error"].startswith("forecast unavailable:")
    assert "hourly" in out["error"]                   # the first failure is reported
    assert any(c["data"]["type"] == "daily" for c in svc.calls)


async def test_daily_failure_is_not_retried(agent):
    svc = _ForecastService(fail={"daily"})
    hass, _svc = _home(svc)
    out = await _forecast(agent, hass, kind="daily")
    assert out["error"].startswith("forecast unavailable:")
    assert len(svc.calls) == 1


# ── shaping the entries ─────────────────────────────────────────────────────

async def test_at_most_a_day_of_entries(agent):
    periods = [_hour(h) for h in range(24)] * 3              # 72 entries
    hass, _svc = _home(_ForecastService(periods))
    out = await _forecast(agent, hass)
    assert len(out["forecast"]) == 24
    assert out["forecast"][0]["datetime"].startswith("2026-08-08T00:00")


async def test_extras_only_when_given(agent):
    periods = [_hour(1, wind_speed=12, humidity=None, templow=55, uv_index=3)]
    hass, _svc = _home(_ForecastService(periods))
    (entry,) = (await _forecast(agent, hass))["forecast"]
    assert entry == {"datetime": "2026-08-08T01:00:00+00:00", "condition": "sunny",
                     "temperature": 70, "wind_speed": 12, "templow": 55}


@pytest.mark.parametrize("reply", [None, {}, {"weather.other": {"forecast": [_hour(2)]}},
                                   {"weather.home": {"forecast": None}}])
async def test_empty_or_foreign_reply_gives_no_entries(agent, reply):
    hass, _svc = _home(_ForecastService(reply=reply))
    out = await _forecast(agent, hass)
    assert out["forecast"] == []


async def test_unreadable_reply_is_a_clean_error(agent):
    hass, _svc = _home(_ForecastService(reply={"weather.home": "garbled"}))
    out = await _forecast(agent, hass)
    assert out["error"].startswith("could not read forecast:")
