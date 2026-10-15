"""Presenting (8.51.0): an energy card beside the hologram.

The card is chosen from the tools a turn really ran, never from the model's
text, and its figures come from Nova's own energy readings."""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass, FakeUserInput


@pytest.fixture
def pres(load):
    return load("presentation")


STATUS = {"configured": True, "solar": {"w": 3120.4}, "battery": {"pct": 64.2}}
TODAY = {"configured": True, "solar_kwh": 14.26, "grid_export_kwh": 5.0, "home_kwh": 9.84}


@pytest.fixture
def energy(load, monkeypatch):
    ef = load("energy_flow")
    box = {"status": STATUS, "today": TODAY}

    async def status(hass):
        return box["status"]

    async def today(hass):
        return box["today"]
    monkeypatch.setattr(ef, "energy_flow_status", status)
    monkeypatch.setattr(ef, "energy_flow_today", today)
    return box


@pytest.fixture
def runtime(load, monkeypatch):
    pres = load("presentation")
    rt = types.SimpleNamespace(presentation=pres.PresentationStore())
    monkeypatch.setattr(load("runtime"), "domain_runtime", lambda hass: rt)
    return rt


# ── the card ───────────────────────────────────────────────────────────────

async def test_an_energy_tool_turn_builds_a_card_from_energy_flow(pres, energy, runtime):
    card = await pres.present_turn(FakeHass(), {"solar_status"})
    assert card["v"] == 1 and card["kind"] == "energy" and card["id"]
    assert card["expires"] - card["created"] == 30
    assert card["data"] == {"solar_today_kwh": 14.3, "battery_pct": 64.0, "export_kwh": 5.0,
                            "home_kwh": 9.8, "solar_w": 3120.0, "curve": []}
    assert runtime.presentation.latest is card


@pytest.mark.parametrize("tool", ["energy_status", "solar_status", "energy_report", "energy_outlook"])
async def test_each_energy_tool_counts(pres, energy, runtime, tool):
    assert await pres.present_turn(FakeHass(), {"web_search", tool}) is not None


async def test_a_turn_with_no_energy_tool_makes_no_card(pres, energy, runtime):
    assert await pres.present_turn(FakeHass(), {"control_device", "get_weather"}) is None
    assert await pres.present_turn(FakeHass(), set()) is None
    assert runtime.presentation.latest is None


async def test_unreadable_figures_are_null(pres, energy, runtime):
    energy["status"] = {"solar": {"w": None}, "battery": {"pct": None}}
    energy["today"] = {"solar_kwh": 3.0, "grid_export_kwh": None, "home_kwh": None}
    data = (await pres.present_turn(FakeHass(), {"energy_status"}))["data"]
    assert data["solar_today_kwh"] == 3.0
    assert data["battery_pct"] is None and data["export_kwh"] is None
    assert data["home_kwh"] is None and data["solar_w"] is None


async def test_nothing_readable_means_no_card(pres, energy, runtime):
    energy["status"] = {"configured": False, "solar": {"w": None}, "battery": {"pct": None},
                        "error": True}
    energy["today"] = {"configured": False, "solar_kwh": None, "grid_export_kwh": None,
                       "home_kwh": None}
    assert await pres.present_turn(FakeHass(), {"solar_status"}) is None
    assert runtime.presentation.latest is None


async def test_the_card_never_carries_model_text(pres, energy, runtime):
    card = await pres.present_turn(FakeHass(), {"solar_status"})
    assert set(card) == {"v", "kind", "id", "created", "expires", "data"}
    assert all(not isinstance(v, str) for v in card["data"].values())


# ── the store ──────────────────────────────────────────────────────────────

def test_the_store_serves_only_an_unexpired_card(pres):
    store = pres.PresentationStore()
    assert store.current() is None
    store.publish({"id": "a", "expires": 130.0})
    assert store.current(now=100.0)["id"] == "a"
    assert store.current(now=130.0) is None


def test_the_store_tells_listeners_until_they_stop(pres):
    store, seen = pres.PresentationStore(), []
    stop = store.subscribe(seen.append)
    store.publish({"id": "a", "expires": 1e12})
    stop()
    store.publish({"id": "b", "expires": 1e12})
    assert [c["id"] for c in seen] == ["a"]


def test_a_failing_listener_does_not_block_the_others(pres):
    store, seen = pres.PresentationStore(), []

    def bad(card):
        raise RuntimeError("x")
    store.subscribe(bad)
    store.subscribe(seen.append)
    store.publish({"id": "a", "expires": 1e12})
    assert len(seen) == 1


# ── the websocket subscription ─────────────────────────────────────────────

class _Conn:
    def __init__(self):
        self.user = types.SimpleNamespace(id="u", name="n")
        self.results, self.errors, self.events, self.subscriptions = [], [], [], {}

    def send_result(self, msg_id, result=None):
        self.results.append((msg_id, result))

    def send_error(self, msg_id, code, message=""):
        self.errors.append((msg_id, code, message))

    def send_message(self, message):
        self.events.append(message)


@pytest.fixture
def ws(load, monkeypatch):
    api = types.ModuleType("homeassistant.components.websocket_api")
    api.websocket_command = lambda schema: (lambda fn: fn)
    api.async_response = lambda fn: fn
    api.require_admin = lambda fn: fn
    api.ActiveConnection = _Conn
    api.event_message = lambda msg_id, event: {"id": msg_id, "type": "event", "event": event}
    monkeypatch.setitem(sys.modules, "homeassistant.components.websocket_api", api)
    monkeypatch.setattr(sys.modules["homeassistant.components"], "websocket_api", api,
                        raising=False)
    sys.modules.pop("jc.ws_modes", None)
    mod = load("ws_modes")
    yield mod
    sys.modules.pop("jc.ws_modes", None)


