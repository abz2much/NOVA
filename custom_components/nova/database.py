"""Nova's conversation store (SQLite): chat turns, sentinel events and the
activity log, all in conversations.db.

Every operation opens a connection, commits on success (rolls back on an
error) and closes it again; nothing is left open for the garbage collector.
Reads and writes fail open: a failure is logged and the caller gets an
empty answer, while health() reports the store as degraded.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import paths
from .persistence import sqlite as _store

_LOGGER = logging.getLogger(__name__)

DB_PATH: Optional[Path] = None   # set to use another file; None asks paths.py

# The last connect, schema or write failure, for diagnostics; None when the
# last attempt worked.
_last_error: Optional[str] = None

# Newest `limit` rows of a window, handed back oldest first. id breaks ties
# between rows with the same timestamp so the order is always the same.
_RECENT_SQL = ("SELECT * FROM ("
               "  SELECT * FROM conversations WHERE timestamp > ?{scope} "
               "  ORDER BY timestamp DESC, id DESC LIMIT ?"
               ") ORDER BY timestamp ASC, id ASC")


def _db_path() -> Path:
    return DB_PATH or Path(paths.conversations_db())


def _utc_now() -> datetime:
    """Stored times are naive UTC ISO strings."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _since(hours: float) -> str:
    return (_utc_now() - timedelta(hours=hours)).isoformat()


def _failed(exc: Exception) -> None:
    global _last_error
    _last_error = f"{type(exc).__name__}: {exc}"


def _connect() -> sqlite3.Connection:
    """A connection with the schema applied. A connect or schema failure is
    logged and kept for health() before it is raised to the caller."""
    global _last_error
    try:
        conn = _store.connect(_db_path())
        _store.ensure(conn, "conversations")
        conn.commit()
    except Exception as exc:
        _failed(exc)
        _LOGGER.error("conversation DB connect/schema failed: %s", exc)
        raise
    _last_error = None
    return conn


@contextmanager
def _session() -> Iterator[sqlite3.Connection]:
    """One connection for one operation: committed when the block finishes,
    rolled back if it raises, and closed either way."""
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def health() -> dict:
    """Connect, apply the schema and read once. ``{'ok': bool, 'error': str}``,
    so diagnostics shows a failing store as degraded rather than silent."""
    try:
        with _session() as conn:
            conn.execute("SELECT 1 FROM conversations LIMIT 1")
    except Exception as exc:
        return {"ok": False, "error": _last_error or f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "error": ""}


# ── conversation turns ──────────────────────────────────────────────────────

def save_message(role: str, content: str, device_id: str = "unknown",
                 subject: Optional[str] = None) -> Optional[int]:
    """Store one turn; its row id, or None when it could not be written.
    A failed write (a full disk, say) is kept for health() too, so health
    does not report fine straight after one."""
    try:
        with _session() as conn:
            cursor = conn.execute(
                "INSERT INTO conversations (timestamp, device_id, role, content, subject) "
                "VALUES (?,?,?,?,?)",
                (_utc_now().isoformat(), device_id, role, content, subject))
            return cursor.lastrowid
    except Exception as exc:
        _failed(exc)
        _LOGGER.warning("Nova DB write error: %s", exc)
        return None


def get_recent_messages(hours: int = 24, device_id: Optional[str] = None,
                        limit: int = 200, subject: Optional[str] = None) -> list[dict]:
    """The newest ``limit`` turns of the last ``hours``, oldest first.

    ``device_id`` (the conversation) scopes the read when given, and
    ``subject`` is then ignored; ``subject`` (the person) is the fallback
    scope for a caller with no conversation to go by. The two are never
    combined. Taking the newest rows matters: ordering oldest first before
    the limit used to return the start of a long window instead of its end.
    No index on subject is needed: a household's rows are few, this
    fallback is rare, and the timestamp index narrows the scan first.
    """
    args: list = [_since(hours)]
    scope = ""
    if device_id:
        scope, args = " AND device_id = ?", args + [device_id]
    elif subject:
        scope, args = " AND subject = ?", args + [subject]
    try:
        with _session() as conn:
            rows = conn.execute(_RECENT_SQL.format(scope=scope), (*args, limit)).fetchall()
    except Exception as exc:
        _LOGGER.warning("Nova DB read error: %s", exc)
        return []
    return [dict(row) for row in rows]


