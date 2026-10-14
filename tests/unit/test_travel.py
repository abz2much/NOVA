"""Fresh behaviour tests for travel.py: keyless geocode (Nominatim) and
routing (OSRM) for leave-alert drive times, with plain-language failure
reasons and an in-process geocode cache.

The network seam `_get_json` is replaced in most tests; `_get_json` itself
is exercised with a fake aiohttp module and a fake client session."""
from __future__ import annotations

import sys
import types

import pytest

ORIGIN = (51.5, -0.12)


@pytest.fixture
def tr(load):
    m = load("travel")
    m._GEO_CACHE.clear()
    m._LAST_FAILURE.clear()
    yield m
    m._GEO_CACHE.clear()
    m._LAST_FAILURE.clear()


class Net:
    """Scripted responses for the geocoder and router, recording calls."""

    def __init__(self, tr, geo=None, route=None):
        self.tr, self.geo, self.route = tr, geo, route
        self.calls: list = []

    async def __call__(self, hass, url, params=None):
        self.calls.append((url, params))
        if url == self.tr.NOMINATIM_URL:
            return self.geo
        return self.route

    @property
    def geo_calls(self):
        return [c for c in self.calls if c[0] == self.tr.NOMINATIM_URL]

    @property
    def route_calls(self):
        return [c for c in self.calls if c[0] != self.tr.NOMINATIM_URL]


def _net(tr, monkeypatch, **kw):
    n = Net(tr, **kw)
    monkeypatch.setattr(tr, "_get_json", n)
    return n


OK = {"code": "Ok", "routes": [{"duration": 900}]}
HIT = [{"lat": "52.0", "lon": "-1.0"}]


def test_constants(tr):
    assert tr.NOMINATIM_URL == "https://nominatim.openstreetmap.org/search"
    assert tr.DEFAULT_OSRM == "https://router.project-osrm.org"
    assert tr._TIMEOUT == 8
    assert tr._UA == "nova-home-assistant (Home Assistant integration)"


# ── pure helpers ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, want", [
    ("51.5,-0.12", (51.5, -0.12)),
    (" -33.86 ,  151.2 ", (-33.86, 151.2)),
    ("40,-74", (40.0, -74.0)),
    ("999,999", (999.0, 999.0)),
])
def test_coordinate_literals(tr, text, want):
    assert tr._coords_from_str(text) == want


@pytest.mark.parametrize("text", [
    "", None, "High Street", "51.5", "51.5;-0.1", "+51.5,-0.1", ".5,1",
    "1e3,2", "51.5,-0.1,3", "51.5, -0.1 x"])
def test_non_coordinates(tr, text):
    assert tr._coords_from_str(text) is None


def test_parse_geocode(tr):
    assert tr._parse_geocode([{"lat": "1.5", "lon": "2.5"}, {"lat": "9", "lon": "9"}]) == (1.5, 2.5)
    assert tr._parse_geocode([{"lat": 3, "lon": 4}]) == (3.0, 4.0)
    for bad in ([], None, {}, "x", [{"lat": "1"}], [{"lat": "a", "lon": "b"}], [None], [5]):
        assert tr._parse_geocode(bad) is None


def test_route_url(tr):
    assert tr._route_url((10.5, 20.25), (11.0, -21.0), "http://osrm.lan:5000/") == (
        "http://osrm.lan:5000/route/v1/driving/20.250000,10.500000;-21.000000,11.000000"
        "?overview=false")
    assert tr._route_url([1, 2], [3, 4]) == (
        "https://router.project-osrm.org/route/v1/driving/"
        "2.000000,1.000000;4.000000,3.000000?overview=false")
    assert tr._route_url((1, 2), (3, 4), "").startswith(tr.DEFAULT_OSRM + "/route")


def test_parse_route(tr):
    assert tr._parse_route({"code": "Ok", "routes": [{"duration": 90}]}) == 1.5
    assert tr._parse_route({"code": "Ok", "routes": [{"duration": "125"}]}) == 2.1
    assert tr._parse_route({"code": "Ok", "routes": [{"duration": 600}, {"duration": 1}]}) == 10.0
    for bad in (None, {}, [], "", {"code": "NoRoute", "routes": [{"duration": 5}]},
                {"code": "Ok"}, {"code": "Ok", "routes": []},
                {"code": "Ok", "routes": [{}]}, {"code": "Ok", "routes": [{"duration": "x"}]}):
        assert tr._parse_route(bad) is None


def test_parse_route_raises_on_truthy_non_dict(tr):
    with pytest.raises(AttributeError):
        tr._parse_route([{"code": "Ok"}])


