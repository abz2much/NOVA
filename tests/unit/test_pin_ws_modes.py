"""Pin what the panel's mode and intrusion commands do today (8.7.19, tests only).

ws_modes.py holds nova/intrusion (call off, acknowledge, label, the event log)
and nova/mode, plus nova/energy, nova/hazard, nova/solar, nova/energy_flow and
nova/biometrics.
Its admin gates and schemas are pinned by the websocket contract tests, which
read the real decorators. Here the decorators are stubbed to pass through, so
the handler bodies run against the real intrusion and modes modules with a
recording connection. Tests named test_current_behaviour_* pin behaviour that
looks wrong; each says why.
"""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass


class _Conn:
    """A websocket connection that records what the handler sends back."""

    def __init__(self, user_id="admin-1", name="Abi"):
        self.user = types.SimpleNamespace(id=user_id, name=name)
        self.results, self.errors = [], []

    def send_result(self, msg_id, result=None):
        self.results.append((msg_id, result))

    def send_error(self, msg_id, code, message=""):
        self.errors.append((msg_id, code, message))


@pytest.fixture
def ws(load, monkeypatch):
    api = types.ModuleType("homeassistant.components.websocket_api")
    api.websocket_command = lambda schema: (lambda fn: fn)
    api.async_response = lambda fn: fn
    api.require_admin = lambda fn: fn
    api.ActiveConnection = _Conn
    monkeypatch.setitem(sys.modules, "homeassistant.components.websocket_api", api)
    monkeypatch.setattr(sys.modules["homeassistant.components"], "websocket_api", api,
                        raising=False)
    sys.modules.pop("jc.ws_modes", None)
    mod = load("ws_modes")
    logged = []
    monkeypatch.setattr(mod, "nova_log", lambda cat, msg: logged.append((cat, msg)))
    mod._logged = logged
    yield mod
    sys.modules.pop("jc.ws_modes", None)


@pytest.fixture
def intrusion(load, tmp_path, monkeypatch):
    mod = load("intrusion")
    monkeypatch.setattr(mod, "LOG_PATH", tmp_path / "intrusion_log.json")
    monkeypatch.setattr(mod, "_log", [], raising=False)
    monkeypatch.setattr(mod, "_log_loaded", True, raising=False)
    mod.clear_calloff()
    yield mod
    mod.clear_calloff()


@pytest.fixture
def investigation(load):
    cc = load("cognitive_core")
    saved = cc._CORE.safety_mgr
    cc._CORE.safety_mgr = types.SimpleNamespace(_investigation={"start": 1.0})
    yield cc._CORE.safety_mgr
    cc._CORE.safety_mgr = saved


@pytest.fixture
def modes(load, monkeypatch, tmp_path):
    m = load("modes")
    monkeypatch.setattr(m, "MODE_STATE_PATH", str(tmp_path / "mode_state.json"))
    monkeypatch.setattr(m, "_state", {"mode": "normal", "since": 0.0, "reason": ""})
    monkeypatch.setattr(m, "_loaded", True)
    return m


@pytest.fixture
def scenes(load, monkeypatch):
    entered = []

    async def apply_mode_entry(hass, mode, **kw):
        entered.append((mode, kw))
    monkeypatch.setattr(load("mode_scene"), "apply_mode_entry", apply_mode_entry)
    return entered


# ── nova/intrusion ──────────────────────────────────────────────────────────

async def test_current_behaviour_a_panel_call_off_needs_no_confirmation(ws, intrusion, investigation):
    # Looks wrong, or at least inconsistent: the agent's dismiss_intrusion
    # always goes through a confirmation (a phone tap when asked by voice),
    # but the panel's call off stands the response down on one admin click,
    # with no second step and no Action Audit Log row.
    conn = _Conn()
    await ws.ws_intrusion(FakeHass(), conn, {"id": 1, "action": "dismiss", "reason": "cat"})
    assert intrusion.is_called_off() is True
    assert investigation._investigation is None
    (msg_id, res), = conn.results
    assert res["ok"] is True and res["called_off"] is True and res["recorded"]["reason"] == "cat"
    assert ("SAFETY", "Intrusion called off from panel (false alarm)") in ws._logged


