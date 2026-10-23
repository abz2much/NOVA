"""Automation trials (Phase 3): a probation record for each suggestion that
became a live automation.

Installing a suggestion means the user ACCEPTED it, and the Decision Record
for the suggestion already says so (`decision_record.set_outcome_by_ref(
"suggestion:N", "good", "installed")`). This table answers a different
question: does the automation actually RUN and, through explicit manual
feedback only, does the person living with it think it works? The two stay
separate. Nothing here is inferred from the installation.

Run counts come from Home Assistant's own ``automation_triggered`` event, the
one documented and supported way to see that an automation fired
(homeassistant/components/automation/__init__.py). The only claim is that the
automation ran. Following the context.parent_id of a later device state to
judge whether it did what it was *meant* to do is outside this phase.

``automation_entity_id`` is NULL when the trial is created. It is filled in
lazily, on the event loop and never from a background thread (see
``async_handle_triggered``), the first time the automation really fires. No
periodic sweep resolves it. Resolution follows events and nothing else.

The module needs only SQLite and the standard library. Every entry point takes
a ``db_path`` that is resolved when it is called, so tests can run on their own
files. It has the same shape as decision_record.py and uses the same shared
``patterns.db`` that other Nova modules read and write.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

_DEFAULT_DB: Optional[str] = None  # override; None resolves via paths.py


def _default_db() -> str:
    from .. import paths  # lazily: the package reaches root modules only at call time
    return _DEFAULT_DB or paths.patterns_db()


def _resolve(db_path: Optional[str]) -> str:
    return db_path or _default_db()


def _connect(db_path: str) -> sqlite3.Connection:
    from ..persistence import sqlite as _store   # lazy: package layering rule
    conn = _store.connect(db_path)
    _store.ensure(conn, "automation_trials")  # idempotent — adds the table without
                                              # touching patterns.db's other tables
    return conn


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def create(
    suggestion_id: int,
    automation_id: str,
    installed_at: Optional[float] = None,
    db_path: Optional[str] = None,
) -> Optional[int]:
    """Add the trial row for a newly installed automation. It is best effort
    by design: a failure to record must never break the installation it
    records. Returns the new row id, or None on failure."""
    try:
        conn = _connect(_resolve(db_path))
    except Exception:
        return None
    try:
        started = float(installed_at) if installed_at is not None else time.time()
        cur = conn.execute(
            "INSERT INTO automation_trials "
            "(suggestion_id, automation_id, automation_entity_id, installed_at, run_count) "
            "VALUES (?, ?, NULL, ?, 0)",
            (int(suggestion_id), str(automation_id), started),
        )
        conn.commit()
        new_id = cur.lastrowid
        return None if new_id is None else int(new_id)
    except Exception:
        return None
    finally:
        _close(conn)


def record_run(
    automation_entity_id: str,
    automation_id: Optional[str] = None,
    ts: Optional[float] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Record one confirmed run, from a real ``automation_triggered`` event.

    Matches an already-resolved trial by ``automation_entity_id`` (the fast
    path, true for every run after the first), or an unresolved one by its
    ``automation_id`` (the entity registry's unique_id) — resolving it in the
    very same statement. Returns whether any row was updated (False for a
    non-Nova automation, which is the overwhelmingly common case — every
    automation in the house fires this event, not just Nova's).
    """
    try:
        conn = _connect(_resolve(db_path))
    except Exception:
        return False
    try:
        ran_at = float(ts) if ts is not None else time.time()
        cur = conn.execute(
            "UPDATE automation_trials SET run_count = run_count + 1, last_run = ?, "
            "automation_entity_id = ? "
            "WHERE automation_entity_id = ? "
            "   OR (automation_entity_id IS NULL AND automation_id = ?)",
            (ran_at, automation_entity_id, automation_entity_id, automation_id),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        return False
    finally:
        _close(conn)


def list_trials(db_path: Optional[str] = None) -> list:
    """Every trial, the most recently installed first. A plain read that never
    raises."""
    path = _resolve(db_path)
    if not Path(path).exists():
        return []
    try:
        conn = _connect(path)
    except Exception:
        return []
    try:
        cursor = conn.execute(
            "SELECT * FROM automation_trials ORDER BY installed_at DESC")
        return [dict(row) for row in cursor.fetchall()]
    except Exception:
        return []
    finally:
        _close(conn)


def set_manual_outcome(
    trial_id: int,
    verdict: str,
    ts: Optional[float] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Record the household's own assessment: 'working' or 'needs_adjustment'.

    Unlike a Decision Record's immutable outcome, this is NOT set-once — the
    person living with the automation can legitimately change their mind
    later (they adjusted it, or it started misbehaving), and that's still
    manual feedback, never an inferred verdict. Returns whether a row with
    that id exists.
    """
    try:
        conn = _connect(_resolve(db_path))
    except Exception:
        return False
    try:
        given_at = float(ts) if ts is not None else time.time()
        cur = conn.execute(
            "UPDATE automation_trials SET manual_outcome = ?, manual_outcome_ts = ? WHERE id = ?",
            (str(verdict), given_at, int(trial_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        return False
    finally:
        _close(conn)


async def async_handle_triggered(hass, event) -> None:
    """Bus listener for Home Assistant's real ``automation_triggered`` event.

    The entity registry lookup happens here, on the event loop — a plain
    in-memory dict read, not I/O, so it's safe and fast — never from the
    background thread the SQLite write itself runs on. Every automation in
    the house fires this event, not just Nova's, so a miss (no matching
    trial row) is the normal case, not a failure.
    """
    entity_id = event.data.get("entity_id")
    if not entity_id:
        return
    time_fired = getattr(event, "time_fired", None)
    ts = time_fired.timestamp() if time_fired is not None else time.time()

    unique_id = None
    try:
        from homeassistant.helpers import entity_registry as er
        ent = er.async_get(hass).async_get(entity_id)
        unique_id = ent.unique_id if ent else None
    except Exception:
        unique_id = None

    try:
        await hass.async_add_executor_job(record_run, entity_id, unique_id, ts)
    except Exception:
        pass