def test_key_normalisation(tr):
    assert tr._key("  Town Hall ") == "town hall"
    assert tr._key(None) == "" and tr._key("") == ""


# ── travel_minutes ──────────────────────────────────────────────────────────

async def test_geocode_then_route(tr, fake_hass, monkeypatch):
    n = _net(tr, monkeypatch, geo=HIT, route=OK)
    assert await tr.travel_minutes(fake_hass, ORIGIN, " Town Hall ") == 15.0
    assert n.geo_calls == [(tr.NOMINATIM_URL, {"q": " Town Hall ", "format": "json", "limit": "1"})]
    assert n.route_calls == [(tr._route_url(ORIGIN, (52.0, -1.0)), None)]
    assert tr._GEO_CACHE == {"town hall": (52.0, -1.0)}
    assert tr.failure_reason("Town Hall") is None


async def test_custom_router_base(tr, fake_hass, monkeypatch):
    n = _net(tr, monkeypatch, geo=HIT, route=OK)
    await tr.travel_minutes(fake_hass, ORIGIN, "Library", "http://router.local/")
    assert n.route_calls[0][0].startswith("http://router.local/route/v1/driving/")


async def test_geocode_cache_is_shared_across_spellings(tr, fake_hass, monkeypatch):
    n = _net(tr, monkeypatch, geo=HIT, route=OK)
    await tr.travel_minutes(fake_hass, ORIGIN, "Market Square")
    await tr.travel_minutes(fake_hass, ORIGIN, "  market square")
    assert len(n.geo_calls) == 1 and len(n.route_calls) == 2


async def test_routes_are_not_cached(tr, fake_hass, monkeypatch):
    n = _net(tr, monkeypatch, route=OK)
    await tr.travel_minutes(fake_hass, ORIGIN, "52,-1")
    await tr.travel_minutes(fake_hass, ORIGIN, "52,-1")
    assert len(n.route_calls) == 2


async def test_coordinate_destination_is_not_geocoded_or_cached(tr, fake_hass, monkeypatch):
    n = _net(tr, monkeypatch, route=OK)
    assert await tr.travel_minutes(fake_hass, ORIGIN, "52.0, -1.0") == 15.0
    assert n.geo_calls == [] and tr._GEO_CACHE == {}
    assert n.route_calls[0][0] == tr._route_url(ORIGIN, (52.0, -1.0))


async def test_geocode_misses_are_retried(tr, fake_hass, monkeypatch):
    n = _net(tr, monkeypatch, geo=[], route=OK)
    await tr.travel_minutes(fake_hass, ORIGIN, "Nowhere Lane")
    await tr.travel_minutes(fake_hass, ORIGIN, "Nowhere Lane")
    assert len(n.geo_calls) == 2 and n.route_calls == []


@pytest.mark.parametrize("origin, dest", [(None, "Library"), ((), "Library"),
                                          (ORIGIN, ""), (ORIGIN, None), (None, None)])
async def test_missing_origin_or_destination(tr, fake_hass, monkeypatch, origin, dest):
    n = _net(tr, monkeypatch, geo=HIT, route=OK)
    assert await tr.travel_minutes(fake_hass, origin, dest) is None
    assert n.calls == []
    assert tr.failure_reason(dest) == "no starting position or no destination"


async def test_map_search_down(tr, fake_hass, monkeypatch):
    _net(tr, monkeypatch, geo=None, route=OK)
    assert await tr.travel_minutes(fake_hass, ORIGIN, "Library") is None
    assert tr.failure_reason("LIBRARY ") == "the map search service did not answer"


async def test_place_not_found(tr, fake_hass, monkeypatch):
    _net(tr, monkeypatch, geo=[{"name": "no coords"}], route=OK)
    assert await tr.travel_minutes(fake_hass, ORIGIN, "Library") is None
    assert tr.failure_reason("library") == "the place could not be found on the map"


async def test_router_down_then_no_route_then_success(tr, fake_hass, monkeypatch):
    n = _net(tr, monkeypatch, geo=HIT, route=None)
    assert await tr.travel_minutes(fake_hass, ORIGIN, "Clinic") is None
    assert tr.failure_reason("Clinic") == "the routing service did not answer"
    n.route = {"code": "NoRoute"}
    assert await tr.travel_minutes(fake_hass, ORIGIN, "Clinic") is None
    assert tr.failure_reason("Clinic") == "the routing service found no route"
    n.route = OK
    assert await tr.travel_minutes(fake_hass, ORIGIN, "Clinic") == 15.0
    assert tr.failure_reason("Clinic") is None
    assert "clinic" not in tr._LAST_FAILURE


