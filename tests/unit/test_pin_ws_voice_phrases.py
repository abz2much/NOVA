"""Pin the Voice Phrases card's commands in ws_voice_phrases.py (8.32.0).

nova/voice_phrases_list, _save, _delete, _set_enabled and _test. The handlers
run against the real voice_phrases module and a dict standing in for
config.json, with the decorators stubbed to pass through and a recording
connection.
"""
from __future__ import annotations

import sys

import pytest

from fakes import FakeHass
from test_pin_ws_update_config import _Conn, _stub_ws_api, store  # noqa: F401


@pytest.fixture
def wsp(load, monkeypatch):
    _stub_ws_api(monkeypatch)
    sys.modules.pop("jc.ws_voice_phrases", None)
    mod = load("ws_voice_phrases")
    yield mod
    sys.modules.pop("jc.ws_voice_phrases", None)


@pytest.fixture
def hass():
    h = FakeHass()
    h.services.register("script", "turn_on")
    h.services.register("shell_command", "heat_boost")
    h.states.set("script.heating_boost", "off")
    return h


async def _call(handler, hass, **msg):
    conn = _Conn()
    await handler(hass, conn, {"id": 2, **msg})
    return conn


def _new(**over):
    e = {"name": "House is cold", "phrases": ["the house is cold"],
         "match": "flexible", "allow_ai": False, "enabled": True,
         "action": {"service": "script.turn_on",
                    "target": {"entity_id": "script.heating_boost"}, "data": {}},
         "reply": ""}
    e.update(over)
    return e


async def test_list_when_nothing_is_saved(wsp, hass, store):
    conn = await _call(wsp.ws_voice_phrases_list, hass)
    (msg_id, res), = conn.results
    assert msg_id == 2 and res["enabled"] is True and res["entries"] == []
    assert "shell_command" in res["allowed_domains"] and "script" in res["allowed_domains"]
    assert "homeassistant" not in res["allowed_domains"]
    assert res["allowed_domains"] == sorted(res["allowed_domains"])
    assert res["asks_first"] == []


async def test_list_says_which_entries_ask_first(wsp, hass, store, load, monkeypatch):
    vc = load("voice_confirm")
    monkeypatch.setattr(vc, "is_enabled", lambda h: True)
    monkeypatch.setattr(vc, "_cfg", lambda h, k, d=None: (
        ["shell_command.heat_boost", "!script.heating_boost"]
        if k == "voice_confirm_entities" else d))
    script = (await _call(wsp.ws_voice_phrases_save, hass, entry=_new())).results[0][1]["entry"]
    hass.states.set("script.other", "off")
    other = (await _call(wsp.ws_voice_phrases_save, hass, entry=_new(
        name="Other", phrases=["other words"],
        action={"service": "script.turn_on", "target": {"entity_id": "script.other"}}))
    ).results[0][1]["entry"]
    shell = (await _call(wsp.ws_voice_phrases_save, hass, entry=_new(
        name="Shell", phrases=["boost it"], action={"service": "shell_command.heat_boost"}))
    ).results[0][1]["entry"]
    store.data["voice_phrases"]["entries"].append({"phrase_id": "vp_bad", "action": 3})
    res = (await _call(wsp.ws_voice_phrases_list, hass)).results[0][1]
    # The exempt script runs; the other script and the listed shell command ask.
    assert res["asks_first"] == [other["phrase_id"], shell["phrase_id"]]
    assert script["phrase_id"] not in res["asks_first"]


async def test_save_adds_then_updates(wsp, hass, store):
    conn = await _call(wsp.ws_voice_phrases_save, hass, entry=_new())
    (_, res), = conn.results
    pid = res["entry"]["phrase_id"]
    assert res["warnings"] == [] and pid.startswith("vp_")
    assert store.data["voice_phrases"] == {"enabled": True, "entries": [res["entry"]]}
    conn = await _call(wsp.ws_voice_phrases_save, hass,
                       entry=_new(phrase_id=pid, name="Cold house"))
    assert conn.results[0][1]["entry"]["phrase_id"] == pid
    assert [e["name"] for e in store.data["voice_phrases"]["entries"]] == ["Cold house"]


async def test_save_builds_a_new_object(wsp, hass, store):
    await _call(wsp.ws_voice_phrases_save, hass, entry=_new())
    first = store.data["voice_phrases"]
    await _call(wsp.ws_voice_phrases_save, hass, entry=_new(name="Two", phrases=["two words"]))
    assert len(first["entries"]) == 1 and store.data["voice_phrases"] is not first


async def test_save_lists_every_problem(wsp, hass, store):
    conn = await _call(wsp.ws_voice_phrases_save, hass,
                       entry=_new(name="", match="fuzzy", phrases=[]))
    (msg_id, code, text), = conn.errors
    assert code == "invalid_value"
    assert "The name must be 1 to 60 characters." in text
    assert "Match must be flexible or exact." in text
    assert "Add 1 to 10 phrases." in text
    assert store.calls == []


async def test_save_refuses_a_service_home_assistant_lacks(wsp, hass, store):
    conn = await _call(wsp.ws_voice_phrases_save, hass,
                       entry=_new(action={"service": "light.turn_on"}))
    assert conn.errors == [(2, "invalid_value", "Home Assistant has no light.turn_on service.")]
    assert store.calls == []


