"""Nova — Spoken History.

Records the text of what Nova actually sent to a speaker (welcome-home,
reminders, alerts, briefings, manual tests, confirmed Assist replies, and
repeats), so the panel can show "the last things Nova said" and a voice
command can ask Nova to repeat the last one.

Storage: a dedicated table in the existing conversations database
(database.py's own conversations/activity_log store) rather than a new
database file — this is the closest existing fit, and it's already the
home of the panel's Logs tab. Ownership of the table is independent of
database.py, the same shape automation_trials.py already uses to share
patterns.db with other modules: every entry point takes ``db_path``
resolved at call time, so tests run fully isolated.

Delivery is "sent" the moment Home Assistant accepts the service call —
this module makes no claim that audio was physically heard or played.
A recording failure must never break speech: every write here is
best-effort and never raises into its caller.

An in-memory mirror of the most recent entry (``_last``) is kept so the
deterministic local "repeat that" command (local_engine.py) can answer
instantly without touching SQLite from the event loop — matching the
requirement that all SQLite work here stays off it. ``hydrate()`` fills
that mirror from disk once at startup so a restart doesn't lose it; it
also never raises, so a history-store problem can never block setup.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# Overridden by configure(hass) with the instance's own reported config
# directory (hass.config.path(...)) — this default only applies before
# configure() has run (e.g. in isolated unit tests that never call it).
_DEFAULT_DB = "/config/nova/conversations.db"

_MAX_ENTRIES = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spoken_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      REAL    NOT NULL,
    text           TEXT    NOT NULL,
    source         TEXT    NOT NULL,
    speakers       TEXT    NOT NULL,
    delivery_state TEXT    NOT NULL DEFAULT 'sent',
    repeat_of_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_spoken_history_ts ON spoken_history(timestamp);
"""