async def test_acknowledge_holds_without_calling_off(ws, intrusion, investigation):
    conn = _Conn()
    await ws.ws_intrusion(FakeHass(), conn, {"id": 2, "action": "acknowledge"})
    res = conn.results[0][1]
    assert res["acknowledged"] is True and res["called_off"] is False
    assert intrusion.is_called_off() is False
    assert investigation._investigation is not None


async def test_status_is_a_read_and_changes_nothing(ws, intrusion, investigation):
    conn = _Conn()
    await ws.ws_intrusion(FakeHass(), conn, {"id": 3, "action": "status"})
    res = conn.results[0][1]
    assert res["called_off"] is False and res["acknowledged"] is False
    assert investigation._investigation is not None and ws._logged == []


async def test_labelling_feeds_learning_and_the_log_inlines_snapshots(
        ws, intrusion, tmp_path, monkeypatch):
    snap = tmp_path / "snap.jpg"
    snap.write_bytes(b"\xff\xd8jpeg")
    intrusion.record_event("confirmed", reason="route", breach_area="hall",
                           snapshot={"path": str(snap), "camera": "camera.hall"})
    event_id = intrusion.get_log(1)[0]["id"]

    async def b64(hass, path):
        return "B64:" + path
    monkeypatch.setattr(intrusion, "get_snapshot_b64", b64)

    conn = _Conn()
    await ws.ws_intrusion(FakeHass(), conn, {"id": 4, "action": "label", "event_id": event_id,
                                             "label": "false"})
    assert conn.results[0][1]["ok"] is True
    await ws.ws_intrusion(FakeHass(), conn, {"id": 5, "action": "log", "limit": 5})
    log = conn.results[1][1]
    assert log["events"][0]["label"] == "false"
    assert log["events"][0]["image_b64"] == "B64:" + str(snap)
    assert "learning" in log


async def test_an_error_becomes_an_intrusion_failed_reply(ws, intrusion, monkeypatch):
    monkeypatch.setattr(intrusion, "dismiss_intrusion",
                        lambda reason="": (_ for _ in ()).throw(RuntimeError("boom")))
    conn = _Conn()
    await ws.ws_intrusion(FakeHass(), conn, {"id": 6, "action": "dismiss"})
    assert conn.results == [] and conn.errors[0][:2] == (6, "intrusion_failed")


# ── nova/mode ───────────────────────────────────────────────────────────────

async def test_mode_set_switches_and_applies_the_scene_as_the_panel_user(ws, modes, scenes):
    conn = _Conn(user_id="admin-9", name="Sam")
    await ws.ws_mode(FakeHass(), conn, {"id": 7, "action": "set", "mode": "movie",
                                        "reason": "film night"})
    res = conn.results[0][1]
    assert res["ok"] is True and res["active"] == "movie" and modes.active_mode() == "movie"
    assert scenes == [("movie", {"source": "panel", "requested_by_user_id": "admin-9",
                                 "requested_by_name": "Sam"})]
    assert ("MODE", "mode → movie (panel)") in ws._logged


async def test_an_unknown_mode_is_reported_and_nothing_changes(ws, modes, scenes):
    conn = _Conn()
    await ws.ws_mode(FakeHass(), conn, {"id": 8, "action": "set", "mode": "rave"})
    res = conn.results[0][1]
    assert res["ok"] is False and res["active"] == "normal"
    assert modes.active_mode() == "normal" and scenes == [] and ws._logged == []


