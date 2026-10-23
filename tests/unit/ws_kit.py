"""Shared helpers for the websocket command tests: a recording connection,
a websocket_api stand in whose decorators pass through, a fake config entry
and hass, and config.json as a dict."""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass


class _Conn:
    """A websocket connection that records what the handler sends back."""

    def __init__(self):
        self.user = types.SimpleNamespace(id="admin-1", name="Abi")
        self.results, self.errors = [], []

    def send_result(self, msg_id, result=None):
        self.results.append((msg_id, result))

    def send_error(self, msg_id, code, message=""):
        self.errors.append((msg_id, code, message))


def _stub_ws_api(monkeypatch):
    api = types.ModuleType("homeassistant.components.websocket_api")
    api.websocket_command = lambda schema: (lambda fn: fn)
    api.async_response = lambda fn: fn
    api.require_admin = lambda fn: fn
    api.ActiveConnection = _Conn
    api.async_register_command = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "homeassistant.components.websocket_api", api)
    monkeypatch.setattr(sys.modules["homeassistant.components"], "websocket_api", api,
                        raising=False)


@pytest.fixture
def store(load, monkeypatch):
    """config.json, as a dict. `store.ok` is what nova_config.set returns."""
    nc = load("nova_config")
    data: dict = {}
    state = types.SimpleNamespace(data=data, ok=True, calls=[])

    def _set(key, value):
        state.calls.append((key, value))
        data[key] = value
        return state.ok
    monkeypatch.setattr(nc, "set", _set)
    monkeypatch.setattr(nc, "get", lambda k, d=None: data.get(k, d))
    return state


def _entry(load, runtime_config=None, with_runtime=True):
    rt_mod = load("runtime")
    runtime = None
    if with_runtime:
        runtime = rt_mod.NovaRuntime(
            client=None, llm_provider_name="groq", sentinel=None,
            reminder_watcher=None, scheduler=None, resources=None,
            automation_contexts=None,
            runtime_config=dict(runtime_config or {}))
    return types.SimpleNamespace(entry_id="e1", options={}, data={},
                                 runtime_data=runtime, state=None)


def _hass(entry):
    hass = FakeHass()
    hass.config_entries = types.SimpleNamespace(
        async_entries=lambda domain: [entry] if entry is not None else [])
    return hass


async def _update(ws, hass, key, value, msg_id=1):
    conn = _Conn()
    await ws.ws_update_config(hass, conn, {"id": msg_id, "key": key, "value": value})
    return conn