def _migrate_action_request_id_column(conn: sqlite3.Connection) -> None:
    """Additive migration: nullable action_request_id, linking a spoken row
    back to the Action Audit Log request it narrates (if any) — same
    convention as database.py::_migrate_subject_column. Re-checked on every
    connect, no cached flag. Never stores the action row's text or any
    other action_log field, only the request_id string."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(spoken_history)")}
    if "action_request_id" in cols:
        return
    try:
        conn.execute("ALTER TABLE spoken_history ADD COLUMN action_request_id TEXT")
    except sqlite3.OperationalError:
        cols_after = {row["name"] for row in conn.execute("PRAGMA table_info(spoken_history)")}
        if "action_request_id" not in cols_after:
            raise

# In-memory mirror of the most recent successfully recorded entry, e.g.
# {"id": 42, "text": "...", "source": "reminder", "speakers": [...], "repeat_of_id": None}.
# Never read from or written to SQLite directly — see module docstring.
_last: Optional[dict] = None


def configure(hass) -> None:
    """Point the default db path at this Home Assistant instance's own
    config directory (mirrors nova_config.py's own configure()). Call
    once, early in async_setup_entry, before any spoken_history access."""
    global _DEFAULT_DB
    _DEFAULT_DB = hass.config.path("nova", "conversations.db")


def _resolve(db_path: Optional[str]) -> str:
    return db_path or _DEFAULT_DB


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_action_request_id_column(conn)
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    keys = row.keys()
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "text": row["text"],
        "source": row["source"],
        "speakers": json.loads(row["speakers"]) if row["speakers"] else [],
        "delivery_state": row["delivery_state"],
        "repeat_of_id": row["repeat_of_id"],
        "action_request_id": row["action_request_id"] if "action_request_id" in keys else None,
    }


def record(
    text: str,
    source: str,
    speakers: list,
    repeat_of_id: Optional[int] = None,
    db_path: Optional[str] = None,
    *,
    action_request_id: Optional[str] = None,
) -> Optional[int]:
    """Insert one spoken-history row and prune to the most recent
    _MAX_ENTRIES. Call only after Home Assistant has actually accepted the
    speech request — ``speakers`` should be the subset that succeeded, not
    every target attempted. Never raises: a recording failure must never
    break speech, so every error here is logged and swallowed.

    ``repeat_of_id`` is flattened to the true original automatically — if
    the referenced row is itself a "repeat" entry, this stores a reference
    to *its* original instead, so a chain of repeats never nests.

    ``action_request_id`` (Action Audit Log linkage, v3 correction):
    optional, nullable, and purely a reference — this stores the shared
    request_id string only, never any action_log field or text. The link
    is to the REQUEST (a bulk action, a briefing), not to one target row,
    since one spoken message can describe several targets at once. Omit it
    for ordinary speech that isn't about a logged action (the common case).
    Never affects delivery or recording — a bad/unknown request_id is
    stored as given, the panel resolves it, this module doesn't validate it.

    Safe to call from an executor thread only (does blocking SQLite I/O).
    """
    global _last
    if not text or not source or not speakers:
        return None
    db = _resolve(db_path)
    try:
        conn = _connect(db)
        try:
            resolved_repeat_of_id = repeat_of_id
            if repeat_of_id is not None:
                ref = conn.execute(
                    "SELECT source, repeat_of_id FROM spoken_history WHERE id = ?",
                    (repeat_of_id,),
                ).fetchone()
                if ref is not None and ref["source"] == "repeat" and ref["repeat_of_id"] is not None:
                    resolved_repeat_of_id = ref["repeat_of_id"]

            cur = conn.execute(
                "INSERT INTO spoken_history "
                "(timestamp, text, source, speakers, delivery_state, repeat_of_id, action_request_id) "
                "VALUES (?, ?, ?, ?, 'sent', ?, ?)",
                (time.time(), text, source, json.dumps(list(speakers)), resolved_repeat_of_id,
                 action_request_id),
            )
            new_id = cur.lastrowid
            conn.execute(
                "DELETE FROM spoken_history WHERE id NOT IN "
                "(SELECT id FROM spoken_history ORDER BY id DESC LIMIT ?)",
                (_MAX_ENTRIES,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        _LOGGER.warning("Nova spoken_history: record failed: %s", exc)
        return None

    _last = {
        "id": new_id,
        "text": text,
        "source": source,
        "speakers": list(speakers),
        "repeat_of_id": resolved_repeat_of_id,
        "action_request_id": action_request_id,
    }
    return new_id


def list_recent(db_path: Optional[str] = None) -> list:
    """Newest-first, up to the retention cap. Raises on a genuine
    connect/schema/read failure so the caller (the websocket handler) can
    report a distinct error state instead of an indistinguishable empty
    one — same convention as setup_health.py/provider_activity.py."""
    db = _resolve(db_path)
    conn = _connect(db)
    try:
        rows = conn.execute(
            "SELECT id, timestamp, text, source, speakers, delivery_state, repeat_of_id, "
            "action_request_id "
            "FROM spoken_history ORDER BY id DESC LIMIT ?",
            (_MAX_ENTRIES,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def get(spoken_id: int, db_path: Optional[str] = None) -> Optional[dict]:
    """One entry by id, or None if it doesn't exist. Raises on a genuine
    connect/schema failure (same convention as list_recent)."""
    db = _resolve(db_path)
    conn = _connect(db)
    try:
        row = conn.execute(
            "SELECT id, timestamp, text, source, speakers, delivery_state, repeat_of_id, "
            "action_request_id "
            "FROM spoken_history WHERE id = ?",
            (spoken_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row is not None else None


def get_last() -> Optional[dict]:
    """The in-memory mirror only — never touches SQLite, safe to call
    directly on the event loop. Used by the deterministic local
    "repeat that" command."""
    return dict(_last) if _last is not None else None


def find_by_action_request_id(
    request_ids: list[str], db_path: Optional[str] = None,
) -> dict[str, int]:
    """{request_id: spoken_history_id} for whichever of `request_ids` have a
    linked spoken row — a direct indexed lookup, not bounded by
    list_recent's retention window (an action page can legitimately show
    requests older than the last 100 spoken entries). When a request_id has
    more than one spoken row (rare — e.g. a briefing that pushed and also
    spoke), the most recent one wins. Raises on a genuine connect/schema
    failure (same convention as list_recent/get); returns {} for an empty
    or falsy `request_ids`."""
    if not request_ids:
        return {}
    db = _resolve(db_path)
    conn = _connect(db)
    try:
        placeholders = ",".join("?" for _ in request_ids)
        rows = conn.execute(
            f"SELECT id, action_request_id FROM spoken_history "
            f"WHERE action_request_id IN ({placeholders}) ORDER BY id ASC",
            tuple(request_ids),
        ).fetchall()
    finally:
        conn.close()
    result: dict[str, int] = {}
    for row in rows:
        result[row["action_request_id"]] = row["id"]  # later rows overwrite -> most recent wins
    return result


def hydrate(db_path: Optional[str] = None) -> None:
    """Load the most recent row into the in-memory mirror at startup, so a
    voice "repeat that" works right after a restart without Nova needing
    to speak something new first. Never raises — a history-store problem
    must never block integration setup. Safe to call from an executor
    thread only (does blocking SQLite I/O); call via
    hass.async_add_executor_job from async_setup_entry."""
    global _last
    try:
        db = _resolve(db_path)
        conn = _connect(db)
        try:
            row = conn.execute(
                "SELECT id, text, source, speakers, repeat_of_id FROM spoken_history "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            _last = {
                "id": row["id"],
                "text": row["text"],
                "source": row["source"],
                "speakers": json.loads(row["speakers"]) if row["speakers"] else [],
                "repeat_of_id": row["repeat_of_id"],
            }
    except Exception as exc:
        _LOGGER.warning("Nova spoken_history: hydrate failed (non-fatal): %s", exc)
