"""Per-person routine store (v6.85.0).

This is the home of the person_patterns table: the routines the pattern
analyzer finds for each household member ("Username usually starts the coffee
around 06:40"). The table has existed since v6.41. The analyzer writes it and
the Memory panel reads it. This module puts that scattered logic behind one
small API, so anticipation and any later consumer read routines from a single
place.

Each function takes an optional db_path (the shared patterns.db by default),
sets up the schema itself before it writes, and never raises. A failed read
returns [] and a failed write returns False. Person ids go through identity,
so the key is the same everywhere ('Username' becomes 'username').
"""
from __future__ import annotations

import sqlite3
import logging
import json
from typing import Optional
from datetime import datetime

from . import paths
from .persistence import sqlite as _store

_LOGGER = logging.getLogger(__name__)

DB_PATH: Optional[str] = None  # override; None resolves via paths.py


def _db_path() -> str:
    return DB_PATH or paths.patterns_db()


def _normalize(person: str) -> str:
    try:
        from . import identity
        return identity.normalize(person)
    except Exception:
        return str(person or "")


def _open(path: str):
    return _store.connect(path, row_factory=False, mkdir=False, busy_timeout_ms=None)


def ensure_schema(db_path: Optional[str] = None) -> None:
    """Create the person_patterns table and its index when they are missing.
    Safe to repeat. cognitive_core also creates the table at start up. This
    keeps the module usable alone."""
    path = db_path or _db_path()
    try:
        with _open(path) as conn:
            _store.ensure(conn, "person_patterns")
    except Exception as exc:
        _LOGGER.debug("person_patterns ensure_schema failed: %s", exc)


def _existing_ids(conn, person: str, pattern_type: str, description: str,
                  key: Optional[str]) -> list:
    """Row ids of the routine being stored. A `key` matches on (person,
    pattern_type, routine_key), newest first. With no key the description is
    what matches."""
    if key:
        found = conn.execute(
            "SELECT id FROM person_patterns WHERE person = ? AND "
            "pattern_type = ? AND routine_key = ? ORDER BY id DESC",
            (person, pattern_type, key),
        )
    else:
        found = conn.execute(
            "SELECT id FROM person_patterns "
            "WHERE person = ? AND pattern_type = ? AND description = ?",
            (person, pattern_type, description),
        )
    return [row[0] for row in found.fetchall()]


def store(person: str, pattern_type: str, description: str, *,
          data: Optional[dict] = None, confidence: float = 0.0,
          occurrences: int = 1, db_path: Optional[str] = None,
          key: Optional[str] = None) -> bool:
    """Save a routine for a person, updating it when it already exists.

    With a `key` (cognitive.routines.routine_key) the routine is found by
    (person, pattern_type, routine_key), so measuring the same habit again
    refreshes one row, wording included. Without a key the match is on the
    description. The person is normalised. True means it was saved. This never
    raises."""
    if not person:
        return False
    path = db_path or _db_path()
    person = _normalize(person)
    try:
        ensure_schema(path)
        with _open(path) as conn:
            ids = _existing_ids(conn, person, pattern_type, description, key)
            seen_at = datetime.now().isoformat()
            payload = json.dumps(data or {})
            if not ids:
                conn.execute(
                    "INSERT INTO person_patterns "
                    "(person, pattern_type, description, data, confidence, "
                    "last_seen, occurrences, routine_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (person, pattern_type, description, payload, confidence,
                     seen_at, occurrences, key),
                )
                return True
            conn.execute(
                "UPDATE person_patterns SET description = ?, confidence = ?, "
                "occurrences = ?, last_seen = ?, data = ? WHERE id = ?",
                (description, confidence, occurrences, seen_at, payload, ids[0]),
            )
            # Rows left over from an interrupted upgrade: only one is kept.
            for extra in ids[1:]:
                conn.execute("DELETE FROM person_patterns WHERE id = ?", (extra,))
        return True
    except Exception as exc:
        _LOGGER.debug("person_patterns store failed: %s", exc)
        return False


def read(person: Optional[str] = None, db_path: Optional[str] = None) -> list[dict]:
    """Stored routines as column-keyed dicts, for one person (normalised) or
    for everyone, best confidence first. Never raises."""
    path = db_path or _db_path()
    try:
        with _open(path) as conn:
            conn.row_factory = sqlite3.Row
            if not person:
                query, params = ("SELECT * FROM person_patterns "
                                 "ORDER BY person, confidence DESC"), ()
            else:
                query, params = ("SELECT * FROM person_patterns WHERE person = ? "
                                 "ORDER BY confidence DESC"), (_normalize(person),)
            return [dict(row) for row in conn.execute(query, params).fetchall()]
    except Exception as exc:
        _LOGGER.debug("person_patterns read failed: %s", exc)
        return []
