"""hazard_monitor.py's pure logic against canned feed replies: distance and
box maths, where home is, per feed memory, and how each feed is parsed and
filtered (magnitude, severity, radius). No network: _get_json is replaced in
every feed test, and the feed replies are built by the helpers below.

The wording, delivery, the query parameters and the weather warnings
lifecycle are covered in test_hazard_monitor_spec.py,
test_hazard_monitor_warnings.py and test_hazard_quiet_hours.py.
"""
from __future__ import annotations

import math
import types

import pytest

# A made up home point well away from Ireland, so the region defaults are
# the "anywhere else" ones.
HOME = (45.0, -100.0)
KM_PER_DEG_LAT = 2 * math.pi * 6371.0 / 360


def north_of_home(km: float) -> tuple[float, float]:
    """A (lat, lon) point `km` due north of HOME."""
    return HOME[0] + km / KM_PER_DEG_LAT, HOME[1]


def make_hass(lat=HOME[0], lon=HOME[1]):
    """Just enough of Home Assistant: a home point and no zone.home."""
    return types.SimpleNamespace(
        config=types.SimpleNamespace(latitude=lat, longitude=lon),
        states=types.SimpleNamespace(get=lambda entity_id: None))


@pytest.fixture
def settings(load, monkeypatch):
    """The user's saved Nova settings, as a plain dict the test fills in."""
    saved: dict = {}
    monkeypatch.setattr(load("nova_config"), "get", lambda key, default=None:
                        saved.get(key, default))
    return saved


@pytest.fixture
def hz(load, monkeypatch, settings):
    mod = load("hazard_monitor")
    monkeypatch.setattr(mod, "_STORE", None)
    monkeypatch.setattr(mod, "_LAST_WARNINGS", {})
    monkeypatch.setattr(mod, "_SEEN", {"quake": set(), "wx": set(), "disaster": set()})
    return mod


@pytest.fixture
def feed(hz, monkeypatch):
    """Answer every fetch with `feed.reply` (a dict, or a callable of the
    URL), and keep the URLs asked for in `feed.urls`."""
    box = types.SimpleNamespace(reply=None, urls=[])

    async def fake_get(hass, url, params=None):
        box.urls.append(url)
        return box.reply(url) if callable(box.reply) else box.reply
    monkeypatch.setattr(hz, "_get_json", fake_get)
    return box


def quake(quake_id, mag, point, **props):
    lat, lon = point
    return {"id": quake_id, "properties": {"mag": mag, **props},
            "geometry": {"coordinates": [lon, lat, 10.0]}}


def wx_alert(alert_id, severity, **props):
    return {"id": alert_id, "properties": {"severity": severity, **props}}


def natural_event(event_id, *points, **extra):
    return {"id": event_id, "geometry": [{"coordinates": [lon, lat]} for lat, lon in points],
            **extra}


async def scan(hz, check):
    return await check(make_hass(), *HOME)


# ── distance and the search box ─────────────────────────────────────────────

@pytest.mark.parametrize("a, b, km, tol", [
    ((40.0, -75.0), (40.0, -75.0), 0.0, 0.01),
    ((0.0, 0.0), (1.0, 0.0), KM_PER_DEG_LAT, 0.01),             # one degree of latitude
    ((0.0, 0.0), (0.0, 90.0), math.pi / 2 * 6371.0, 0.5),        # a quarter of the equator
    ((40.71, -74.01), (39.95, -75.16), 129.6, 1.5),              # two cities a short drive apart
])
def test_great_circle_distance(hz, a, b, km, tol):
    assert hz._haversine_km(*a, *b) == pytest.approx(km, abs=tol)
    assert hz._haversine_km(*b, *a) == pytest.approx(km, abs=tol)   # either way round


def test_the_search_box_surrounds_the_point(hz):
    south, north, west, east = hz._bbox(40.0, -75.0, 100.0)
    assert south < 40.0 < north
    assert west < -75.0 < east
    # 100 km is about 0.9 degrees of latitude either side.
    assert north - 40.0 == pytest.approx(100.0 / 111.0)
    assert 40.0 - south == pytest.approx(100.0 / 111.0)
    # Further from the equator a degree of longitude is shorter, so the box is wider.
    assert east - (-75.0) > north - 40.0


# ── where home is ───────────────────────────────────────────────────────────

def test_home_is_home_assistants_point_by_default(hz):
    assert hz._home_latlon(make_hass()) == pytest.approx(HOME)


def test_a_saved_point_overrides_home(hz, settings):
    settings.update(hazard_lat=-33.9, hazard_lon=18.4)
    assert hz._home_latlon(make_hass()) == pytest.approx((-33.9, 18.4))


# ── each feed tells an event once ───────────────────────────────────────────

def test_an_id_is_new_once_and_feeds_are_separate(hz):
    assert hz._remember("quake", "ev1") is True
    assert hz._remember("quake", "ev1") is False
    assert hz._remember("disaster", "ev1") is True     # the same id in another feed is new


# ── earthquakes ─────────────────────────────────────────────────────────────

async def test_only_quakes_inside_the_radius_are_reported(hz, feed):
    feed.reply = {"features": [
        quake("near1", 3.2, north_of_home(4), place="4 km N of home", url="https://test/near1"),
        quake("far1", 6.0, (35.0, -120.0), place="far away"),
    ]}
    found = await scan(hz, hz._check_earthquakes)
    assert found == [{"id": "near1", "mag": 3.2, "place": "4 km N of home",
                      "dist_km": 4, "url": "https://test/near1"}]