async def test_subscribe_sends_the_latest_unexpired_card_then_new_ones(ws, pres, runtime):
    import time
    runtime.presentation.publish({"id": "old", "expires": time.time() + 20})
    conn = _Conn()
    await ws.ws_subscribe_presentation(FakeHass(), conn, {"id": 7})
    assert conn.results == [(7, None)] and conn.errors == []
    assert [e["event"]["id"] for e in conn.events] == ["old"]
    runtime.presentation.publish({"id": "new", "expires": time.time() + 30})
    assert [e["event"]["id"] for e in conn.events] == ["old", "new"]
    assert all(e["id"] == 7 for e in conn.events)


async def test_subscribe_skips_an_expired_card(ws, pres, runtime):
    runtime.presentation.publish({"id": "stale", "expires": 1.0})
    conn = _Conn()
    await ws.ws_subscribe_presentation(FakeHass(), conn, {"id": 8})
    assert conn.events == []


async def test_nothing_is_sent_after_unsubscribe(ws, pres, runtime):
    import time
    conn = _Conn()
    await ws.ws_subscribe_presentation(FakeHass(), conn, {"id": 9})
    conn.subscriptions.pop(9)()          # what Home Assistant's unsubscribe does
    runtime.presentation.publish({"id": "late", "expires": time.time() + 30})
    assert conn.events == []


async def test_subscribe_without_a_loaded_nova_is_an_error(ws, load, monkeypatch):
    monkeypatch.setattr(load("runtime"), "domain_runtime", lambda hass: None)
    conn = _Conn()
    await ws.ws_subscribe_presentation(FakeHass(), conn, {"id": 10})
    assert conn.results == [] and conn.errors[0][:2] == (10, "not_loaded")


# ── the agent loop hook ────────────────────────────────────────────────────

class _Client:
    name = "fake"
    model = "m"

    def __init__(self, script):
        self.script = list(script)

    def chat(self, messages, tools=None, max_tokens=512, temperature=0.7):
        return self.script.pop(0) if self.script else {"text": "done", "tool_calls": []}


async def _turn(load, monkeypatch, script, run_tool=None):
    loop = load("agent_runtime.loop")
    monkeypatch.setattr(load("agent"), "_load_learned", lambda: {"alias": {}})
    client = _Client(script)

    async def primary(*a, **k):
        return client
    monkeypatch.setattr(loop, "_create_provider_with_fallback", primary)
    if run_tool is not None:
        async def execute(hass, call, ctx):
            return run_tool(call)
        monkeypatch.setattr(loop._dispatcher, "execute", execute)
    hass = FakeHass()
    reply = await loop.run_agent(
        hass, messages=[{"role": "user", "content": "how is the solar"}], persona="P",
        provider_name="fake", api_key="", model="m",
        user_input=FakeUserInput("how is the solar"), config={})
    hass.close_pending()
    return reply


def _tc(name):
    return {"id": "c1", "name": name, "args": {}}


async def test_a_turn_that_ran_solar_status_publishes_an_energy_card(load, energy, runtime, monkeypatch):
    models = load("agent_runtime.models")
    reply = await _turn(
        load, monkeypatch,
        [{"text": "", "tool_calls": [_tc("solar_status")]},
         {"text": "Plenty of sun, sir.", "tool_calls": []}],
        run_tool=lambda call: models.ToolResult('{"ok": true}'))
    assert reply == "Plenty of sun, sir."
    card = runtime.presentation.latest
    assert card["kind"] == "energy" and card["data"]["solar_w"] == 3120.0
    assert "Plenty" not in str(card)


async def test_a_turn_with_only_other_tools_publishes_nothing(load, energy, runtime, monkeypatch):
    models = load("agent_runtime.models")
    await _turn(
        load, monkeypatch,
        [{"text": "", "tool_calls": [_tc("get_weather")]},
         {"text": "Sunny.", "tool_calls": []}],
        run_tool=lambda call: models.ToolResult('{"ok": true}'))
    assert runtime.presentation.latest is None


async def test_the_model_saying_energy_words_does_not_make_a_card(load, energy, runtime, monkeypatch):
    await _turn(load, monkeypatch,
                [{"text": "Solar is making 3 kW and the battery is at 64%.", "tool_calls": []}])
    assert runtime.presentation.latest is None


async def test_a_refused_energy_tool_does_not_make_a_card(load, energy, runtime, monkeypatch):
    models = load("agent_runtime.models")
    await _turn(
        load, monkeypatch,
        [{"text": "", "tool_calls": [_tc("energy_status")]},
         {"text": "I could not read it.", "tool_calls": []}],
        run_tool=lambda call: models.ToolResult.error("tool_error", "boom"))
    assert runtime.presentation.latest is None


# ── the rotation setting ───────────────────────────────────────────────────

def test_the_card_rotation_is_on_by_default_and_reaches_the_panel(load):
    schema = load("settings_schema")
    assert schema.default("hero_card_cycle") is True
    payload = schema.payload(lambda key, default: default, lambda key, default: default,
                             lambda v: v)
    assert payload["hero_card_cycle"] is True


def test_the_card_rotation_can_be_turned_off(load):
    schema = load("settings_schema")
    payload = schema.payload(lambda key, default: False if key == "hero_card_cycle" else default,
                             lambda key, default: default, lambda v: v)
    assert payload["hero_card_cycle"] is False
