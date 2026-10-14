"""The hazard monitor checked against its spec (7d): the wording of each alert,
the delivery request, the feed queries and the panel and summary shapes. The
older hazard tests cover region defaults, levels and the warnings lifecycle."""
from __future__ import annotations

import sys
import time
import types

import pytest

from fakes import FakeHass


def _hass(country="US", lat=40.7, lon=-74.0):
    hass = FakeHass()
    hass.config = types.SimpleNamespace(country=country, latitude=lat, longitude=lon,
                                        time_zone="America/New_York", language="en")
    return hass


@pytest.fixture
def cfg(load, monkeypatch):
    nc = load("nova_config")
    data: dict = {}
    monkeypatch.setattr(nc, "get", lambda k, d=None: data.get(k, d))
    return data


@pytest.fixture
def hm(load, monkeypatch, cfg):
    mod = load("hazard_monitor")
    monkeypatch.setattr(mod, "_STORE", None)
    monkeypatch.setattr(mod, "_LAST_WARNINGS", {})
    for seen in mod._SEEN.values():
        seen.clear()
    return mod


@pytest.fixture
def feeds(hm, monkeypatch):
    """_get_json answered from a table keyed by URL, with every call kept."""
    st = types.SimpleNamespace(answers={}, calls=[])

    async def get_json(hass, url, params=None):
        st.calls.append((url, dict(params or {})))
        return st.answers.get(url)
    monkeypatch.setattr(hm, "_get_json", get_json)
    return st


# ── wording ─────────────────────────────────────────────────────────────────

def test_quake_wording(hm):
    q = {"mag": 4.26, "dist_km": 120, "place": "10 km N of Town"}
    assert hm._fmt_quake(q, "sir") == (
        "Seismic alert, sir. A magnitude 4.3 earthquake was just recorded 120 km "
        "away — 10 km N of Town.")
    assert hm._fmt_quake(dict(q, mag=None), "") == (
        "Seismic alert. A an earthquake earthquake was just recorded 120 km away "
        "— 10 km N of Town.")


def test_weather_wording(hm):
    w = {"severity": "Severe", "event": "Flood Warning", "area": "", "instruction": ""}
    assert hm._fmt_weather(w, "") == "Severe weather alert: Flood Warning."
    full = dict(w, area="County A", instruction="x" * 250)
    assert hm._fmt_weather(full, "madam") == (
        "Severe weather alert, madam: Flood Warning for County A. " + "x" * 200)


def test_disaster_wording(hm):
    d = {"title": "Fire", "category": "Wildfires", "dist_km": 42}
    assert hm._fmt_disaster(d, "sir") == "Natural hazard nearby, sir: Fire (Wildfires), about 42 km away."
    assert hm._fmt_disaster(d, "") == "Natural hazard nearby: Fire (Wildfires), about 42 km away."


# ── settings ────────────────────────────────────────────────────────────────

def test_cfg_turns_a_stored_none_into_the_default(hm, cfg):
    cfg["k"] = None
    assert hm._cfg("k", 5) == 5
    cfg["k"] = 0
    assert hm._cfg("k", 5) == 0


def test_saved_is_none_when_config_fails(hm, load, monkeypatch):
    def boom(k, d=None):
        raise RuntimeError
    monkeypatch.setattr(load("nova_config"), "get", boom)
    assert hm._saved("x") is None and hm._cfg("x", 3) == 3 and hm._enabled() is False


@pytest.mark.parametrize("raw, expected", [
    (None, "yellow"), ("RED ", "red"), ("orange", "orange"), ("off", "yellow"), ("", "yellow"),
])
def test_level_settings(hm, cfg, raw, expected):
    cfg["hazard_push_level"] = raw
    assert hm._level_setting("hazard_push_level", "yellow") == expected


@pytest.mark.parametrize("raw, expected", [
    (None, "red"), ("off", "off"), (" Yellow", "yellow"), ("blue", "red"),
])
def test_night_level_setting(hm, cfg, raw, expected):
    cfg["hazard_night_speak_level"] = raw
    assert hm._night_level_setting() == expected


