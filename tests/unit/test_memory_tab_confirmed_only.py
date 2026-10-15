"""After adding or forgetting a fact, the Memory list holds confirmed facts
only (8.49.41).

nova/add_knowledge and nova/forget_knowledge sent back every stored fact,
pending ones included, so until a refresh the Memory tab showed facts nobody
had confirmed in the list of things Nova knows. Opening the tab
(nova/get_knowledge) already split them.
"""
from __future__ import annotations

import sys
import types

import pytest

CONFIRMED = {"id": 1, "key": "bins", "value": "Tuesday", "status": "confirmed"}
PENDING = {"id": 2, "key": "boiler", "value": "Worcester", "status": "pending"}


class _Conn:
    def __init__(self):
        self.user = types.SimpleNamespace(id="admin", name="Admin")
        self.results, self.errors = [], []

    def send_result(self, msg_id, result=None):
        self.results.append(result)

    def send_error(self, msg_id, code, message=""):
        self.errors.append(code)


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
    knowledge = load("knowledge")

    def all_facts(subject=None, now=None, subjects=None, status=None):
        stored = [CONFIRMED, PENDING]
        return [f for f in stored if status is None or f["status"] == status]
    monkeypatch.setattr(knowledge, "all_facts", all_facts)
    monkeypatch.setattr(knowledge, "remember", lambda *a, **k: PENDING)
    monkeypatch.setattr(knowledge, "forget", lambda **k: 1)
    sys.modules.pop("jc.ws_knowledge", None)
    yield load("ws_knowledge")
    sys.modules.pop("jc.ws_knowledge", None)


@pytest.mark.parametrize("handler, msg", [
    ("ws_add_knowledge", {"key": "boiler", "value": "Worcester"}),
    ("ws_forget_knowledge", {"fact_id": 3}),
])
async def test_the_list_sent_back_is_confirmed_facts_only(ws, fake_hass, handler, msg):
    conn = _Conn()
    await getattr(ws, handler)(fake_hass, conn, {"id": 1, **msg})
    assert conn.errors == []
    (result,) = conn.results
    assert result["facts"] == [CONFIRMED]
