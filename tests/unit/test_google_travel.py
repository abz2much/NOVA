"""Google Maps Travel Time lookups for leave alerts (8.9.0): the request Nova
sends, the answer it reads, and every way a lookup can fail without raising."""
import asyncio
import datetime
import types

import pytest


@pytest.fixture
def gt(load):
    mod = load("google_travel")
    mod._USAGE.update(day=None, count=0)
    return mod


def _entry(state="loaded", entry_id="entry1"):
    return types.SimpleNamespace(entry_id=entry_id,
                                 state=types.SimpleNamespace(value=state))


class _Hass:
    def __init__(self, entries=(), response=None, exc=None, services=("get_travel_times", "get_transit_times")):
        self.calls = []
        self._response, self._exc = response, exc
        self.config_entries = types.SimpleNamespace(async_entries=lambda domain: list(entries)
                                                    if domain == "google_travel_time" else [])
        self.services = types.SimpleNamespace(
            async_call=self._call,
            has_service=lambda domain, service: domain == "google_travel_time" and service in services)

    async def _call(self, domain, service, data, blocking=False, return_response=False):
        self.calls.append((domain, service, dict(data), blocking, return_response))
        if self._exc:
            raise self._exc
        return self._response


def test_parse_minutes_takes_the_shortest_route(gt):
    assert gt.parse_minutes({"routes": [{"duration": 1200}, {"duration": 600}]}) == 10.0
    assert gt.parse_minutes({"routes": [{"duration": 750}]}) == 12.5


@pytest.mark.parametrize("response", [None, {}, {"routes": []}, {"routes": [{}]},
                                      {"routes": [{"duration": "soon"}]}, "no", []])
def test_parse_minutes_gives_none_for_anything_else(gt, response):
    assert gt.parse_minutes(response) is None


def test_available_needs_a_loaded_entry_and_both_actions(gt):
    assert gt.available(_Hass([_entry()])) is True
    assert gt.available(_Hass([])) is False
    assert gt.available(_Hass([_entry("setup_error")])) is False
    assert gt.available(_Hass([_entry()], services=("get_travel_times",))) is False
    assert gt.available(None) is False


async def test_walking_asks_for_a_walking_route_from_coordinates(gt):
    hass = _Hass([_entry()], {"routes": [{"duration": 740}]})
    minutes, why = await gt.route_minutes(hass, "walk", (53.6, -6.2), "The School, Balbriggan")
    assert (minutes, why) == (12.3, None)
    domain, service, data, blocking, rr = hass.calls[0]
    assert (domain, service, blocking, rr) == ("google_travel_time", "get_travel_times", True, True)
    assert data == {"config_entry_id": "entry1", "origin": "53.600000,-6.200000",
                    "destination": "The School, Balbriggan", "mode": "walking"}


async def test_driving_asks_for_a_driving_route(gt):
    hass = _Hass([_entry()], {"routes": [{"duration": 150}]})
    assert await gt.route_minutes(hass, "drive", (1.0, 2.0), "X") == (2.5, None)
    assert hass.calls[0][2]["mode"] == "driving" and "arrival_time" not in hass.calls[0][2]


async def test_transit_uses_the_transit_action_and_an_arrival_time(gt):
    hass = _Hass([_entry()], {"routes": [{"duration": 3844}]})
    arrive = datetime.datetime(2026, 10, 9, 9, 0)
    minutes, why = await gt.route_minutes(hass, "transit", (1.0, 2.0), "Connolly", arrive_by=arrive)
    assert (minutes, why) == (64.1, None)
    _d, service, data, *_ = hass.calls[0]
    assert service == "get_transit_times" and data["arrival_time"] == "09:00:00"
    assert "mode" not in data


async def test_transit_without_an_arrival_time_sends_none(gt):
    hass = _Hass([_entry()], {"routes": [{"duration": 600}]})
    await gt.route_minutes(hass, "transit", (1.0, 2.0), "X")
    assert "arrival_time" not in hass.calls[0][2]


async def test_no_entry_means_no_lookup(gt):
    hass = _Hass([])
    minutes, why = await gt.route_minutes(hass, "walk", (1.0, 2.0), "X")
    assert minutes is None and "not set up" in why and hass.calls == []


async def test_an_unknown_mode_is_refused(gt):
    minutes, why = await gt.route_minutes(_Hass([_entry()]), "teleport", (1.0, 2.0), "X")
    assert minutes is None and why == "unknown travel mode"


async def test_no_routes_is_reported(gt):
    hass = _Hass([_entry()], {"routes": []})
    assert await gt.route_minutes(hass, "walk", (1.0, 2.0), "X") == (None, "Google found no walk route")


async def test_a_permission_error_names_the_routes_api(gt):
    hass = _Hass([_entry()], exc=RuntimeError("permission_denied: Routes API is not enabled"))
    minutes, why = await gt.route_minutes(hass, "drive", (1.0, 2.0), "X")
    assert minutes is None and why == "the Routes API is not enabled for your Google key"


async def test_any_other_error_never_raises(gt):
    hass = _Hass([_entry()], exc=ValueError("boom"))
    assert await gt.route_minutes(hass, "drive", (1.0, 2.0), "X") == (
        None, "Google Maps Travel Time returned an error")


async def test_a_slow_answer_times_out(gt, monkeypatch):
    async def never(*a, **k):
        await asyncio.sleep(10)
    hass = _Hass([_entry()])
    hass.services.async_call = never
    monkeypatch.setattr(gt, "_TIMEOUT", 0.01)
    minutes, why = await gt.route_minutes(hass, "walk", (1.0, 2.0), "X")
    assert minutes is None and why == "Google did not answer in time"


async def test_the_daily_limit_stops_lookups_and_resets_next_day(gt):
    hass = _Hass([_entry()], {"routes": [{"duration": 600}]})
    day1 = datetime.datetime(2026, 10, 9, 12, 0).timestamp()
    for _ in range(gt.DAILY_CAP):
        assert (await gt.route_minutes(hass, "walk", (1.0, 2.0), "X", now=day1))[0] == 10.0
    assert gt.calls_today(day1) == gt.DAILY_CAP
    minutes, why = await gt.route_minutes(hass, "walk", (1.0, 2.0), "X", now=day1)
    assert minutes is None and "daily limit" in why and len(hass.calls) == gt.DAILY_CAP
    day2 = day1 + 86400
    assert (await gt.route_minutes(hass, "walk", (1.0, 2.0), "X", now=day2))[0] == 10.0
    assert gt.calls_today(day2) == 1