def get_stats() -> dict:
    """How many turns are stored and the oldest one's time, for diagnostics."""
    try:
        with _session() as conn:
            total, oldest = conn.execute(
                "SELECT COUNT(*), MIN(timestamp) FROM conversations").fetchone()
    except Exception:
        return {}
    return {"total_messages": total, "oldest_entry": oldest}


def delete_conversation(conversation_id: str) -> int:
    """Remove one conversation's turns (the Chat tab's NEW CHAT); how many
    went, or 0 on a failure, which is logged."""
    if not conversation_id:
        return 0
    try:
        with _session() as conn:
            cursor = conn.execute("DELETE FROM conversations WHERE device_id = ?",
                                  (conversation_id,))
            return cursor.rowcount or 0
    except Exception as exc:
        _LOGGER.warning("Nova DB conversation delete error: %s", exc)
        return 0


def purge_old_records(days: int = 30) -> int:
    """Drop turns, sentinel events and activity older than ``days``. Returns
    how many turns went."""
    cutoff = (_utc_now() - timedelta(days=days)).isoformat()
    try:
        with _session() as conn:
            turns = conn.execute("DELETE FROM conversations WHERE timestamp < ?",
                                 (cutoff,)).rowcount
            for table in ("sentinel_events", "activity_log"):
                conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
            return turns
    except Exception as exc:
        _LOGGER.warning("Nova DB purge error: %s", exc)
        return 0


# ── sentinel events and the activity log ────────────────────────────────────

def save_sentinel_event(entity_id: str, event_type: str, detail: str = "") -> None:
    try:
        with _session() as conn:
            conn.execute("INSERT INTO sentinel_events (timestamp, entity_id, event_type, "
                         "detail) VALUES (?,?,?,?)",
                         (_utc_now().isoformat(), entity_id, event_type, detail))
    except Exception as exc:
        _LOGGER.warning("Nova sentinel DB write error: %s", exc)


def save_activity(*, entity_id: str = "", category: str = "other", urgency: str = "low",
                  message: str = "", was_spoken: bool = False, source: str = "observer",
                  pushed: bool = False, outcome: str = "", request_id: str = "") -> None:
    """Log one observer event, sentinel alert or other activity. A proactive
    message also records whether it was pushed, why it was or was not
    delivered, and its action request (delivery.py)."""
    row = (_utc_now().isoformat(), entity_id, category, urgency, message,
           int(bool(was_spoken)), source, int(bool(pushed)),
           outcome or None, request_id or None)
    try:
        with _session() as conn:
            conn.execute(
                "INSERT INTO activity_log (timestamp, entity_id, category, urgency, "
                "message, was_spoken, source, pushed, outcome, request_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", row)
    except Exception as exc:
        _LOGGER.warning("Nova activity log write error: %s", exc)


def get_recent_activity(hours: int = 24, limit: int = 50) -> list[dict]:
    """The last ``hours`` of activity, newest first."""
    try:
        with _session() as conn:
            rows = conn.execute(
                "SELECT * FROM activity_log WHERE timestamp > ? "
                "ORDER BY timestamp DESC LIMIT ?", (_since(hours), limit)).fetchall()
    except Exception as exc:
        _LOGGER.warning("Nova activity log read error: %s", exc)
        return []
    return [dict(row) for row in rows]


def get_activity_count_today() -> int:
    """Spoken announcements since midnight (UTC)."""
    midnight = _utc_now().replace(hour=0, minute=0, second=0).isoformat()
    try:
        with _session() as conn:
            row = conn.execute("SELECT COUNT(*) FROM activity_log "
                               "WHERE timestamp > ? AND was_spoken = 1",
                               (midnight,)).fetchone()
    except Exception:
        return 0
    return row[0] if row else 0


def get_activity_counts(hours: int = 24) -> dict:
    """Counts over the last ``hours`` of activity, worked out by SQLite so a
    busy home is never capped at a page of rows: everything, spoken, and
    messages that say "flagged" or "not worth"."""
    empty = {"total": 0, "spoken": 0, "flagged": 0, "dropped": 0}
    try:
        with _session() as conn:
            row = conn.execute(
                "SELECT COUNT(*),"
                " SUM(CASE WHEN was_spoken THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN instr(COALESCE(message, ''), 'flagged') > 0 THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN instr(COALESCE(message, ''), 'not worth') > 0 THEN 1 ELSE 0 END)"
                " FROM activity_log WHERE timestamp > ?", (_since(hours),)).fetchone()
    except Exception as exc:
        _LOGGER.warning("Nova activity log count error: %s", exc)
        return empty
    return {key: int(value or 0) for key, value in zip(empty, row)} if row else empty
