"""Self-scheduled follow-ups: instructions the agent leaves for its future self.

Each follow-up is a row in the ``followups`` table of the patterns store.
The cognitive tick calls :func:`async_process_due`, which hands every due
instruction to a headless agent runner and turns each answer into a
proactive action for the normal delivery path (quiet hours and urgency
routing are applied there). The CRUD helpers are blocking SQLite calls for
the executor and never raise to their callers.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from .safe_errors import safe_error_message
from . import paths
from .persistence import sqlite as _store

_LOGGER = logging.getLogger(__name__)

DB_PATH: Optional[str] = None

MAX_OPEN = 25
MAX_DELAY_MINUTES = 7 * 24 * 60
STATUSES = ("pending", "done", "cancelled", "failed")

_MIN_DELAY_MINUTES = 0.1


def _db_path() -> str:
    """The store location, resolved on every call."""
    return DB_PATH if DB_PATH else paths.patterns_db()


def _resolve(db_path: Optional[str]) -> str:
    return db_path or _db_path()


def _open(db_path: Optional[str]) -> sqlite3.Connection:
    conn = _store.connect(_resolve(db_path), mkdir=False)
    _store.ensure(conn, "followups")
    return conn


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now()


def _stamp(moment: datetime) -> str:
    return moment.isoformat(sep=" ")


def schedule(instruction: str, delay_minutes: float, *, context: str = "",
             now: Optional[datetime] = None,
             db_path: Optional[str] = None) -> dict:
    """Queue ``instruction`` to run ``delay_minutes`` from now."""
    text = (instruction or "").strip()
    if not text:
        return {"error": "empty instruction"}
    try:
        delay = float(delay_minutes)
    except (TypeError, ValueError):
        return {"error": f"bad delay: {delay_minutes!r}"}
    delay = min(max(delay, _MIN_DELAY_MINUTES), MAX_DELAY_MINUTES)

    moment = _now(now)
    due = moment + timedelta(minutes=delay)
    try:
        conn = _open(db_path)
        with conn:
            (open_count,) = conn.execute(
                "SELECT COUNT(*) FROM followups WHERE status = 'pending'"
            ).fetchone()
            cap = MAX_OPEN
            if open_count >= cap:
                return {"error": f"follow-up queue is full ({cap} pending)"}
            cur = conn.execute(
                "INSERT INTO followups (created_ts, due_ts, instruction, context) "
                "VALUES (?, ?, ?, ?)",
                (_stamp(moment), _stamp(due), text, context),
            )
            new_id = cur.lastrowid
        return {"id": new_id, "due_ts": _stamp(due), "instruction": text}
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Nova follow-ups: could not schedule: %s", exc)
        return {"error": safe_error_message(exc)}


def pending(*, now: Optional[datetime] = None, due_only: bool = False,
            db_path: Optional[str] = None) -> list[dict]:
    """Pending follow-ups, soonest first; only the due ones with ``due_only``."""
    try:
        conn = _open(db_path)
        with conn:
            if due_only:
                rows = conn.execute(
                    "SELECT * FROM followups WHERE status = 'pending' "
                    "AND datetime(due_ts) <= datetime(?) "
                    "ORDER BY datetime(due_ts) ASC",
                    (_stamp(_now(now)),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM followups WHERE status = 'pending' "
                    "ORDER BY datetime(due_ts) ASC"
                ).fetchall()
        return [dict(row) for row in rows]
    except Exception:  # noqa: BLE001
        return []


def cancel(followup_id: int, *, db_path: Optional[str] = None) -> bool:
    """Cancel a pending follow-up. True only when a row actually changed."""
    try:
        target = int(followup_id)
        conn = _open(db_path)
        with conn:
            cur = conn.execute(
                "UPDATE followups SET status = 'cancelled' "
                "WHERE id = ? AND status = 'pending'",
                (target,),
            )
            return cur.rowcount > 0
    except Exception:  # noqa: BLE001
        return False


def mark(followup_id: int, status: str, result: str = "", *,
         db_path: Optional[str] = None) -> None:
    """Record an outcome on a follow-up, whatever its current status."""
    if status not in STATUSES:
        return
    try:
        conn = _open(db_path)
        with conn:
            conn.execute(
                "UPDATE followups SET status = ?, result = ? WHERE id = ?",
                (status, result[:2000], followup_id),
            )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova follow-ups: mark %s failed: %s", followup_id, exc)


async def async_process_due(
    hass: Any,
    config: dict,
    runner: Optional[Callable[[str, str], Awaitable[str]]] = None,
    *,
    now: Optional[datetime] = None,
    db_path: Optional[str] = None,
) -> list[dict]:
    """Run every due follow-up and return the proactive actions they produce."""
    try:
        due = await hass.async_add_executor_job(
            lambda: pending(now=now, due_only=True, db_path=db_path)
        )
    except Exception:  # noqa: BLE001
        due = []
    if not due:
        return []

    if runner is None:
        for row in due:
            await hass.async_add_executor_job(
                lambda fid=row["id"]: mark(fid, "failed", "no runner available",
                                           db_path=db_path)
            )
        return []

    actions: list[dict] = []
    for row in due:
        fid = row["id"]
        instruction = row["instruction"]
        try:
            answer = await runner(instruction, row["context"] or "")
            answer = (answer or "").strip() or "Done."
            await hass.async_add_executor_job(
                lambda f=fid, r=answer: mark(f, "done", r, db_path=db_path)
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Nova follow-up #%s failed: %s", fid, exc)
            await hass.async_add_executor_job(
                lambda f=fid, e=str(exc): mark(f, "failed", e, db_path=db_path)
            )
            continue

        actions.append({
            "type": "followup",
            "urgency": "medium",
            "message": answer,
            "auto_act": True,
        })
        try:
            from . import database
            database.save_activity(
                category="followup",
                urgency="low",
                message=f"Follow-up #{fid} ran: {instruction[:120]} → {answer[:160]}",
                source="agent",
            )
        except Exception:  # noqa: BLE001
            pass
    return actions
