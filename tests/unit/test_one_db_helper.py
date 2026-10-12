"""Every Nova database opens through persistence.sqlite.connect (8.43.0),
with write-ahead logging on every store. A store that passes
busy_timeout_ms=None keeps the wait its own `timeout` sets."""
from __future__ import annotations

import ast
import pathlib

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


def _calls(name: str):
    for path in sorted(COMP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == name:
                yield path.relative_to(COMP).as_posix(), node


def test_only_the_helper_calls_sqlite3_connect():
    found = sorted({where for where, _n in _calls("sqlite3.connect")})
    assert found == ["persistence/sqlite.py"]


def test_no_store_turns_wal_off():
    # provider_activity negotiates WAL itself under its own lock, then sets it.
    off = []
    for where, node in _calls("_store.connect"):
        for kw in node.keywords:
            if kw.arg == "wal" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                off.append(where)
    assert off == ["provider_activity.py"]
    assert 'conn.execute("PRAGMA journal_mode=WAL")' in (
        COMP / "provider_activity.py").read_text(encoding="utf-8")


def test_every_store_is_wal(load):
    schema = load("persistence.schema")
    assert all(store.wal for store in schema.STORES)
