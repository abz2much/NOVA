"""`id` is reserved in Home Assistant's WebSocket messages (v6.36.2).

The frontend numbers every message through `id` and overwrites whatever a
caller put there. A command whose schema asks for its own `id` therefore gets
the sequence number instead; that is how the Memory tab's forget button once
deleted nothing at all. Record ids travel under their own names (`fact_id`).
"""
from __future__ import annotations

import ast
import pathlib
import re

from ws_sources import ws_function, ws_text, ws_tree

PANEL_SOURCE = (pathlib.Path(__file__).resolve().parents[2]
                / "custom_components" / "nova" / "frontend" / "nova-panel.js")

# The body of a `{ type: "nova/…", … }` literal the panel sends, without the
# type entry itself. Nested objects are skipped, which is fine for key names.
PANEL_CALL = re.compile(r"""\{\s*type:\s*["'](nova/[a-z0-9_]+)["']([^{}]*)\}""")
OBJECT_KEY = re.compile(r"([A-Za-z_]\w*)\s*:")


def _panel_calls() -> dict[str, list[set[str]]]:
    """Command -> the key sets of every literal call the panel makes for it."""
    calls: dict[str, list[set[str]]] = {}
    for command, body in PANEL_CALL.findall(PANEL_SOURCE.read_text(encoding="utf-8")):
        calls.setdefault(command, []).append(set(OBJECT_KEY.findall(body)))
    return calls


def _schema_keys_named(name: str, within: ast.AST | None = None) -> list[int]:
    """Line numbers of every vol.Required(name) / vol.Optional(name) call,
    across the websocket layer or inside one function (decorators included)."""
    hits = []
    for node in ast.walk(within if within is not None else ws_tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"Required", "Optional"} and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == name):
            hits.append(node.lineno)
    return hits


# ── backend schemas ──────────────────────────────────────────────────────

def test_no_command_schema_asks_for_a_plain_id_by_text():
    pattern = r"""vol\.(?:Optional|Required)\(\s*["']id["']\s*\)"""
    assert re.findall(pattern, ws_text()) == [], (
        "a schema declares 'id', which the frontend overwrites; use e.g. 'fact_id'")


def test_no_command_schema_asks_for_a_plain_id_by_syntax_tree():
    # Catches spellings the text search above would miss, such as a schema
    # key split over lines or written with a keyword argument after it.
    assert _schema_keys_named("id") == []


# ── the forget button, end to end ────────────────────────────────────────

def test_forget_schema_accepts_fact_id():
    handler = ws_function("ws_forget_knowledge")
    assert _schema_keys_named("fact_id", within=handler), (
        "nova/forget_knowledge lost its fact_id field")


def test_forget_button_sends_fact_id_and_not_id():
    sent = _panel_calls().get("nova/forget_knowledge")
    assert sent, "the panel no longer calls nova/forget_knowledge"
    for keys in sent:
        assert "fact_id" in keys
        assert "id" not in keys


async def test_forget_handler_reads_fact_id(load, monkeypatch):
    import sys
    from fakes import FakeHass
    from ws_kit import _Conn, _stub_ws_api
    _stub_ws_api(monkeypatch)
    monkeypatch.delitem(sys.modules, "jc.ws_knowledge", raising=False)
    wk, knowledge = load("ws_knowledge"), load("knowledge")
    forgot = []
    monkeypatch.setattr(knowledge, "forget", lambda **kw: forgot.append(kw) or 1)
    monkeypatch.setattr(knowledge, "all_facts", lambda **kw: [])
    conn = _Conn()
    await wk.ws_forget_knowledge(FakeHass(), conn, {"id": 41, "fact_id": 7})
    assert forgot == [{"fact_id": 7, "subject": None, "key": None}]
    assert conn.results == [(41, {"removed": 1, "facts": []})]   # the reply goes on the message number


# ── every other panel call ──────────────────────────────────────────────

def test_no_panel_call_puts_its_own_value_under_id():
    calls = _panel_calls()
    assert len(calls) >= 20, "panel call extraction found too little to be trusted"
    offenders = sorted(cmd for cmd, key_sets in calls.items()
                       if any("id" in keys for keys in key_sets))
    assert offenders == []
