"""
Nova — per-person routine store (v6.85.0).

The dedicated home for the person_patterns table: the per-household-member
routines the pattern analyzer detects ("Username usually starts the coffee around
06:40"). The table has existed since v6.41 and been written by the analyzer and
read by the Memory panel; this consolidates that scattered logic behind one
clean API so anticipation and any future consumer read routines from one place.

Every function takes an explicit db_path (default the shared patterns.db), is
self-sufficient (ensures the schema on write), and never raises — a read returns
[] and a write returns False on error. Person ids are normalized via identity so
the store key matches everywhere ('Username' -> 'username').
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Optional

from .persistence import sqlite as _store
from . import paths

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


def ensure_schema(db_path: Optional[str] = None) -> None:
    """Create the person_patterns table + index if missing. Idempotent.
    (cognitive_core also creates it at init; this keeps the module standalone.)"""
    db_path = db_path or _db_path()
    try:
        with sqlite3.connect(db_path) as conn:
            _store.ensure(conn, "person_patterns")
    except Exception as exc:
        _LOGGER.debug("person_patterns ensure_schema failed: %s", exc)


def store(person: str, pattern_type: str, description: str, *,
          data: Optional[dict] = None, confidence: float = 0.0,
          occurrences: int = 1, db_path: Optional[str] = None,
          key: Optional[str] = None) -> bool:
    """Upsert a person routine. With a `key` (cognitive.routines.routine_key)
    the routine is found by (person, pattern_type, routine_key), so the same
    habit measured again refreshes one row, wording included; without one it
    falls back to matching the description. person is normalized. Returns
    True on success. Never raises."""
    if not person:
        return False
    db_path = db_path or _db_path()
    person = _normalize(person)
    try:
        ensure_schema(db_path)
        with sqlite3.connect(db_path) as conn:
            if key:
                rows = conn.execute(
                    "SELECT id FROM person_patterns WHERE person = ? AND "
                    "pattern_type = ? AND routine_key = ? ORDER BY id DESC",
                    (person, pattern_type, key),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM person_patterns "
                    "WHERE person = ? AND pattern_type = ? AND description = ?",
                    (person, pattern_type, description),
                ).fetchall()
            now_iso = datetime.now().isoformat()
            payload = json.dumps(data or {})
            if rows:
                conn.execute(
                    "UPDATE person_patterns SET description = ?, confidence = ?, "
                    "occurrences = ?, last_seen = ?, data = ? WHERE id = ?",
                    (description, confidence, occurrences, now_iso, payload,
                     rows[0][0]),
                )
                # A duplicate left by an interrupted upgrade: keep one row.
                for (extra,) in rows[1:]:
                    conn.execute("DELETE FROM person_patterns WHERE id = ?", (extra,))
            else:
                conn.execute(
                    "INSERT INTO person_patterns "
                    "(person, pattern_type, description, data, confidence, "
                    "last_seen, occurrences, routine_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (person, pattern_type, description, payload, confidence,
                     now_iso, occurrences, key),
                )
        return True
    except Exception as exc:
        _LOGGER.debug("person_patterns store failed: %s", exc)
        return False


def read(person: Optional[str] = None, db_path: Optional[str] = None) -> list[dict]:
    """Read stored routines, optionally for one (normalized) person, ordered by
    confidence. Returns a list of dicts (column-keyed). Never raises."""
    db_path = db_path or _db_path()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if person:
                rows = conn.execute(
                    "SELECT * FROM person_patterns WHERE person = ? "
                    "ORDER BY confidence DESC", (_normalize(person),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM person_patterns ORDER BY person, confidence DESC"
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        _LOGGER.debug("person_patterns read failed: %s", exc)
        return []