async def test_a_smaller_radius_setting_drops_a_quake(hz, feed, settings):
    settings["hazard_quake_radius_km"] = 3
    feed.reply = {"features": [quake("near1", 3.2, north_of_home(4))]}
    assert await scan(hz, hz._check_earthquakes) == []


async def test_a_quake_is_reported_once(hz, feed):
    feed.reply = {"features": [quake("q9", 4.0, north_of_home(1), place="here")]}
    assert len(await scan(hz, hz._check_earthquakes)) == 1
    assert await scan(hz, hz._check_earthquakes) == []


@pytest.mark.parametrize("reply", [None, {}, {"type": "FeatureCollection"}])
async def test_no_quake_data_is_no_quakes(hz, feed, reply):
    feed.reply = reply
    assert await scan(hz, hz._check_earthquakes) == []


async def test_a_quake_without_a_place_still_reads(hz, feed):
    feed.reply = {"features": [quake("q1", None, north_of_home(2))]}
    (found,) = await scan(hz, hz._check_earthquakes)
    assert found["place"] == "unknown location"
    assert found["mag"] is None


# ── weather alerts ──────────────────────────────────────────────────────────

async def test_only_extreme_and_severe_weather_by_default(hz, feed):
    feed.reply = {"features": [
        wx_alert("wx-extreme", "Extreme", event="Tornado Warning", areaDesc="Test County",
                 instruction="Take cover"),
        wx_alert("wx-severe", "Severe", event="Storm Warning"),
        wx_alert("wx-moderate", "Moderate", event="Wind Advisory"),
        wx_alert("wx-minor", "Minor", event="Frost Advisory", areaDesc="Test County"),
    ]}
    found = await scan(hz, hz._check_weather)
    assert [w["id"] for w in found] == ["wx-extreme", "wx-severe"]
    first = found[0]
    assert (first["event"], first["severity"], first["area"], first["instruction"]) == \
        ("Tornado Warning", "Extreme", "Test County", "Take cover")


async def test_the_wanted_severities_are_a_setting(hz, feed, settings):
    settings["hazard_wx_severities"] = ["Minor"]
    feed.reply = {"features": [wx_alert("m1", "Minor", event="Advisory"),
                               wx_alert("e1", "Extreme", event="Warning")]}
    assert [w["id"] for w in await scan(hz, hz._check_weather)] == ["m1"]


async def test_a_severities_setting_that_is_not_a_list_means_the_default(hz, feed, settings):
    settings["hazard_wx_severities"] = "Minor"
    feed.reply = {"features": [wx_alert("m1", "Minor"), wx_alert("e1", "Extreme")]}
    assert [w["id"] for w in await scan(hz, hz._check_weather)] == ["e1"]


# ── natural events ──────────────────────────────────────────────────────────

async def test_natural_events_use_their_nearest_point_and_the_radius(hz, feed):
    feed.reply = {"events": [
        # A wildfire track that starts far away but has come within 10 km.
        natural_event("EONET_near", (33.0, 130.0), north_of_home(10), title="Wildfire XYZ",
                      categories=[{"title": "Wildfires"}],
                      sources=[{"url": "https://test/near"}]),
        natural_event("EONET_far", (33.0, 130.0), title="Volcano ABC",
                      categories=[{"title": "Volcanoes"}]),
        natural_event("EONET_nowhere", title="No usable point"),
    ]}
    found = await scan(hz, hz._check_disasters)
    assert found == [{"id": "EONET_near", "title": "Wildfire XYZ", "category": "Wildfires",
                      "dist_km": 10, "url": "https://test/near"}]


# ── a scan on request ───────────────────────────────────────────────────────

async def test_a_scan_on_request_never_uses_up_an_alert(hz, feed):
    feed.reply = lambda url: ({"features": [quake("sq1", 3.0, north_of_home(1))]}
                              if "earthquake" in url else {"features": [], "events": []})
    before = {name: set(ids) for name, ids in hz._SEEN.items()}
    result = await hz.scan_now(make_hass())
    assert result["ok"] is True
    assert result["counts"]["earthquakes"] == 1
    assert result["earthquakes"][0]["id"] == "sq1"
    assert hz._SEEN == before                        # nothing marked as told
    # So the timed check still passes it on afterwards.
    assert [q["id"] for q in await scan(hz, hz._check_earthquakes)] == ["sq1"]


# ── switched off, and the panel's view ──────────────────────────────────────

async def test_nothing_is_fetched_while_the_monitor_is_off(hz, feed):
    assert await hz.periodic_check(make_hass()) == {"skipped": "disabled"}
    assert feed.urls == []


async def test_status_describes_the_watch(hz, settings):
    settings["hazard_monitor_enabled"] = True
    st = await hz.status(make_hass())
    assert st["enabled"] is True
    assert st["center"] == list(HOME)
    assert st["using_override"] is False
    assert st["feeds"] == {"earthquakes": True, "weather": False, "disasters": True}
    # No country set and the home is outside Ireland: the custom source.
    assert st["source"] == "custom"
    assert st["in_ireland"] is False
