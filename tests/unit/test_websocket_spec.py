"""websocket.py, checked against its spec (7e).

websocket.py registers every nova/* panel command, owns the entry and runtime
config readers the ws_* siblings reach through ws_bridge, and handles the
panel data, activity log, config write, debug log, health, provider
activity, semantic search, documents, sparklines and appliance reload
commands. The decorators are stubbed to pass through, so each handler body
runs with a recording connection, a fake entry and fake collaborators. The
older pin files (test_websocket_panel.py, test_websocket_update_config.py,
test_hazard_settings.py) cover the main paths; this file covers the readers,
fallbacks and branches they leave out.
"""
from __future__ import annotations

import ast
import json
import logging
import pathlib
import sys
import types

import pytest

from fakes import FakeHass
from ws_kit import _Conn, _entry, _hass, _stub_ws_api, _update

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


@pytest.fixture
def ws(load, monkeypatch):
    _stub_ws_api(monkeypatch)
    monkeypatch.delitem(sys.modules, "jc.websocket", raising=False)
    monkeypatch.setattr(sys.modules["jc"], "websocket", None, raising=False)
    mod = load("websocket")
    monkeypatch.setattr(mod, "_get_knowledge_stats", lambda: {"facts": 0})
    return mod


@pytest.fixture
def store(load, monkeypatch):
    """config.json as a dict, with every nova_config write recorded."""
    nc = load("nova_config")
    data: dict = {}
    calls: list = []
    state = types.SimpleNamespace(data=data, calls=calls, ok=True)

    def _set(key, value):
        calls.append(("set", key, value))
        data[key] = value
        return state.ok
    monkeypatch.setattr(nc, "set", _set)
    monkeypatch.setattr(nc, "set_many", lambda values: calls.append(("set_many", dict(values)))
                        or data.update(values) or True)
    monkeypatch.setattr(nc, "delete", lambda key: calls.append(("delete", key))
                        or data.pop(key, None) or state.ok)
    monkeypatch.setattr(nc, "get", lambda key, default=None: data.get(key, default))
    return state


async def _call(handler, hass, **msg):
    conn = _Conn()
    await handler(hass, conn, {"id": 3, **msg})
    return conn


def _broken(*_a, **_k):
    raise RuntimeError("broken")


# ── Registration ────────────────────────────────────────────────────────────

