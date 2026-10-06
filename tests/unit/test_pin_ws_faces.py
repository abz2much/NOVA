"""Pin what the Faces tab commands in ws_faces.py do today (8.7.22, tests only).

nova/list_faces, nova/add_resident and nova/remove_resident. The roster is
what the opt in face_stand_down trusts: a recognised resident can stop a new
intrusion investigation opening. The handlers run against the real
face_roster module (its save is faked), with the decorators stubbed to pass
through and a recording connection. Tests named test_current_behaviour_* pin
behaviour that looks wrong; each says why.
"""
from __future__ import annotations

import sys

import pytest

from fakes import FakeHass
from test_pin_ws_update_config import _Conn, _stub_ws_api


@pytest.fixture
def faces(load, monkeypatch):
    _stub_ws_api(monkeypatch)
    sys.modules.pop("jc.ws_faces", None)
    mod = load("ws_faces")
    yield mod
    sys.modules.pop("jc.ws_faces", None)


@pytest.fixture
def roster(load, monkeypatch):
    fr = load("face_roster")
    monkeypatch.setattr(fr, "_NAMES", {})
    saves = []

    async def _save(hass):
        saves.append(dict(fr._NAMES))
        return fr._save_ok
    fr._save_ok = True
    monkeypatch.setattr(fr, "_save", _save)
    fr._saves = saves
    yield fr
    del fr._save_ok


async def _call(handler, **msg):
    conn = _Conn()
    await handler(FakeHass(), conn, {"id": 2, **msg})
    return conn


async def test_adding_a_resident(faces, roster):
    conn = await _call(faces.ws_add_resident, name="  Abi ")
    assert conn.results == [(2, {"added": True, "residents": ["Abi"], "saved": True})]
    assert roster._saves == [{"abi": "Abi"}]


async def test_adding_a_resident_twice_adds_once(faces, roster):
    await _call(faces.ws_add_resident, name="Abi")
    conn = await _call(faces.ws_add_resident, name="ABI")
    assert conn.results == [(2, {"added": False, "residents": ["Abi"], "saved": True})]
    assert len(roster._saves) == 1


async def test_a_resident_that_could_not_be_saved_says_so(faces, roster):
    roster._save_ok = False
    conn = await _call(faces.ws_add_resident, name="Abi")
    assert conn.results == [(2, {"added": True, "residents": ["Abi"], "saved": False})]


@pytest.mark.parametrize("name", ["", "   ", "unknown", "Unknown", "a\x00b", "x" * 61])
async def test_an_invalid_name_is_refused(faces, roster, name):
    conn = await _call(faces.ws_add_resident, name=name)
    assert conn.errors == [(2, "invalid_name",
                            "A name is 1 to 60 characters, with no control characters, "
                            "and cannot be 'unknown'")]
    assert roster._NAMES == {} and roster._saves == []


async def test_a_full_roster_is_refused(faces, roster, monkeypatch):
    monkeypatch.setattr(roster, "MAX_RESIDENTS", 2)
    await _call(faces.ws_add_resident, name="A")
    await _call(faces.ws_add_resident, name="B")
    conn = await _call(faces.ws_add_resident, name="C")
    assert conn.errors == [(2, "roster_full", "The roster holds at most 2 residents")]
    assert sorted(roster._NAMES.values()) == ["A", "B"]


async def test_a_failed_add_is_add_failed(faces, roster, monkeypatch):
    async def boom(hass):
        raise RuntimeError("disk")
    monkeypatch.setattr(roster, "_save", boom)
    conn = await _call(faces.ws_add_resident, name="Abi")
    assert conn.errors == [(2, "add_failed", "The resident could not be added")]


async def test_current_behaviour_adding_a_resident_writes_no_audit_row(faces, roster, load,
                                                                       monkeypatch):
    # Looks wrong: with face_stand_down on, a name on this roster can stop
    # a new intrusion investigation opening. Adding one is a safety change,
    # but nothing records who added it or when (no Action Audit Log row, no
    # nova_log line).
    al = load("action_log")
    rows = []
    for name in ("start", "start_many", "set_approval", "set_execution"):
        monkeypatch.setattr(al, name, lambda *a, **k: rows.append(a))
    conn = await _call(faces.ws_add_resident, name="Stranger")
    assert conn.results[0][1]["added"] is True and rows == []


async def test_removing_a_resident(faces, roster):
    await _call(faces.ws_add_resident, name="Abi")
    conn = await _call(faces.ws_remove_resident, name="abi")
    assert conn.results == [(2, {"removed": True, "residents": [], "saved": True})]
    conn = await _call(faces.ws_remove_resident, name="abi")
    assert conn.results == [(2, {"removed": False, "residents": [], "saved": True})]


async def test_removing_a_blank_name_is_refused(faces, roster):
    conn = await _call(faces.ws_remove_resident, name=" ")
    assert conn.errors == [(2, "invalid_name", "Give the name to remove")]


async def test_a_failed_remove_is_remove_failed(faces, roster, monkeypatch):
    await _call(faces.ws_add_resident, name="Abi")

    async def boom(hass):
        raise RuntimeError("disk")
    monkeypatch.setattr(roster, "_save", boom)
    conn = await _call(faces.ws_remove_resident, name="Abi")
    assert conn.errors == [(2, "remove_failed", "That name could not be removed")]


@pytest.mark.parametrize("handler", ["ws_add_resident", "ws_remove_resident"])
async def test_an_unexpected_failure_returns_no_raw_text(faces, roster, monkeypatch, handler):
    async def boom(hass, raw):
        raise RuntimeError("token=abc")
    monkeypatch.setattr(roster, "async_add", boom)
    monkeypatch.setattr(roster, "async_remove", boom)
    conn = await _call(getattr(faces, handler), name="Abi")
    assert conn.errors == [(2, "faces_failed",
                            "RuntimeError (details are in the Home Assistant log)")]


async def test_list_faces(faces, roster, load, monkeypatch):
    rec = load("recognition")
    seen = []
    monkeypatch.setattr(rec, "recent_faces", lambda hass, limit: seen.append(limit) or [])
    monkeypatch.setattr(rec, "source_status", lambda hass: {"frigate": True})
    await _call(faces.ws_add_resident, name="Abi")
    conn = await _call(faces.ws_list_faces)
    assert conn.results == [(2, {"faces": [], "residents": ["Abi"],
                                 "sources": {"frigate": True},
                                 "confidence_threshold": rec.CONFIDENCE_THRESHOLD})]
    await _call(faces.ws_list_faces, limit=5)
    assert seen == [20, 5]


async def test_list_faces_failure_returns_no_raw_text(faces, roster, load, monkeypatch):
    def boom(hass, limit):
        raise RuntimeError("token=abc")
    monkeypatch.setattr(load("recognition"), "recent_faces", boom)
    conn = await _call(faces.ws_list_faces)
    assert conn.errors == [(2, "faces_failed",
                            "RuntimeError (details are in the Home Assistant log)")]
