"""The departure alert calendar exclusion: validated on write, shown in the panel data."""
from __future__ import annotations

import sys
import types

import pytest

from test_pin_ws_update_config import _entry, _hass, _stub_ws_api, _update

KEY = "departure_excluded_calendars"


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
    nc = load("nova_config")
    data: dict = {}
    calls = []
    monkeypatch.setattr(nc, "set", lambda k, v: calls.append((k, v)) or data.__setitem__(k, v) or True)
    monkeypatch.setattr(nc, "set_many", lambda values: calls.append(("set_many", dict(values)))
                        or data.update(values) or True)
    monkeypatch.setattr(nc, "delete", lambda k: calls.append(("delete", k)) or data.pop(k, None))
    monkeypatch.setattr(nc, "get", lambda k, d=None: data.get(k, d))
    return types.SimpleNamespace(data=data, calls=calls)


@pytest.mark.parametrize("value", [
    "[]", '["calendar.family"]', '["calendar.family", "calendar.birthdays_2"]',
])
async def test_a_calendar_list_saves(ws, load, store, value):
    conn = await _update(ws, _hass(_entry(load)), KEY, value)
    assert conn.errors == [] and store.data[KEY] == value


@pytest.mark.parametrize("value", [
    '["light.kitchen"]', '["calendar."]', '["Calendar.Family"]', '["calendar.a b"]',
    '[1]', "calendar.family", "{}", "", None, True,
])
async def test_only_calendar_entity_ids_are_accepted(ws, load, store, value):
    conn = await _update(ws, _hass(_entry(load)), KEY, value)
    assert conn.errors == [(1, "invalid_value", f"Key '{KEY}' must be a list of calendar entity ids")]
    assert store.calls == []
