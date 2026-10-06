"""The hazard monitor with weather warnings (8.8.0): region defaults, delivery
levels, memory across restarts, and every reader of the scan."""
from __future__ import annotations

import json
import sys
import types

import pytest

from fakes import FakeHass

ORANGE = {"id": "o1", "msg_type": "Alert", "refs": [], "source": "met_eireann",
          "source_label": "Met Éireann", "type": "Wind", "level": "orange",
          "onset": "2099-09-09T12:00:00+01:00", "expiry": "2099-09-10T12:00:00+01:00",
          "headline": "Status Orange - Wind warning for Clare",
          "description": "<p>Prepare for impacts.</p>", "areas": ["Clare"], "area_keys": ["EI03"]}
YELLOW = dict(ORANGE, id="y1", level="yellow", type="Rain",
              headline="Status Yellow - Rain warning for Clare", area_keys=["EI03"])


def _hass(country="IE", lat=52.85, lon=-8.98):
    hass = FakeHass()
    hass.config = types.SimpleNamespace(country=country, latitude=lat, longitude=lon,
                                        time_zone="Europe/Dublin", language="en")
    return hass


@pytest.fixture
def cfg(load, monkeypatch):
    nc = load("nova_config")
    data: dict = {}
    monkeypatch.setattr(nc, "get", lambda k, d=None: data.get(k, d))
    monkeypatch.setattr(nc, "set", lambda k, v: data.__setitem__(k, v) or True)
    return data


@pytest.fixture
def hm(load, monkeypatch, tmp_path, cfg):
    mod = load("hazard_monitor")
    hw = load("hazard_warnings")
    monkeypatch.setattr(mod, "_STORE", None)
    monkeypatch.setattr(mod, "_LAST_WARNINGS", {})
    monkeypatch.setattr(hw, "_store_path", lambda: str(tmp_path / "hazard_warnings.json"))
    st = types.SimpleNamespace(met=(True, []), met_calls=[], delivered=[], legacy=[])

    async def met_fetch(hass, counties, *, lang, user_agent):
        st.met_calls.append(list(counties))
        if isinstance(st.met, Exception):
            raise st.met
        return st.met[0], st.met[1], "rss"
    monkeypatch.setattr(load("hazard_met_eireann"), "fetch", met_fetch)

    async def deliver(hass, push_text, action_key, *, speak_text=""):
        st.delivered.append((push_text, action_key, speak_text))
    monkeypatch.setattr(mod, "_deliver", deliver)

    for name, key in (("_check_earthquakes", "quake"), ("_check_weather", "wx"),
                      ("_check_disasters", "disaster")):
        async def check(hass, lat, lon, _k=key):
            st.legacy.append(_k)
            return []
        monkeypatch.setattr(mod, name, check)
    mod._st = st
    return mod


# ── region aware defaults ───────────────────────────────────────────────────

def test_an_irish_home_with_nothing_saved(hm):
    hass = _hass("IE")
    assert hm.effective_flag(hass, "hazard_met_eireann_on") is True
    for key in ("hazard_quakes_on", "hazard_weather_on", "hazard_disasters_on"):
        assert hm.effective_flag(hass, key) is False


def test_a_home_on_the_island_with_no_country_set_is_irish(hm):
    assert hm.is_irish_home(_hass(country="", lat=53.35, lon=-6.26)) is True
    assert hm.effective_flag(_hass(country=None, lat=53.35, lon=-6.26), "hazard_quakes_on") is False


def test_an_irish_home_keeps_its_saved_values(hm, cfg):
    cfg.update(hazard_quakes_on=True, hazard_weather_on=True, hazard_met_eireann_on=False)
    hass = _hass("IE")
    assert hm.effective_flag(hass, "hazard_quakes_on") is True
    assert hm.effective_flag(hass, "hazard_weather_on") is True
    assert hm.effective_flag(hass, "hazard_met_eireann_on") is False
    assert hm.effective_flag(hass, "hazard_disasters_on") is False   # still unset