@pytest.mark.parametrize("raw, expected", [
    (None, []), ("", []), ("  ", []), ("not json", []), ('["a", 2]', ["a", "2"]),
    (["x", 3], ["x", "3"]), ('{"a": 1}', []), (7, []),
])
def test_json_lists(hm, cfg, raw, expected):
    cfg["k"] = raw
    assert hm._json_list("k") == expected


def test_runtime_reads_the_live_config(hm, load, monkeypatch):
    monkeypatch.setattr(load("runtime"), "domain_runtime_config", lambda hass: {"a": None})
    assert hm._runtime(None, "a", 1) is None and hm._runtime(None, "b", 2) == 2


def test_timezone_and_language_fail_soft(hm):
    assert hm._tz(object()) is None and hm._lang(object()) == ""
    hass = _hass()
    hass.config.language = None
    assert hm._tz(hass) == "America/New_York" and hm._lang(hass) == ""


def test_zone_home_wins_over_the_config_point(hm):
    hass = _hass()
    hass.states.async_set("zone.home", "zoning", {"latitude": 1.5, "longitude": 2.5})
    assert hm.base_home_latlon(hass) == (1.5, 2.5)
    hass.states.async_set("zone.home", "zoning", {"latitude": 1.5})
    assert hm.base_home_latlon(hass) == (40.7, -74.0)
    assert hm.base_home_latlon(object()) is None


@pytest.mark.parametrize("lat, lon", [("", 2), (None, 2), ("abc", "2")])
def test_a_partial_or_bad_override_falls_back_to_home(hm, cfg, lat, lon):
    cfg.update(hazard_lat=lat, hazard_lon=lon)
    assert hm._home_latlon(_hass()) == (40.7, -74.0)


def test_a_string_override_is_used(hm, cfg):
    cfg.update(hazard_lat="10.5", hazard_lon="-3")
    assert hm._home_latlon(_hass()) == (10.5, -3.0)


def test_cap_settings(hm, cfg):
    cfg.update(hazard_cap_url="  https://feed.example/cap  ",
               hazard_cap_area_codes='["A1"]', hazard_cap_area_names=["North"])
    assert hm._cap_settings() == {"url": "https://feed.example/cap", "codes": ["A1"],
                                  "names": ["North"]}


def test_the_bbox_near_a_pole_stays_finite(hm):
    lo_lat, hi_lat, lo_lon, hi_lon = hm._bbox(90.0, 0.0, 111.0)
    assert (lo_lat, hi_lat) == pytest.approx((89.0, 91.0))
    assert (lo_lon, hi_lon) == pytest.approx((-100.0, 100.0))


# ── dedup ───────────────────────────────────────────────────────────────────

def test_remember_caps_a_feed_at_half(hm, monkeypatch):
    monkeypatch.setattr(hm, "_SEEN_CAP", 10)
    for i in range(10):
        assert hm._remember("quake", f"e{i}") is True
    assert len(hm._SEEN["quake"]) == 10
    assert hm._remember("quake", "e10") is True
    assert len(hm._SEEN["quake"]) == 5


def test_remember_makes_a_new_feed(hm):
    assert hm._remember("other", "x") is True and hm._remember("other", "x") is False
    del hm._SEEN["other"]


# ── fetching ────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type="x"):
        assert content_type is None
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def session(monkeypatch):
    st = types.SimpleNamespace(reply=_Resp(200, {"ok": 1}), calls=[])

    class _Session:
        def get(self, url, **kw):
            st.calls.append((url, kw))
            if isinstance(st.reply, Exception):
                raise st.reply
            return st.reply
    aio = types.ModuleType("aiohttp")
    aio.ClientTimeout = lambda total: ("timeout", total)
    monkeypatch.setitem(sys.modules, "aiohttp", aio)
    client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    client.async_get_clientsession = lambda hass: _Session()
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.aiohttp_client", client)
    return st


async def test_get_json_sends_the_agent_and_parses(hm, session):
    assert await hm._get_json(None, "https://x", {"a": 1}) == {"ok": 1}
    url, kw = session.calls[0]
    assert url == "https://x" and kw["params"] == {"a": 1}
    assert kw["headers"] == {"User-Agent": hm._USER_AGENT, "Accept": "application/geo+json"}
    assert kw["timeout"] == ("timeout", 20)