def _registered_names():
    tree = ast.parse((COMP / "websocket.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "async_register")
    return [ast.unparse(c.args[1]) for c in ast.walk(fn)
            if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "async_register_command"]


def test_every_handler_is_registered_once(ws, monkeypatch):
    seen = []
    monkeypatch.setattr(ws.websocket_api, "async_register_command",
                        lambda hass, handler: seen.append(handler))
    ws.async_register(FakeHass())
    names = _registered_names()
    assert len(seen) == len(names) == 81 and len(set(names)) == 81
    assert seen == [getattr(ws, n) for n in names]
    for local in ("ws_get_panel_data", "ws_get_activity_log", "ws_update_config",
                  "ws_reload_appliances", "ws_get_debug_log", "ws_get_area_sparklines",
                  "ws_documents", "ws_semantic_search", "ws_diagnostics",
                  "ws_get_setup_health", "ws_get_provider_activity"):
        assert local in names


def test_a_registration_failure_is_logged_at_debug_and_swallowed(ws, monkeypatch, caplog):
    monkeypatch.setattr(ws.websocket_api, "async_register_command", _broken)
    with caplog.at_level(logging.DEBUG, logger=ws.__name__):
        ws.async_register(FakeHass())
    assert any(r.levelno == logging.DEBUG and "WS command register note" in r.getMessage()
               for r in caplog.records)


def test_the_log_names_are_the_ws_log_objects(ws, load):
    wl = load("ws_log")
    for name in ("nova_log", "recent_debug_log", "recent_conversation_log",
                 "_DEBUG_LOG", "_LOG_QUEUE", "_ensure_writer"):
        assert getattr(ws, name) is getattr(wl, name), name
    assert not hasattr(ws, "_LOG_FILE")


def test_the_writable_keys_come_from_the_settings_schema(ws, load):
    assert ws.PANEL_WRITABLE_KEYS == set(load("settings_schema").PANEL_KEYS)


def test_the_version_is_the_manifest_version(ws):
    manifest = json.loads((COMP / "manifest.json").read_text())
    assert ws._INTEGRATION_VERSION == manifest["version"]


# ── Entry and runtime readers ───────────────────────────────────────────────

def test_get_entry_is_the_first_entry_or_none(ws):
    first, second = types.SimpleNamespace(n=1), types.SimpleNamespace(n=2)
    hass = FakeHass()
    hass.config_entries = types.SimpleNamespace(async_entries=lambda domain: [first, second])
    assert ws._get_entry(hass) is first
    hass.config_entries = types.SimpleNamespace(async_entries=lambda domain: [])
    assert ws._get_entry(hass) is None


def test_no_entry_reads_an_empty_runtime_config(ws):
    assert ws._live_runtime_config(None) == {}
    assert ws._executor_runtime_config(None) == {}


def test_the_executor_gets_a_copy_not_the_live_dict(ws, load):
    entry = _entry(load, runtime_config={"a": 1})
    snap = ws._executor_runtime_config(entry)
    assert snap == {"a": 1} and snap is not entry.runtime_data.runtime_config


def test_runtime_opt_reads_live_first_and_passes_none_as_hass(ws, load, monkeypatch):
    nc = load("nova_config")
    asked = []
    monkeypatch.setattr(nc, "runtime_get",
                        lambda hass, entry, key, default: asked.append((hass, key)) or "lower")
    entry = _entry(load, runtime_config={"live": 0, "blank": "", "gone": None})
    assert ws._runtime_opt(object(), entry, "live", 9) == 0
    assert ws._runtime_opt(object(), entry, "blank", 9) == "lower"
    assert ws._runtime_opt(object(), entry, "gone", 9) == "lower"
    assert ws._entry_opt(entry, "x", 1) == "lower"
    assert asked == [(None, "blank"), (None, "gone"), (None, "x")]


@pytest.mark.parametrize("stored,expected", [(0, 0), ("7", 7), ("x", 15), (None, 15)])
def test_int_opt_keeps_zero_and_falls_back_on_bad_values(ws, load, monkeypatch, stored, expected):
    monkeypatch.setattr(load("nova_config"), "runtime_get", lambda h, e, k, d: d)
    entry = _entry(load, runtime_config={"n": stored})
    assert ws._int_opt(None, entry, "n", 15) == expected


def test_runtime_json_walks_live_then_config_then_entry(ws, load, store, monkeypatch):
    entry = _entry(load, runtime_config={"a": [1], "b": "{bad", "c": '{"x": 1}'})
    assert ws._get_runtime_json(None, entry, "a", None) == [1]
    assert ws._get_runtime_json(None, entry, "c", None) == {"x": 1}
    store.data["b"] = '["saved"]'
    assert ws._get_runtime_json(None, entry, "b", None) == ["saved"]
    monkeypatch.setattr(load("nova_config"), "runtime_get",
                        lambda h, e, k, d: '{"opt": true}' if k == "d" else d)
    assert ws._get_runtime_json(None, entry, "d", None) == {"opt": True}
    assert ws._get_runtime_json(None, entry, "e", "dflt") == "dflt"
    assert ws._get_runtime_json(None, None, "e", "dflt") == "dflt"


def test_bad_json_in_config_json_is_skipped(ws, load, store):
    # Fixed (bugs.md #16): bad JSON in config.json falls through to the next
    # layer, like bad JSON in the live value or the entry options.
    entry = _entry(load, runtime_config={"b": "{bad"})
    store.data["b"] = "not json"
    assert ws._get_runtime_json(None, entry, "b", {"d": 1}) == {"d": 1}


def test_runtime_json_skips_a_config_store_that_fails(ws, load, monkeypatch):
    monkeypatch.setattr(load("nova_config"), "get", _broken)
    monkeypatch.setattr(load("nova_config"), "runtime_get", lambda h, e, k, d: "[2]")
    assert ws._get_runtime_json(None, _entry(load), "k", None) == [2]


@pytest.mark.parametrize("stored,expected", [
    (["a"], ["a"]), ('["b", "c"]', ["b", "c"]), ("garbage", []), (5, [])])
def test_disabled_rules_accept_a_list_or_json(ws, load, stored, expected):
    entry = _entry(load, runtime_config={"disabled_sentinel_rules": stored})
    assert ws._get_disabled_rules(None, entry) == expected


def test_disabled_rules_without_an_entry_are_empty(ws):
    assert ws._get_disabled_rules(None, None) == []


# ── Display helpers and their fallbacks ─────────────────────────────────────

def test_hazard_helpers_fall_back_when_the_monitor_is_missing(ws, load, monkeypatch):
    monkeypatch.setitem(sys.modules, "jc.hazard_monitor", None)
    monkeypatch.setattr(sys.modules["jc"], "hazard_monitor", None, raising=False)
    entry = _entry(load, runtime_config={"hazard_lat": 52.0})
    assert ws._hazard_flag(None, "hazard_met_eireann_on") is False
    assert ws._hazard_flag(None, "hazard_quakes_on") is True
    assert ws._hazard_source(None, entry) == "custom"
    assert ws._hazard_coordinate(None, entry, "hazard_lat", 0) == 52.0
    assert ws._hazard_coordinate(None, entry, "hazard_lon", 1) == ""


def test_a_saved_hazard_source_outside_the_three_is_ignored(ws, load, monkeypatch):
    hm = load("hazard_monitor")
    monkeypatch.setattr(hm, "effective_source", lambda hass: "us")
    entry = _entry(load, runtime_config={"hazard_source": "nws"})
    assert ws._hazard_source(None, entry) == "us"


def test_hazard_coordinate_is_blank_with_no_home(ws, load, monkeypatch):
    monkeypatch.setattr(load("hazard_monitor"), "base_home_latlon", lambda hass: None)
    assert ws._hazard_coordinate(None, _entry(load), "hazard_lat", 0) == ""


def test_exempt_locks_are_sorted_or_empty(ws, load, monkeypatch):
    cls = load("core_lockdown_sync")
    monkeypatch.setattr(cls, "_lockdown_exempt_locks", lambda: {"lock.b", "lock.a"})
    assert ws._get_lockdown_exempt_locks() == ["lock.a", "lock.b"]
    monkeypatch.setattr(cls, "_lockdown_exempt_locks", _broken)
    assert ws._get_lockdown_exempt_locks() == []


def test_home_doors_fall_back_when_a_read_fails(ws, load, monkeypatch, caplog):
    hd = load("home_doors")
    monkeypatch.setattr(hd, "has_garage", _broken)
    entry = _entry(load, runtime_config={"garage_mode": "attached"})
    with caplog.at_level(logging.DEBUG, logger=ws.__name__):
        out = ws._get_home_doors(FakeHass(), entry)
    assert out == {"garage_mode": hd.garage_mode({"garage_mode": "attached"}),
                   "has_garage": False, "exit_doors": [], "exit_door_status": [],
                   "exit_door_candidates": [], "basement_mode": hd.basement_mode({}),
                   "home_features": {"basement": False, "utility": False}}
    assert "home doors read failed" in caplog.text


def test_host_health_status_lists_every_metric(ws, load):
    hh = load("host_health")
    out = ws._get_host_health_status(FakeHass(), _entry(load))
    assert [m["key"] for m in out["metrics"]] == [m.key for m in hh.METRICS]
    assert set(out["metrics"][0]) == {"key", "label", "recommended", "status", "source",
                                      "entity_id", "candidates"}
    assert "snapshot" in out


def test_host_health_failure_is_a_safe_error(ws, load, monkeypatch):
    monkeypatch.setattr(load("host_health"), "discover_candidates", _broken)
    out = ws._get_host_health_status(FakeHass(), _entry(load))
    assert out == {"error": "RuntimeError (details are in the Home Assistant log)"}


def test_labels_are_sorted_by_name_or_empty(ws, monkeypatch):
    lr = types.ModuleType("homeassistant.helpers.label_registry")
    labels = {"1": types.SimpleNamespace(label_id="1", name="zeta"),
              "2": types.SimpleNamespace(label_id="2", name="Alpha"),
              "3": types.SimpleNamespace(label_id="3", name=None)}
    lr.async_get = lambda hass: types.SimpleNamespace(labels=labels)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.label_registry", lr)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "label_registry", lr,
                        raising=False)
    assert ws._available_labels(None) == [{"id": "3", "name": None},
                                          {"id": "2", "name": "Alpha"},
                                          {"id": "1", "name": "zeta"}]
    lr.async_get = _broken
    assert ws._available_labels(None) == []


def test_memory_stats_fall_back_when_memory_fails(ws, monkeypatch):
    broken = types.ModuleType("jc.memory")
    broken.get_memory_stats = _broken
    monkeypatch.setitem(sys.modules, "jc.memory", broken)
    monkeypatch.setattr(sys.modules["jc"], "memory", broken, raising=False)
    assert ws._get_memory_stats() == {"backend": "unavailable", "total_memories": 0}


async def test_voice_id_readers_fall_back(ws, monkeypatch):
    for name in ("voice_id_store", "voice_enrol"):
        monkeypatch.setitem(sys.modules, f"jc.{name}", None)
        monkeypatch.setattr(sys.modules["jc"], name, None, raising=False)
    assert await ws._voice_id_token(FakeHass()) == ""
    assert ws._get_voice_id_enrol() == {"state": "idle"}


def test_masked_url_masks_only_credentials(ws):
    assert ws._masked_url("") == "" and ws._masked_url(0) == ""
    assert ws._masked_url("http://u:p@h") == "http://**REDACTED**@h"
    assert ws._masked_url("http://h:1/x") == "http://h:1/x"


def test_named_log_entries_keep_the_list_when_there_is_nothing_to_name(ws, monkeypatch):
    entries = [{"msg": "light.kitchen"}]
    assert ws._named_log_entries(entries, {}) is entries
    monkeypatch.setitem(sys.modules, "jc.cognitive.naming", None)
    assert ws._named_log_entries(entries, {"light.kitchen": "Kitchen"}) is entries


# ── nova/get_panel_data ─────────────────────────────────────────────────────

@pytest.fixture
def house(ws, load, monkeypatch):
    """Two areas, one occupied, with a satellite in the dominant one."""
    ar = load("audio_routing")
    occupied = {"kitchen"}
    monkeypatch.setattr(ws, "_all_areas_with_anything", lambda hass: ["office", "bed", "kitchen"])
    monkeypatch.setattr(ws, "_area_capabilities", lambda hass, a: {"lights": True})
    monkeypatch.setattr(ws, "_area_light_state", lambda hass, a: (1, 2))
    monkeypatch.setattr(ws, "_area_name", lambda hass, a: a.title())
    monkeypatch.setattr(ws, "_area_live_readings",
                        lambda hass, a: {"temp": 20.5, "humidity": None, "lights": "1/2",
                                         "last_motion_seconds": 90 if a == "kitchen" else None})
    monkeypatch.setattr(ws, "_area_temp_humidity_entities",
                        lambda hass, a: (f"sensor.{a}_t", None))
    monkeypatch.setattr(ar, "is_area_occupied", lambda hass, a: a in occupied)
    monkeypatch.setattr(ar, "satellites_in_area",
                        lambda hass, a: ["assist_satellite.kitchen_satellite_with_a_long_name"])
    monkeypatch.setattr(ws, "_dominant_area", lambda hass: "kitchen")
    monkeypatch.setattr(ws, "_satellite_count", lambda hass: (1, 2))
    return occupied


async def test_panel_areas_sort_occupied_then_bedrooms_then_name(ws, load, house, monkeypatch):
    monkeypatch.setattr(load("nova_config"), "runtime_get",
                        lambda h, e, k, d: ["bed"] if k == "bedroom_areas" else d)
    res = (await _call(ws.ws_get_panel_data, _hass(_entry(load)))).results[0][1]
    assert [a["id"] for a in res["areas"]] == ["kitchen", "bed", "office"]
    kitchen = res["areas"][0]
    assert kitchen == {"id": "kitchen", "name": "Kitchen", "caps": {"lights": True},
                       "active": True, "bedroom": False, "lights_on": 1, "lights_total": 2,
                       "temp": 20.5, "humidity": None, "temp_entity": "sensor.kitchen_t",
                       "humidity_entity": None,
                       "last_motion": ws._format_duration(90)}
    assert res["meta"]["bedrooms"] == 1 and res["meta"]["areas_monitored"] == 3
    assert res["meta"]["est_cost"] == "—"


async def test_panel_dominant_room_is_described(ws, load, house):
    res = (await _call(ws.ws_get_panel_data, _hass(_entry(load)))).results[0][1]
    assert res["dominant"] == {
        "area_id": "kitchen", "name": "Kitchen",
        "subtitle": f"Occupied · {ws._format_duration(90)}", "coord": "#kitchen",
        "temp": 20.5, "humidity": "—", "lights": "1/2",
        "satellite": "kitchen_satellite_wi", "last_motion": ws._format_duration(90)}
    assert res["status"]["satellites"] == {"state": "1 / 2", "level": "warn"}


async def test_panel_dominant_without_motion_is_just_occupied(ws, load, house, monkeypatch):
    monkeypatch.setattr(ws, "_dominant_area", lambda hass: "office")
    monkeypatch.setattr(load("audio_routing"), "satellites_in_area", lambda hass, a: [])
    res = (await _call(ws.ws_get_panel_data, _hass(_entry(load)))).results[0][1]
    assert res["dominant"]["subtitle"] == "Occupied"
    assert res["dominant"]["satellite"] == "—"


async def test_panel_with_nobody_in_a_room_but_someone_home(ws, load, monkeypatch):
    monkeypatch.setattr(ws, "_dominant_area", lambda hass: None)
    monkeypatch.setattr(load("audio_routing"), "anyone_home", lambda hass: True)
    res = (await _call(ws.ws_get_panel_data, _hass(_entry(load)))).results[0][1]
    assert res["dominant"] == {"area_id": None, "name": "AT HOME",
                               "subtitle": "no presence detected", "coord": "—",
                               "temp": "—", "humidity": "—", "lights": "—",
                               "satellite": "—", "last_motion": "—"}


@pytest.mark.parametrize("counts,tile", [
    ((2, 2), {"state": "2 / 2", "level": "live"}),
    ((0, 0), {"state": "NONE", "level": "off"}),
])
async def test_panel_satellite_tile(ws, load, monkeypatch, counts, tile):
    monkeypatch.setattr(ws, "_satellite_count", lambda hass: counts)
    res = (await _call(ws.ws_get_panel_data, _hass(_entry(load)))).results[0][1]
    assert res["status"]["satellites"] == tile


async def test_panel_status_tiles_follow_the_settings(ws, load, monkeypatch):
    sd = load("sleep_detection")
    monkeypatch.setattr(sd, "is_sleeping", lambda hass, **kw: (True, "quiet hours"))
    monkeypatch.setattr(ws, "sleep_detection", sd)
    monkeypatch.setattr(load("nova_config"), "runtime_get", lambda h, e, k, d: {
        "gemini_api_key": "k", "broadcast_group": "media_player.all"}.get(k, d))
    entry = _entry(load, runtime_config={"notify_service": "notify.phone"})
    res = (await _call(ws.ws_get_panel_data, _hass(entry))).results[0][1]
    st = res["status"]
    assert st["sleep"] == {"state": "ASLEEP", "level": "warn"}
    assert st["gemini"] == {"state": "READY", "level": "live"}
    assert st["broadcast"] == {"state": "ONLINE", "level": "live"}
    assert st["notify"] == {"state": "READY", "level": "live"}
    assert res["sleep_reason"] == "quiet hours"
    assert res["config"]["notify_service"] == "notify.phone"
    assert res["config"]["notify_services"] == ["notify.phone"]


async def test_panel_status_tiles_when_nothing_is_set(ws, load):
    res = (await _call(ws.ws_get_panel_data, _hass(_entry(load)))).results[0][1]
    st = res["status"]
    assert st["gemini"] == {"state": "UNSET", "level": "warn"}
    assert st["broadcast"] == {"state": "UNSET", "level": "warn"}
    assert st["notify"] == {"state": "UNSET", "level": "warn"}
    assert res["config"]["notify_service"] == ""


async def test_panel_observer_tile_when_running(ws, load):
    entry = _entry(load)
    entry.runtime_data.observer_running = True
    res = (await _call(ws.ws_get_panel_data, _hass(entry))).results[0][1]
    assert res["status"]["observer"] == {"state": "RUNNING", "level": "live"}


async def test_panel_lists_the_notify_services_home_assistant_has(ws, load):
    hass = _hass(_entry(load))
    hass.services.register("notify", "mobile_app_one")
    res = (await _call(ws.ws_get_panel_data, hass)).results[0][1]
    assert res["config"]["notify_services_available"] == ["notify.mobile_app_one"]


async def test_panel_config_fallbacks(ws, load):
    entry = _entry(load, runtime_config={
        "camera_event_learning": False, "llm_provider": "groq", "model": "m1",
        "movie_dim_pct": 0, "home_context_max_entities": 0,
        "ground_floor_areas": ["hall"]})
    cfg = (await _call(ws.ws_get_panel_data, _hass(entry))).results[0][1]["config"]
    assert cfg["camera_historical_awareness"] is False
    assert cfg["suggestion_review_provider"] == "groq"
    assert cfg["suggestion_review_model"] == "m1"
    assert cfg["home_context_max_entities"] == 0      # 0 is kept here
    assert cfg["ground_floor_areas"] == ["hall"]
    assert cfg["sentinel_enabled"] is True and cfg["observer_enabled"] is False
    assert cfg["self_hosted_endpoints_migrated"] is False


async def test_a_saved_zero_movie_dim_stays_zero(ws, load):
    # Fixed (bugs.md #15): a saved 0 shows as 0; only blank reads as 15.
    entry = _entry(load, runtime_config={"movie_dim_pct": 0})
    cfg = (await _call(ws.ws_get_panel_data, _hass(entry))).results[0][1]["config"]
    assert cfg["movie_dim_pct"] == 0
    entry = _entry(load, runtime_config={"movie_dim_pct": ""})
    cfg = (await _call(ws.ws_get_panel_data, _hass(entry))).results[0][1]["config"]
    assert cfg["movie_dim_pct"] == 15


async def test_panel_meta_uptime_counts_from_import(ws, load, monkeypatch):
    monkeypatch.setattr(ws, "_STARTUP_TS", ws.time.time() - 7200)
    seen = []
    monkeypatch.setattr(ws, "_format_uptime", lambda s: seen.append(s) or "2h")
    res = (await _call(ws.ws_get_panel_data, _hass(_entry(load)))).results[0][1]
    assert res["meta"]["uptime"] == "2h" and 7199 < seen[0] < 7300


async def test_panel_data_without_an_entry_still_answers(ws, load):
    conn = await _call(ws.ws_get_panel_data, _hass(None))
    assert conn.errors == [] and conn.results[0][1]["status"]["observer"]["state"] == "DISABLED"


# ── nova/get_activity_log ───────────────────────────────────────────────────

async def test_activity_rows_use_friendly_names_and_short_stamps(ws, load, monkeypatch):
    rows = [{"timestamp": "2026-04-23T05:30:00", "entity_id": "light.kitchen",
             "message": "light.kitchen on", "source": "sentinel"},
            {"timestamp": "abc", "entity_id": "sensor.a_very_long_object_id_beyond_twenty",
             "message": "x"},
            {"message": "y", "source": "agent"}]
    monkeypatch.setattr(load("database"), "get_recent_activity",
                        lambda hours, limit: rows)
    monkeypatch.setattr(ws, "_entity_names", lambda hass: {"light.kitchen": "Kitchen Light"})
    conn = await _call(ws.ws_get_activity_log, _hass(_entry(load)), hours=2, limit=3)
    first, second, third = conn.results[0][1]["entries"]
    assert first == {"ts": "05:30", "urgency": "low", "tag": "KITCHEN LIGHT",
                     "msg": "Kitchen Light on", "source": "sentinel"}
    assert second["ts"] == "abc" and second["tag"] == "A_VERY_LONG_OBJECT_I"
    assert third["tag"] == "AGENT" and third["ts"] == ""


async def test_activity_failure_is_a_warning(ws, load, monkeypatch, caplog):
    monkeypatch.setattr(load("database"), "get_recent_activity", _broken)
    with caplog.at_level(logging.WARNING, logger=ws.__name__):
        conn = await _call(ws.ws_get_activity_log, _hass(_entry(load)), hours=1, limit=1)
    assert conn.errors[0][1] == "activity_log_failed"
    assert any(r.levelno == logging.WARNING and "ws_get_activity_log failed" in r.getMessage()
               for r in caplog.records)


# ── nova/update_config branches ─────────────────────────────────────────────

async def test_voice_id_fallback_cannot_be_nova_itself(ws, load, store):
    entry = _entry(load)
    entry.runtime_data.voice_id = types.SimpleNamespace(entity_id="stt.nova_voice_id")
    conn = await _update(ws, _hass(entry), "voice_id_fallback_stt", "stt.nova_voice_id")
    assert conn.errors == [(1, "invalid_value", "The fallback can't be Nova Voice ID itself")]
    assert store.calls == []


async def test_voice_id_settings_refresh_the_entity(ws, load, store):
    refreshed = []

    async def refresh():
        refreshed.append(1)
    entry = _entry(load)
    entry.runtime_data.voice_id = types.SimpleNamespace(entity_id="stt.nova_voice_id",
                                                        async_refresh_info=refresh)
    conn = await _update(ws, _hass(entry), "voice_id_uri", "  tcp://10.0.0.9:10300  ")
    assert conn.results[0][1] == {"key": "voice_id_uri", "value": "tcp://10.0.0.9:10300",
                                  "persisted": True}
    conn = await _update(ws, _hass(entry), "voice_id_fallback_stt", "stt.other")
    assert conn.errors == [] and refreshed == [1, 1]


async def test_voice_id_settings_without_the_entity_still_save(ws, load, store):
    conn = await _update(ws, _hass(_entry(load)), "voice_id_fallback_stt", "stt.other")
    assert conn.results[0][1]["persisted"] is True


async def test_exempt_locks_are_applied_live(ws, load, store, monkeypatch):
    applied = []

    async def apply(key, value):
        applied.append((key, value))
    monkeypatch.setattr(load("cognitive_core"), "apply_runtime_config", apply)
    await _update(ws, _hass(_entry(load)), "lockdown_exempt_locks", '["lock.shed"]')
    assert applied == [("lockdown_exempt_locks", '["lock.shed"]')]


async def test_a_write_logs_the_key_and_a_short_value(ws, load, store, caplog):
    with caplog.at_level(logging.INFO, logger=ws.__name__):
        value = '{"1f": "' + "x" * 200 + '"}'
        conn = await _update(ws, _hass(_entry(load)), "floor_plan_bg", value)
    assert conn.errors == []
    line = next(r.getMessage() for r in caplog.records if "Nova panel: set" in r.getMessage())
    assert line == "Nova panel: set floor_plan_bg = " + value[:80]


async def test_an_unsaved_write_is_a_warning(ws, load, store, caplog):
    store.ok = False
    with caplog.at_level(logging.WARNING, logger=ws.__name__):
        await _update(ws, _hass(_entry(load)), "banter_level", 1)
    assert any(r.levelno == logging.WARNING and "FAILED to persist" in r.getMessage()
               for r in caplog.records)


async def test_a_sleep_override_that_will_not_apply_still_saves(ws, load, store, monkeypatch,
                                                                 caplog):
    sd = load("sleep_detection")
    monkeypatch.setattr(sd, "set_override", _broken)
    monkeypatch.setattr(ws, "sleep_detection", sd)
    with caplog.at_level(logging.WARNING, logger=ws.__name__):
        conn = await _update(ws, _hass(_entry(load)), "sleep_override", "awake")
    assert conn.results[0][1] == {"key": "sleep_override", "value": "awake", "persisted": True}
    assert "sleep_override apply failed" in caplog.text


async def test_sleep_override_uses_the_default_quiet_end(ws, load, store, monkeypatch):
    sd = load("sleep_detection")
    seen = []
    monkeypatch.setattr(sd, "set_override", lambda v, q: seen.append(q))
    monkeypatch.setattr(ws, "sleep_detection", sd)
    await _update(ws, _hass(_entry(load)), "sleep_override", "auto")
    assert seen == ["07:00"]


async def test_a_tier_refresh_failure_does_not_fail_the_write(ws, load, store, monkeypatch):
    ob = load("observer")

    async def refresh(hass, updates):
        raise RuntimeError("down")
    monkeypatch.setattr(ob, "is_running", lambda: True)
    monkeypatch.setattr(ob, "refresh_tier_providers", refresh)
    conn = await _update(ws, _hass(_entry(load)), "reasoning_provider", "groq")
    assert conn.errors == [] and conn.results[0][1]["value"] == "groq"


async def test_observer_start_gets_a_snapshot_with_the_new_value(ws, load, store, monkeypatch):
    ob = load("observer")
    nc = load("nova_config")
    running = [False]
    seen = []

    async def start(hass, config, entry=None):
        running[0] = True
    monkeypatch.setattr(ob, "start", start)
    monkeypatch.setattr(ob, "is_running", lambda: running[0])
    monkeypatch.setattr(nc, "effective_config_with_runtime",
                        lambda entry, rc: seen.append(rc) or dict(rc))
    entry = _entry(load, runtime_config={"banter_level": 1})
    await _update(ws, _hass(entry), "observer_enabled", True)
    assert seen == [{"banter_level": 1, "observer_enabled": True}]
    assert seen[0] is not entry.runtime_data.runtime_config


async def test_clearing_one_coordinate_with_none_saved_deletes_both(ws, load, store):
    entry = _entry(load, runtime_config={"hazard_lat": 1.0, "hazard_lon": 2.0})
    conn = await _update(ws, _hass(entry), "hazard_lat", "")
    assert conn.results == [(1, {"key": "hazard_lat", "value": "", "persisted": True})]
    assert store.calls == [("delete", "hazard_lat"), ("delete", "hazard_lon")]
    assert entry.runtime_data.runtime_config == {}


async def test_clearing_a_coordinate_reports_a_failed_save(ws, load, store):
    # Fixed (bugs.md #17): a failed write is reported, not always "saved".
    store.ok = False
    entry = _entry(load, runtime_config={"hazard_lat": 1.0, "hazard_lon": 2.0})
    conn = await _update(ws, _hass(entry), "hazard_lat", "")
    assert conn.results == [(1, {"key": "hazard_lat", "value": "", "persisted": False})]


async def test_a_coordinate_pairs_with_the_saved_other_one(ws, load, store):
    entry = _entry(load, runtime_config={"hazard_lon": -8.0})
    conn = await _update(ws, _hass(entry), "hazard_lat", 52)
    assert conn.results == [(1, {"key": "hazard_lat", "value": 52, "persisted": True})]
    assert store.calls == [("set_many", {"hazard_lat": 52.0, "hazard_lon": -8.0})]
    assert entry.runtime_data.runtime_config == {"hazard_lat": 52.0, "hazard_lon": -8.0}


async def test_an_ai_value_key_refusal_uses_the_safe_message(ws, load, store):
    conn = await _update(ws, _hass(_entry(load)), "vision_provider", "nope")
    assert conn.errors == [(1, "invalid_value", "Unsupported provider for vision_provider")]


async def test_update_failure_is_a_warning(ws, load, store, monkeypatch, caplog):
    async def boom(key, value):
        raise RuntimeError("x")
    monkeypatch.setattr(load("cognitive_core"), "apply_runtime_config", boom)
    with caplog.at_level(logging.WARNING, logger=ws.__name__):
        conn = await _update(ws, _hass(_entry(load)), "face_stand_down", True)
    assert conn.errors[0][1] == "update_failed"
    assert "ws_update_config failed" in caplog.text


# ── The other commands ──────────────────────────────────────────────────────

async def test_reload_appliances_with_no_entry_starts_with_an_empty_config(ws, load, monkeypatch):
    started = []

    async def start(hass, cfg, entry=None):
        started.append((cfg, entry))
    monkeypatch.setattr(load("appliance_monitor"), "start", start)
    monkeypatch.setattr(ws, "_get_appliance_status", lambda: [])
    conn = await _call(ws.ws_reload_appliances, _hass(None))
    assert started == [({}, None)] and conn.results == [(3, {"ok": True, "appliances": []})]


async def test_reload_appliances_merges_a_runtime_snapshot(ws, load, monkeypatch):
    nc = load("nova_config")
    seen = []
    monkeypatch.setattr(nc, "effective_config_with_runtime",
                        lambda entry, rc: seen.append(rc) or {"merged": True})

    async def start(hass, cfg, entry=None):
        seen.append(cfg)
    monkeypatch.setattr(load("appliance_monitor"), "start", start)
    monkeypatch.setattr(ws, "_get_appliance_status", lambda: [])
    entry = _entry(load, runtime_config={"a": 1})
    await _call(ws.ws_reload_appliances, _hass(entry))
    assert seen == [{"a": 1}, {"merged": True}]
    assert seen[0] is not entry.runtime_data.runtime_config


async def test_debug_log_sends_a_copy_of_the_entries(ws, load, monkeypatch):
    log = [{"msg": "a"}]
    monkeypatch.setattr(ws, "_DEBUG_LOG", log)
    monkeypatch.setattr(ws, "_entity_names", lambda hass: {})
    conn = await _call(ws.ws_get_debug_log, _hass(_entry(load)))
    entries = conn.results[0][1]["entries"]
    assert entries == log and entries is not log


async def test_provider_activity_passes_the_database_path(ws, load, monkeypatch):
    pa = load("provider_activity")
    seen = []
    monkeypatch.setattr(pa, "db_path_for", lambda hass: "/tmp/x.db")
    monkeypatch.setattr(pa, "list_days", lambda days, db_path=None: seen.append(db_path) or [1])
    conn = await _call(ws.ws_get_provider_activity, _hass(_entry(load)))
    assert seen == ["/tmp/x.db"] and conn.results == [(3, {"days": [1]})]


@pytest.fixture
def logged(ws, monkeypatch):
    lines = []
    monkeypatch.setattr(ws, "nova_log", lambda cat, msg: lines.append((cat, msg)))
    return lines


async def test_semantic_messages(ws, load, store, logged, monkeypatch):
    e = load("embeddings")
    monkeypatch.setattr(e, "init_store", lambda: True)

    async def probe(hass):
        return {"ok": True, "model": "m", "dim": 3}
    monkeypatch.setattr(e, "probe", probe)
    await _call(ws.ws_semantic_search, _hass(_entry(load)), action="enable")
    await _call(ws.ws_semantic_search, _hass(_entry(load)), action="disable")
    assert logged == [("AGENT", "semantic search enabled (Ollama m, dim 3) — re-ingest to embed docs"),
                      ("AGENT", "semantic search disabled — keyword (FTS) active")]
    assert ("set", "semantic_search", True) in store.calls


async def test_semantic_status_reads_the_saved_switch(ws, load, store, monkeypatch):
    e = load("embeddings")
    monkeypatch.setattr(e, "_ollama_base", lambda: "http://10.0.0.2:11434")
    monkeypatch.setattr(e, "_model", lambda: "m")
    monkeypatch.setattr(e, "vector_count", lambda: 0)
    store.data["semantic_search"] = 1
    # Fixed (bugs.md #18): every status read happens off the event loop.
    hass, off_loop = _hass(_entry(load)), []
    orig_get = load("nova_config").get

    async def _executor(func, *args):
        off_loop.append(True)
        try:
            return func(*args)
        finally:
            off_loop.pop()
    hass.async_add_executor_job = _executor

    def _get(key, default=None):
        assert off_loop, "config read on the event loop"
        return orig_get(key, default)
    monkeypatch.setattr(load("nova_config"), "get", _get)
    conn = await _call(ws.ws_semantic_search, hass, action="status")
    assert conn.results == [(3, {"enabled": True, "ollama_configured": True,
                                 "base": "http://10.0.0.2:11434", "model": "m",
                                 "vector_count": 0})]


async def test_documents_log_only_what_happened(ws, load, logged, monkeypatch):
    d = load("documents")

    async def ingest(hass):
        return {"files_ingested": 1, "total_chunks": 2, "semantic": False}

    async def upload(hass, filename, content):
        return {"ok": False}

    async def scan(hass):
        return {"new_files": 0}
    monkeypatch.setattr(d, "ingest_directory_async", ingest)
    monkeypatch.setattr(d, "save_and_ingest_upload", upload)
    monkeypatch.setattr(d, "scan_watch_folders", scan)
    monkeypatch.setattr(d, "delete_source", lambda name: {"ok": name == "a.txt"})
    hass = _hass(_entry(load))
    await _call(ws.ws_documents, hass, action="ingest")
    await _call(ws.ws_documents, hass, action="upload")
    await _call(ws.ws_documents, hass, action="scan_watch")
    await _call(ws.ws_documents, hass, action="delete", filename="b.txt")
    conn = await _call(ws.ws_documents, hass, action="delete", filename="a.txt")
    assert conn.results == [(3, {"ok": True})]
    assert logged == [("AGENT", "documents ingested via panel: 1 files, 2 chunks"),
                      ("AGENT", "document removed: a.txt")]


async def test_document_search_with_no_query_asks_for_five(ws, load, monkeypatch):
    seen = []

    async def search(hass, query, k):
        seen.append((query, k))
        return []
    monkeypatch.setattr(load("documents"), "search_documents_async", search)
    conn = await _call(ws.ws_documents, _hass(_entry(load)), action="search")
    assert seen == [("", 5)] and conn.results == [(3, {"results": []})]


async def test_sparklines_include_a_humidity_only_area(ws, load, monkeypatch):
    monkeypatch.setattr(ws, "_all_areas_with_anything", lambda hass: ["bath"])
    monkeypatch.setattr(ws, "_area_temp_humidity_entities", lambda hass, a: (None, "sensor.h"))
    asked = []

    async def spark(hass, entity_map):
        asked.append(entity_map)
        return {}
    monkeypatch.setattr(ws, "_get_area_sparklines", spark)
    await _call(ws.ws_get_area_sparklines, _hass(_entry(load)))
    assert asked == [{"bath": {"temp": None, "humidity": "sensor.h"}}]