def test_a_home_elsewhere_is_unchanged(hm, cfg):
    hass = _hass("US", 40.7, -74.0)
    assert hm.effective_flag(hass, "hazard_met_eireann_on") is False
    for key in ("hazard_quakes_on", "hazard_weather_on", "hazard_disasters_on"):
        assert hm.effective_flag(hass, key) is True
    cfg["hazard_weather_on"] = False
    assert hm.effective_flag(hass, "hazard_weather_on") is False


async def test_the_legacy_feeds_are_not_polled_for_an_irish_home(hm, cfg):
    cfg["hazard_monitor_enabled"] = True
    await hm.periodic_check(_hass("IE"))
    assert hm._st.legacy == [] and hm._st.met_calls == [["EI03"]]     # Clare, nearest
    hm._st.legacy.clear()
    await hm.periodic_check(_hass("US", 40.7, -74.0))
    assert hm._st.legacy == ["quake", "wx", "disaster"]


async def test_nothing_runs_while_the_monitor_is_off(hm, cfg):
    assert await hm.periodic_check(_hass()) == {"skipped": "disabled"}
    assert hm._st.met_calls == [] and hm._st.delivered == []


# ── delivery levels ─────────────────────────────────────────────────────────

async def test_at_the_speak_level_it_is_spoken_and_pushed(hm, cfg):
    cfg["hazard_monitor_enabled"] = True
    hm._st.met = (True, [ORANGE])
    await hm.periodic_check(_hass(), "sir")
    (push, action, speak), = hm._st.delivered
    assert action == "warning"
    assert speak.startswith("Met Éireann Orange wind warning for Clare, from ")
    assert speak.endswith(", sir. Status Orange - Wind warning for Clare")
    assert push.startswith(speak) and push.endswith("Prepare for impacts.\n\nSource: Met Éireann")


async def test_below_the_speak_level_goes_to_the_phone_only(hm, cfg):
    cfg["hazard_monitor_enabled"] = True
    hm._st.met = (True, [YELLOW])
    await hm.periodic_check(_hass(), "sir")
    (push, _action, speak), = hm._st.delivered
    assert speak == "" and "Yellow rain warning" in push


async def test_the_levels_are_settings(hm, cfg):
    cfg.update(hazard_monitor_enabled=True, hazard_push_level="orange", hazard_speak_level="red")
    hm._st.met = (True, [YELLOW, ORANGE])
    await hm.periodic_check(_hass(), "")
    (push, _a, speak), = hm._st.delivered            # yellow not pushed at all
    assert speak == "" and "Orange wind" in push


async def test_announced_once_upgraded_then_cancelled_phone_only(hm, cfg):
    cfg["hazard_monitor_enabled"] = True
    hm._st.met = (True, [YELLOW])
    await hm.periodic_check(_hass(), "")
    await hm.periodic_check(_hass(), "")
    assert len(hm._st.delivered) == 1
    hm._st.met = (True, [dict(YELLOW, level="orange")])
    await hm.periodic_check(_hass(), "")
    up_push, _a, up_speak = hm._st.delivered[-1]
    assert up_speak.startswith("Met Éireann has upgraded the rain warning for Clare from Yellow to Orange")
    hm._st.met = (False, [])                          # a failed poll: nothing
    await hm.periodic_check(_hass(), "")
    assert len(hm._st.delivered) == 2
    hm._st.met = (True, [])                           # a good empty list: cancelled
    await hm.periodic_check(_hass(), "")
    cancel_push, _a, cancel_speak = hm._st.delivered[-1]
    assert cancel_speak == "" and cancel_push.startswith("Met Éireann has cancelled the Orange rain warning")
    assert len(hm._st.delivered) == 3


async def test_a_restart_never_repeats_a_standing_warning(hm, cfg, load, monkeypatch):
    cfg["hazard_monitor_enabled"] = True
    hm._st.met = (True, [ORANGE])
    await hm.periodic_check(_hass(), "")
    assert len(hm._st.delivered) == 1
    monkeypatch.setattr(hm, "_STORE", None)          # a restart: memory from disk only
    monkeypatch.setattr(hm, "_LAST_WARNINGS", {})
    await hm.periodic_check(_hass(), "")
    assert len(hm._st.delivered) == 1