async def test_save_refuses_a_domain_outside_the_list(wsp, hass, store):
    hass.services.register("homeassistant", "restart")
    conn = await _call(wsp.ws_voice_phrases_save, hass,
                       entry=_new(action={"service": "homeassistant.restart"}))
    assert conn.errors[0][1] == "invalid_value" and store.calls == []


async def test_save_warns_about_an_unknown_entity(wsp, hass, store):
    conn = await _call(wsp.ws_voice_phrases_save, hass, entry=_new(
        action={"service": "script.turn_on", "target": {"entity_id": "script.missing"}}))
    assert conn.results[0][1]["warnings"] == ["script.missing was not found in Home Assistant."]
    assert len(store.data["voice_phrases"]["entries"]) == 1


async def test_save_warns_when_the_disk_write_fails(wsp, hass, store):
    store.ok = False
    conn = await _call(wsp.ws_voice_phrases_save, hass, entry=_new())
    assert conn.results[0][1]["warnings"] == [
        "Saved for now, but it could not be written to disk."]


async def test_a_shell_command_is_saved_without_ai(wsp, hass, store):
    conn = await _call(wsp.ws_voice_phrases_save, hass, entry=_new(
        allow_ai=True, action={"service": "shell_command.heat_boost"}))
    assert conn.results[0][1]["entry"]["allow_ai"] is False


async def test_delete(wsp, hass, store):
    conn = await _call(wsp.ws_voice_phrases_save, hass, entry=_new())
    pid = conn.results[0][1]["entry"]["phrase_id"]
    conn = await _call(wsp.ws_voice_phrases_delete, hass, phrase_id=pid)
    assert conn.results == [(2, {"ok": True})]
    assert store.data["voice_phrases"]["entries"] == []
    conn = await _call(wsp.ws_voice_phrases_delete, hass, phrase_id=pid)
    assert conn.results == [(2, {"ok": False})]


async def test_set_enabled_keeps_the_entries(wsp, hass, store):
    await _call(wsp.ws_voice_phrases_save, hass, entry=_new())
    conn = await _call(wsp.ws_voice_phrases_set_enabled, hass, enabled=False)
    assert conn.results == [(2, {"enabled": False})]
    assert store.data["voice_phrases"]["enabled"] is False
    assert len(store.data["voice_phrases"]["entries"]) == 1


async def test_test_matches_and_never_runs(wsp, hass, store):
    conn = await _call(wsp.ws_voice_phrases_save, hass, entry=_new())
    pid = conn.results[0][1]["entry"]["phrase_id"]
    conn = await _call(wsp.ws_voice_phrases_test, hass, text="Nova, the house is a bit cold")
    assert conn.results == [(2, {"match": {"phrase_id": pid, "name": "House is cold",
                                           "phrase": "the house is cold",
                                           "tier": "flexible"}})]
    conn = await _call(wsp.ws_voice_phrases_test, hass, text="the house is not cold")
    assert conn.results == [(2, {"match": None})]
    assert hass.service_calls == []


async def test_test_follows_the_switch(wsp, hass, store):
    await _call(wsp.ws_voice_phrases_save, hass, entry=_new())
    await _call(wsp.ws_voice_phrases_set_enabled, hass, enabled=False)
    conn = await _call(wsp.ws_voice_phrases_test, hass, text="the house is cold")
    assert conn.results == [(2, {"match": None})]


async def test_a_damaged_saved_value_reads_as_empty(wsp, hass, store):
    store.data["voice_phrases"] = "junk"
    conn = await _call(wsp.ws_voice_phrases_list, hass)
    assert conn.results[0][1]["entries"] == [] and conn.results[0][1]["enabled"] is True
    store.data["voice_phrases"] = {"enabled": False, "entries": [1, {"phrase_id": "x"}]}
    conn = await _call(wsp.ws_voice_phrases_list, hass)
    assert conn.results[0][1]["entries"] == [{"phrase_id": "x"}]
    assert conn.results[0][1]["enabled"] is False


@pytest.mark.parametrize("handler,msg", [
    ("ws_voice_phrases_list", {}),
    ("ws_voice_phrases_save", {"entry": {}}),
    ("ws_voice_phrases_delete", {"phrase_id": "vp_00000001"}),
    ("ws_voice_phrases_set_enabled", {"enabled": True}),
    ("ws_voice_phrases_test", {"text": "x"}),
])
async def test_an_unexpected_failure_returns_no_raw_text(wsp, hass, load, monkeypatch,
                                                         handler, msg):
    nc = load("nova_config")

    def boom(*a, **k):
        raise RuntimeError("token=abc")
    monkeypatch.setattr(nc, "get", boom)
    conn = await _call(getattr(wsp, handler), hass, **msg)
    assert conn.errors == [(2, "voice_phrases_failed",
                            "RuntimeError (details are in the Home Assistant log)")]


def test_voice_phrases_is_not_panel_writable():
    import contract_extract as ce
    keys = ce.panel_writable_keys()
    assert keys and "voice_phrases" not in keys