async def test_a_failing_mode_scene_does_not_undo_the_mode(ws, modes, load, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("scene missing")
    monkeypatch.setattr(load("mode_scene"), "apply_mode_entry", boom)
    conn = _Conn()
    await ws.ws_mode(FakeHass(), conn, {"id": 9, "action": "set", "mode": "away"})
    assert conn.results[0][1]["ok"] is True and modes.active_mode() == "away"


# ── nova/energy, nova/hazard, nova/biometrics ───────────────────────────────

async def test_energy_rejects_an_unknown_agency_and_keeps_the_old_one(ws, load, monkeypatch):
    nc = load("nova_config")
    written = {}
    monkeypatch.setattr(nc, "set", lambda k, v: written.__setitem__(k, v))
    energy = load("energy")
    monkeypatch.setattr(energy, "power_status", lambda hass: {"agency": written.get("energy_agency")})
    conn = _Conn()
    await ws.ws_energy(FakeHass(), conn, {"id": 10, "action": "set_agency", "agency": "reckless"})
    assert conn.errors[0][:2] == (10, "bad_agency") and written == {}
    await ws.ws_energy(FakeHass(), conn, {"id": 11, "action": "set_agency",
                                          "agency": energy.AGENCY_AUTONOMOUS.upper()})
    assert written == {"energy_agency": energy.AGENCY_AUTONOMOUS}
    assert conn.results[-1] == (11, {"agency": energy.AGENCY_AUTONOMOUS})


async def test_hazard_scan_and_status_go_to_different_reads(ws, load, monkeypatch):
    hm = load("hazard_monitor")

    async def scan_now(hass):
        return {"kind": "scan"}

    async def status(hass):
        return {"kind": "status"}
    monkeypatch.setattr(hm, "scan_now", scan_now)
    monkeypatch.setattr(hm, "status", status)
    conn = _Conn()
    await ws.ws_hazard(FakeHass(), conn, {"id": 12, "action": "scan"})
    await ws.ws_hazard(FakeHass(), conn, {"id": 13, "action": "status"})
    assert conn.results == [(12, {"kind": "scan"}), (13, {"kind": "status"})]


async def test_biometrics_toggle_writes_the_setting_and_flattens_what_it_found(ws, load, monkeypatch):
    nc = load("nova_config")
    store = {}
    monkeypatch.setattr(nc, "set", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(nc, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(load("biometrics"), "discover",
                        lambda hass: {"heart_rate": [{"entity_id": "sensor.hr"}]})
    conn = _Conn()
    await ws.ws_biometrics(FakeHass(), conn, {"id": 14, "action": "enable"})
    assert store == {"biometrics_enabled": True}
    assert conn.results[0][1] == {"enabled": True, "found": 1, "entities": [
        {"kind": "heart_rate", "entity_id": "sensor.hr"}]}
    await ws.ws_biometrics(FakeHass(), conn, {"id": 15, "action": "disable"})
    assert store == {"biometrics_enabled": False} and conn.results[1][1]["enabled"] is False


# ── nova/energy_flow ───────────────────────────────────────────────────────

async def test_energy_flow_status_sends_the_readout(ws, load, monkeypatch):
    ef = load("energy_flow")
    readout = dict(ef.empty_status(), configured=True, solar={"w": 3200})

    async def status(hass):
        return readout
    monkeypatch.setattr(ef, "energy_flow_status", status)
    conn = _Conn()
    await ws.ws_energy_flow(FakeHass(), conn, {"id": 16, "action": "status"})
    assert conn.results == [(16, readout)] and conn.errors == []


async def test_energy_flow_failure_sends_energy_flow_failed(ws, load, monkeypatch):
    ef = load("energy_flow")

    async def status(hass):
        raise RuntimeError("boom")
    monkeypatch.setattr(ef, "energy_flow_status", status)
    conn = _Conn()
    await ws.ws_energy_flow(FakeHass(), conn, {"id": 17, "action": "status"})
    assert conn.results == [] and conn.errors[0][:2] == (17, "energy_flow_failed")


async def test_energy_flow_today_action_sends_the_totals(ws, load, monkeypatch):
    ef = load("energy_flow")
    totals = dict(ef.empty_today(), configured=True, solar_kwh=12.0)

    async def today(hass):
        return totals

    async def status(hass):
        raise AssertionError("today must not read the live status")
    monkeypatch.setattr(ef, "energy_flow_today", today)
    monkeypatch.setattr(ef, "energy_flow_status", status)
    conn = _Conn()
    await ws.ws_energy_flow(FakeHass(), conn, {"id": 18, "action": "today"})
    assert conn.results == [(18, totals)] and conn.errors == []


async def test_energy_flow_today_failure_sends_energy_flow_failed(ws, load, monkeypatch):
    ef = load("energy_flow")

    async def today(hass):
        raise RuntimeError("boom")
    monkeypatch.setattr(ef, "energy_flow_today", today)
    conn = _Conn()
    await ws.ws_energy_flow(FakeHass(), conn, {"id": 19, "action": "today"})
    assert conn.results == [] and conn.errors[0][:2] == (19, "energy_flow_failed")