async def test_a_failing_source_never_breaks_the_check(hm, cfg):
    cfg["hazard_monitor_enabled"] = True
    hm._st.met = RuntimeError("boom")
    res = await hm.periodic_check(_hass(), "")
    assert res["checked"] is True and hm._st.delivered == []


async def test_deliver_uses_the_existing_push_and_speech_paths(load, monkeypatch):
    mod = load("hazard_monitor")
    cc = load("cognitive_core")
    tts = load("tts_helper")
    ar = load("audio_routing")
    pushed, spoken = [], []

    async def notify(hass, cfg_obj, message, action):
        pushed.append((message, action))

    async def announce(hass, text, tts_entity, speakers, **kw):
        spoken.append((text, kw))
    monkeypatch.setattr(cc, "_notify_all_devices", notify)
    monkeypatch.setattr(tts, "find_best_tts_entity", lambda hass: "tts.x")
    monkeypatch.setattr(tts, "async_announce", announce)
    monkeypatch.setattr(ar, "broadcast_target", lambda hass, **kw: ["media_player.a"])
    await mod._deliver(FakeHass(), "push text", "warning")
    assert pushed == [("push text", "hazard_weather")] and spoken == []
    await mod._deliver(FakeHass(), "push text", "warning", speak_text="say this")
    assert spoken == [("say this", {"context": "hazard"})]


# ── readers: scan_now, status, nova/hazard, the agent tool, the briefing ────

async def test_scan_now_adds_warnings_and_keeps_every_old_key(hm, cfg):
    hm._st.met = (True, [ORANGE, dict(ORANGE, id="gone", expiry="2000-01-01T00:00:00+00:00")])
    res = await hm.scan_now(_hass())
    assert set(res) >= {"ok", "center", "earthquakes", "weather", "disasters", "counts"}
    assert res["counts"] == {"earthquakes": 0, "weather": 0, "disasters": 0, "warnings": 1}
    (w,) = res["warnings"]
    assert w["counties"] == ["Clare"] and w["type"] == "Wind" and w["level"] == "orange"
    assert w["from"] == "Wed 9 Sep 12:00" and w["to"] == "Thu 10 Sep 12:00"
    assert w["headline"] == ORANGE["headline"] and w["source_label"] == "Met Éireann"
    assert hm._st.delivered == [] and hm._STORE is None          # read only


async def test_status_adds_the_county_and_level_settings(hm, cfg):
    cfg.update(hazard_counties='["EI03", "EI16"]', hazard_speak_level="red")
    st = await hm.status(_hass())
    assert {"enabled", "center", "using_override", "quake_radius_km", "quake_min_mag",
            "disaster_radius_km", "feeds"} <= set(st)
    assert st["in_ireland"] is True and st["detected_county"] == {"code": "EI03", "name": "Clare"}
    assert st["counties"] == [{"code": "EI03", "name": "Clare"}, {"code": "EI16", "name": "Limerick"}]
    assert len(st["county_table"]) == 26 and st["county_table"][0]["name"] == "Carlow"
    assert (st["push_level"], st["speak_level"]) == ("yellow", "red")
    assert st["feeds"] == {"earthquakes": False, "weather": False, "disasters": False}
    assert st["sources"] == {"met_eireann": True, "cap": False, "cap_configured": False}


async def test_ws_hazard_returns_the_new_data(hm, cfg, load, monkeypatch):
    from test_pin_ws_update_config import _Conn, _stub_ws_api
    _stub_ws_api(monkeypatch)
    sys.modules.pop("jc.ws_modes", None)
    ws = load("ws_modes")
    hm._st.met = (True, [ORANGE])
    conn = _Conn()
    await ws.ws_hazard(_hass(), conn, {"id": 1, "action": "scan"})
    assert conn.results[0][1]["warnings"][0]["id"] == "o1"
    assert "earthquakes" in conn.results[0][1]
    conn = _Conn()
    await ws.ws_hazard(_hass(), conn, {"id": 2, "action": "status"})
    assert conn.results[0][1]["detected_county"]["name"] == "Clare"
    sys.modules.pop("jc.ws_modes", None)