async def test_get_json_is_none_on_a_bad_status_or_an_error(hm, session):
    session.reply = _Resp(503, {"ok": 1})
    assert await hm._get_json(None, "https://x") is None
    session.reply = RuntimeError("down")
    assert await hm._get_json(None, "https://x") is None


async def test_the_quake_query(hm, feeds, cfg, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 7200.0)
    cfg.update(hazard_quake_radius_km=111, hazard_quake_min_mag=3)
    assert await hm._check_earthquakes(None, 0.0, 0.0) == []
    url, params = feeds.calls[0]
    assert url == hm._USGS_FDSN
    assert params == {"format": "geojson", "starttime": "1970-01-01T01:00:00",
                      "minlatitude": "-1.0000", "maxlatitude": "1.0000",
                      "minlongitude": "-1.0000", "maxlongitude": "1.0000",
                      "minmagnitude": "3.0", "orderby": "time"}


async def test_quakes_keep_their_fields_and_skip_bad_rows(hm, feeds):
    feeds.answers[hm._USGS_FDSN] = {"features": [
        {"id": "q1", "properties": {"mag": 3.1, "url": "u"}, "geometry": {"coordinates": [0.1, 0.1]}},
        {"id": "q2", "properties": None, "geometry": {"coordinates": [0.2]}},
        {"id": None, "geometry": {"coordinates": [0, 0]}},
        {"id": "q3", "properties": {}, "geometry": {"coordinates": ["a", 0]}},
        "not a dict",
    ]}
    (q,) = await hm._check_earthquakes(None, 0.0, 0.0)
    assert q == {"id": "q1", "mag": 3.1, "place": "unknown location", "dist_km": 16, "url": "u"}


async def test_the_weather_query_and_fields(hm, feeds, cfg):
    cfg["hazard_wx_severities"] = "Severe"            # not a list: the defaults
    feeds.answers[hm._NWS_ACTIVE] = {"features": [
        {"id": "w1", "properties": {"severity": "Extreme"}},
        {"id": "w2", "properties": None},
        {"properties": {"severity": "Severe"}},
    ]}
    (w,) = await hm._check_weather(None, 1.23456, -2.5)
    assert feeds.calls[0] == (hm._NWS_ACTIVE, {"point": "1.2346,-2.5000"})
    assert w == {"id": "w1", "event": "Weather alert", "severity": "Extreme",
                 "headline": "", "area": "", "instruction": ""}
    feeds.answers[hm._NWS_ACTIVE] = {"other": []}
    assert await hm._check_weather(None, 0, 0) == []


async def test_the_disaster_query_and_fields(hm, feeds, cfg):
    cfg.update(hazard_disaster_radius_km=111, hazard_disaster_days=5)
    feeds.answers[hm._EONET_EVENTS] = {"events": [
        {"id": "d1", "title": "", "categories": [{"title": "Volcanoes"}, None, {"title": "Ash"}],
         "geometry": [{"coordinates": [5, 5]}, {"coordinates": [0.1, 0]}, {"coordinates": ["x", 1]},
                      {"coordinates": None}],
         "sources": [{"url": "s1"}, {"url": "s2"}]},
        {"id": "d2", "geometry": [], "categories": []},
        {"title": "no id"},
        {"id": "d3", "geometry": [{"coordinates": [0, 0.2]}], "sources": []},
    ]}
    found = await hm._check_disasters(None, 0.0, 0.0)
    url, params = feeds.calls[0]
    assert url == hm._EONET_EVENTS
    assert params == {"status": "open", "days": "5", "bbox": "-1.0000,1.0000,1.0000,-1.0000"}
    assert found == [
        {"id": "d1", "title": "Natural event", "category": "Volcanoes, Ash", "dist_km": 11, "url": "s1"},
        {"id": "d3", "title": "Natural event", "category": "event", "dist_km": 22, "url": None},
    ]


# ── delivery ────────────────────────────────────────────────────────────────