async def test_geocode_success_does_not_clear_old_reason(tr, fake_hass, monkeypatch):
    n = _net(tr, monkeypatch, geo=None, route=None)
    await tr.travel_minutes(fake_hass, ORIGIN, "Park")
    n.geo = HIT
    await tr._geocode(fake_hass, "Park")
    assert tr.failure_reason("Park") == "the map search service did not answer"


async def test_whitespace_destination(tr, fake_hass, monkeypatch):
    n = _net(tr, monkeypatch, geo=HIT, route=OK)
    assert await tr.travel_minutes(fake_hass, ORIGIN, "   ") is None
    assert n.calls == []
    assert tr.failure_reason("") == "the place could not be found on the map"


async def test_whitespace_destination_keeps_existing_empty_key_reason(tr, fake_hass, monkeypatch):
    _net(tr, monkeypatch, geo=HIT, route=OK)
    await tr.travel_minutes(fake_hass, None, "")
    await tr.travel_minutes(fake_hass, ORIGIN, "  ")
    assert tr.failure_reason("") == "no starting position or no destination"


async def test_list_body_from_router_raises(tr, fake_hass, monkeypatch):
    _net(tr, monkeypatch, route=[{"code": "Ok"}])
    with pytest.raises(AttributeError):
        await tr.travel_minutes(fake_hass, ORIGIN, "1,2")


def test_failure_reason_unknown_place(tr):
    assert tr.failure_reason("never asked") is None
    assert tr.failure_reason(None) is None


# ── _geocode ────────────────────────────────────────────────────────────────

async def test_geocode_empty_key(tr, fake_hass, monkeypatch):
    n = _net(tr, monkeypatch, geo=HIT)
    assert await tr._geocode(fake_hass, "  ") is None
    assert n.calls == [] and tr._LAST_FAILURE == {}


async def test_geocode_cached_value_short_circuits(tr, fake_hass, monkeypatch):
    n = _net(tr, monkeypatch, geo=HIT)
    tr._GEO_CACHE["depot"] = (1.0, 2.0)
    assert await tr._geocode(fake_hass, "Depot") == (1.0, 2.0)
    assert n.calls == []


# ── _get_json ───────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body=None, exc=None):
        self.status, self.body, self.exc = status, body, exc
        self.json_kwargs = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, **kw):
        self.json_kwargs = kw
        if self.exc:
            raise self.exc
        return self.body


class _Session:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc = resp, exc
        self.calls: list = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.resp


@pytest.fixture
def http(monkeypatch):
    aiohttp = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total
    aiohttp.ClientTimeout = ClientTimeout
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    helper = sys.modules["homeassistant.helpers.aiohttp_client"]
    holder = types.SimpleNamespace(session=None, hass=[])

    def get_session(hass):
        holder.hass.append(hass)
        return holder.session
    monkeypatch.setattr(helper, "async_get_clientsession", get_session)
    return holder


async def test_get_json_success(tr, fake_hass, http):
    resp = _Resp(200, {"ok": 1})
    http.session = _Session(resp)
    out = await tr._get_json(fake_hass, "https://example.invalid/x", {"a": "b"})
    assert out == {"ok": 1}
    assert http.hass == [fake_hass]
    url, kw = http.session.calls[0]
    assert url == "https://example.invalid/x"
    assert kw["params"] == {"a": "b"}
    assert kw["headers"] == {"User-Agent": tr._UA}
    assert kw["timeout"].total == 8
    assert resp.json_kwargs == {"content_type": None}


async def test_get_json_default_params_none(tr, fake_hass, http):
    http.session = _Session(_Resp(200, []))
    assert await tr._get_json(fake_hass, "u") == []
    assert http.session.calls[0][1]["params"] is None


async def test_get_json_non_200(tr, fake_hass, http):
    http.session = _Session(_Resp(503, {"ignored": True}))
    assert await tr._get_json(fake_hass, "u") is None


async def test_get_json_errors_become_none(tr, fake_hass, http):
    http.session = _Session(exc=OSError("unreachable"))
    assert await tr._get_json(fake_hass, "u") is None
    http.session = _Session(_Resp(200, exc=ValueError("bad json")))
    assert await tr._get_json(fake_hass, "u") is None


async def test_get_json_without_aiohttp_is_none(tr, fake_hass, monkeypatch):
    monkeypatch.setitem(sys.modules, "aiohttp", None)     # import fails
    assert await tr._get_json(fake_hass, "u") is None