async def test_the_agent_tool_returns_the_warnings(hm, load):
    env = load("agent_runtime.capabilities.environment")
    hm._st.met = (True, [ORANGE])
    res = json.loads(await env._exec_hazard_report(_hass(), {}))
    assert res["warnings"][0]["headline"] == ORANGE["headline"]
    assert {"earthquakes", "weather", "disasters", "counts"} <= set(res)


def test_the_briefing_mentions_only_orange_and_red(load):
    br = load("briefing")
    hz = {"ok": True, "earthquakes": [], "weather": [], "disasters": [], "warnings": [
        {"level": "yellow", "type": "Rain", "counties": ["Clare"], "source_label": "Met Éireann",
         "from": "a", "to": "b", "headline_text": "Yellow headline"},
        {"level": "red", "type": "Wind", "counties": ["Clare", "Galway"],
         "source_label": "Met Éireann", "from": "Wed 9 Sep 12:00", "to": "Thu 10 Sep 12:00",
         "headline_text": "Status Red - Wind warning for Clare, Galway"}]}
    (line,) = br._hazard_context(hz)
    assert "Yellow headline" not in line
    assert line == ("Weather warnings in force:\n- Met Éireann Red Wind warning for Clare, Galway "
                    "from Wed 9 Sep 12:00 to Thu 10 Sep 12:00. Headline (quote exactly): "
                    "Status Red - Wind warning for Clare, Galway")


def test_the_briefing_keeps_the_legacy_lines(load):
    br = load("briefing")
    hz = {"ok": True, "earthquakes": [{"mag": 3.1, "dist_km": 40, "place": "x"}],
          "weather": [], "disasters": []}
    assert br._hazard_context(hz) == ["Active hazards nearby: magnitude 3.1 quake 40 km away (x)."]
    assert br._hazard_context({"ok": False}) == []


async def test_a_custom_cap_feed_alerts_only_for_a_matched_area(hm, cfg, load, monkeypatch):
    cap = load("hazard_cap")
    cfg.update(hazard_monitor_enabled=True, hazard_met_eireann_on=False, hazard_cap_on=True,
               hazard_cap_url="https://alerts.example.org/index.xml",
               hazard_cap_area_codes='["NUTS-X"]')
    alerts = [
        {"identifier": "c1", "status": "Actual", "msg_type": "Alert", "references": [],
         "event": "Storm", "severity": "Extreme", "onset": "", "expires": "2099-01-01T00:00:00+00:00",
         "headline": "Storm warning", "description": "Stay in.", "params": {},
         "areas": [{"desc": "Our district", "polygons": [], "circles": [],
                    "geocodes": [("NUTS3", "nuts-x")]}]},
        {"identifier": "c2", "status": "Actual", "msg_type": "Alert", "references": [],
         "event": "Flood", "severity": "Extreme", "onset": "", "expires": "2099-01-01T00:00:00+00:00",
         "headline": "Elsewhere", "description": "", "params": {},
         "areas": [{"desc": "Far away", "polygons": [], "circles": [], "geocodes": []}]},
    ]
    seen = []

    async def collect(hass, url, *, lang, user_agent):
        seen.append((url, lang))
        return True, alerts
    monkeypatch.setattr(cap, "collect", collect)
    await hm.periodic_check(_hass("DE", 50.1, 8.7), "")
    assert seen == [("https://alerts.example.org/index.xml", "en")]
    (push, _a, speak), = hm._st.delivered
    assert speak.startswith("Custom CAP feed Red storm warning for Our district")
    assert push.endswith("Stay in.\n\nSource: Custom CAP feed")