@pytest.fixture
def requests_(hm, load, monkeypatch, cfg):
    dl = load("delivery")
    sent = []

    async def deliver(hass, req):
        sent.append(req)
    monkeypatch.setattr(dl, "async_deliver", deliver)
    monkeypatch.setattr(load("tts_helper"), "find_best_tts_entity", lambda hass: None)
    seen = []

    def target(hass, **kw):
        seen.append(kw)
        return ["media_player.a"]
    monkeypatch.setattr(load("audio_routing"), "broadcast_target", target)
    return types.SimpleNamespace(sent=sent, targets=seen, dl=dl)


async def test_a_spoken_delivery_request(hm, requests_, cfg):
    cfg.update(broadcast_group="", announcement_speakers=["media_player.a"])
    await hm._deliver(None, "push", "disaster", speak_text="say", night=True)
    (req,) = requests_.sent
    assert requests_.targets == [{"broadcast_group": None,
                                  "announcement_speakers": ["media_player.a"]}]
    assert (req.source, req.message, req.spoken, req.urgency, req.category) == (
        "hazard_disaster", "push", "say", "high", "hazard")
    assert (req.phone_only, req.quiet_ok, req.sleep_ok) == (False, True, True)
    assert req.speakers == ("media_player.a",) and req.push == requests_.dl.PUSH_ALWAYS
    assert (req.all_phones, req.push_first, req.tts_entity) == (True, True, "")
    assert (req.tts_context, req.record_kind) == ("hazard", "hazard")


async def test_a_phone_only_request_never_looks_up_speakers(hm, requests_):
    await hm._deliver(None, "push", "odd")
    (req,) = requests_.sent
    assert requests_.targets == [] and req.speakers == () and req.phone_only is True
    assert req.source == "odd" and req.spoken == ""


async def test_a_delivery_failure_is_swallowed(hm, requests_, monkeypatch):
    async def boom(hass, req):
        raise RuntimeError
    monkeypatch.setattr(requests_.dl, "async_deliver", boom)
    await hm._deliver(None, "push", "quake")


# ── the periodic check ──────────────────────────────────────────────────────

@pytest.fixture
def legacy(hm, monkeypatch, cfg):
    st = types.SimpleNamespace(delivered=[], quiet=False, found={
        "quake": [{"id": "q", "mag": 3.0, "dist_km": 5, "place": "P"}],
        "wx": [{"id": "w", "severity": "Severe", "event": "Storm"}],
        "disaster": [{"id": "d", "title": "T", "category": "C", "dist_km": 9}]})
    cfg["hazard_monitor_enabled"] = True

    async def deliver(hass, push_text, action_key, *, speak_text="", night=False):
        st.delivered.append((action_key, push_text, speak_text, night))
    monkeypatch.setattr(hm, "_deliver", deliver)
    monkeypatch.setattr(hm, "_in_quiet_hours", lambda hass: st.quiet)

    async def nothing(hass, honorific, *, in_quiet):
        return {"x": 0}
    monkeypatch.setattr(hm, "_poll_warnings", nothing)
    for name, key in (("_check_earthquakes", "quake"), ("_check_weather", "wx"),
                      ("_check_disasters", "disaster")):
        async def check(hass, lat, lon, _k=key):
            return st.found[_k]
        monkeypatch.setattr(hm, name, check)
    return st


async def test_every_new_event_is_spoken_and_counted(hm, legacy):
    out = await hm.periodic_check(_hass(lat=40.12345, lon=-74.98765), "sir")
    assert out == {"checked": True, "fired": {"quake": 1, "wx": 1, "disaster": 1},
                   "center": [40.123, -74.988], "warnings": {"x": 0}}
    assert [d[0] for d in legacy.delivered] == ["quake", "wx", "disaster"]
    assert all(push == speak and night is False for _k, push, speak, night in legacy.delivered)
    assert legacy.delivered[0][1].startswith("Seismic alert, sir.")


async def test_quiet_hours_send_the_legacy_feeds_to_the_phone(hm, legacy):
    legacy.quiet = True
    await hm.periodic_check(_hass())
    assert [d[2] for d in legacy.delivered] == ["", "", ""]


async def test_no_location_still_reports_the_warnings(hm, legacy, monkeypatch):
    monkeypatch.setattr(hm, "_home_latlon", lambda hass: None)
    assert await hm.periodic_check(_hass()) == {"skipped": "no_location", "warnings": {"x": 0}}
    assert legacy.delivered == []


async def test_a_failing_warnings_poll_is_an_empty_result(hm, legacy, monkeypatch):
    async def boom(hass, honorific, *, in_quiet):
        raise ValueError
    monkeypatch.setattr(hm, "_poll_warnings", boom)
    out = await hm.periodic_check(_hass())
    assert out["warnings"] == {} and out["fired"]["quake"] == 1


async def test_scan_without_a_location(hm, monkeypatch):
    async def fetched(hass):
        return {}
    monkeypatch.setattr(hm, "_fetch_warnings", fetched)
    monkeypatch.setattr(hm, "_home_latlon", lambda hass: None)
    assert await hm.scan_now(_hass()) == {"ok": False, "error": "no home location configured",
                                          "warnings": [], "counts": {"warnings": 0}}


async def test_scan_restores_the_seen_sets_even_on_a_failure(hm, monkeypatch):
    async def fetched(hass):
        raise RuntimeError
    monkeypatch.setattr(hm, "_fetch_warnings", fetched)
    hm._SEEN["quake"].add("old")

    async def check(hass, lat, lon):
        hm._SEEN["quake"].add("new")
        raise KeyError
    monkeypatch.setattr(hm, "_check_earthquakes", check)
    with pytest.raises(KeyError):
        await hm.scan_now(_hass())
    assert hm._SEEN["quake"] == {"old"}


def test_active_drops_cancels_unknown_levels_and_expired(hm):
    from datetime import datetime, timezone
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    keep = {"level": "red", "expiry": "2031-01-01T00:00:00+00:00"}
    rows = [keep, dict(keep, msg_type="Cancel"), dict(keep, level="blue"),
            dict(keep, expiry="2029-01-01T00:00:00+00:00")]
    assert hm._active(rows, now) == [keep]


async def test_the_poll_notes_the_count_for_situations(hm, load, monkeypatch, tmp_path):
    hw = load("hazard_warnings")
    monkeypatch.setattr(hw, "_store_path", lambda: str(tmp_path / "w.json"))
    noted = []
    monkeypatch.setattr(load("situations"), "note_weather", noted.append)
    delivered = []

    async def deliver(hass, push_text, action_key, *, speak_text="", night=False):
        delivered.append(action_key)
    monkeypatch.setattr(hm, "_deliver", deliver)
    warning = {"id": "a", "msg_type": "Alert", "refs": [], "level": "red", "type": "Wind",
               "onset": "2099-01-01T00:00:00+00:00", "expiry": "2099-01-02T00:00:00+00:00",
               "headline": "h", "areas": ["X"], "area_keys": ["X"], "source": "cap"}

    async def fetched(hass):
        return {"cap": (True, [warning]), "met_eireann": (False, [])}
    monkeypatch.setattr(hm, "_fetch_warnings", fetched)
    counts = await hm._poll_warnings(_hass(), "sir", in_quiet=False)
    assert counts == {"cap": 1, "met_eireann": 0}
    assert noted == [1] and delivered == ["warning"]
    assert set(hm._LAST_WARNINGS) == {"cap"}


async def test_nothing_fetched_is_an_empty_poll(hm, monkeypatch):
    async def fetched(hass):
        return {}
    monkeypatch.setattr(hm, "_fetch_warnings", fetched)
    assert await hm._poll_warnings(_hass(), "", in_quiet=True) == {}
    assert hm._STORE is None


async def test_status_shows_the_last_cap_warnings(hm, cfg):
    cfg.update(hazard_source="custom", hazard_lat=1, hazard_lon=2, hazard_quake_min_mag=4)
    hm._LAST_WARNINGS["cap"] = [{"id": "c", "level": "red", "type": "Wind", "areas": []}]
    st = await hm.status(_hass())
    assert st["using_override"] is True and st["center"] == [1.0, 2.0]
    assert st["home_center"] == [40.7, -74.0] and st["quake_min_mag"] == 4.0
    assert st["source"] == "custom" and len(st["warnings"]) == 1
    assert st["sources"] == {"met_eireann": False, "cap": True, "cap_configured": False}
    cfg["hazard_source"] = "us"
    st = await hm.status(_hass())
    assert st["warnings"] == [] and st["feeds"]["weather"] is True
