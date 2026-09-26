"""Nova Automation Trials (Phase 3) — probation tracking for suggestions that
became live automations.

Installing a suggestion means the user ACCEPTED it — the suggestion store's own
Decision Record outcome already reflects that (`decision_record.set_outcome_
by_ref("suggestion:N", "good", "installed")`). This table answers a
materially different question: does the automation actually RUN, and —
only ever via explicit manual feedback — does the person living with it
think it works? The two are kept structurally separate; nothing here is
ever inferred from installation alone.

Run counts come from Home Assistant's own ``automation_triggered`` event —
the one documented, supported mechanism for observing that an automation
fired (homeassistant/components/automation/__init__.py). No causality is
drawn beyond "this automation ran": correlating a subsequent device state's
context.parent_id to infer whether the automation did what it was *meant*
to do is explicitly out of scope for this phase.

``automation_entity_id`` starts NULL at install time and is resolved lazily,
on the event loop (never from a background thread — see
``async_handle_triggered``), the first time that automation genuinely fires.
There is no periodic resolution sweep; resolution is purely event-driven.

Dependency-light (SQLite + stdlib only); every entry point takes ``db_path``
resolved at call time, so tests run fully isolated — same shape as
decision_record.py, targeting the same shared ``patterns.db`` file other
Nova modules already read and write.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

_DEFAULT_DB = "/config/nova/patterns.db"


def _resolve(db_path: Optional[str]) -> str:
    return db_path or _DEFAULT_DB


def _connect(db_path: str) -> sqlite3.Connection:
    from ..persistence import sqlite as _store   # lazy: package layering rule
    conn = _store.connect(db_path)
    _store.ensure(conn, "automation_trials")  # idempotent — adds the table without
                                              # touching patterns.db's other tables
    return conn


def create(
    suggestion_id: int,
    automation_id: str,
    installed_at: Optional[float] = None,
    db_path: Optional[str] = None,
) -> Optional[int]:
    """Insert one trial row for a newly-installed automation. Best-effort by
    design — a logging failure must never break the installation it's
    recording. Returns the new row id, or None on failure."""
    db = _resolve(db_path)
    try:
        conn = _connect(db)
    except Exception:
        return None
    try:
        cur = conn.execute(
            "INSERT INTO automation_trials "
            "(suggestion_id, automation_id, automation_entity_id, installed_at, run_count) "
            "VALUES (?, ?, NULL, ?, 0)",
            (
                int(suggestion_id),
                str(automation_id),
                float(installed_at) if installed_at is not None else time.time(),
            ),
        )
        conn.commit()
        rowid = cur.lastrowid
        return int(rowid) if rowid is not None else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
    db = _resolve(db_path)
    try:
        conn = _connect(db)
    except Exception:
        return False
    try:
        cur = conn.execute(
            "UPDATE automation_trials SET run_count = run_count + 1, last_run = ?, "
            "automation_entity_id = ? "
            "WHERE automation_entity_id = ? "
            "   OR (automation_entity_id IS NULL AND automation_id = ?)",
            (
                float(ts) if ts is not None else time.time(),
                automation_entity_id,
                automation_entity_id,
                automation_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_trials(db_path: Optional[str] = None) -> list:
    """Most-recently-installed first. Pure DB read; never raises."""
    db = _resolve(db_path)
    if not Path(db).exists():
        return []
    try:
        conn = _connect(db)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM automation_trials ORDER BY installed_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
    db = _resolve(db_path)
    try:
        conn = _connect(db)
    except Exception:
        return False
    try:
        cur = conn.execute(
            "UPDATE automation_trials SET manual_outcome = ?, manual_outcome_ts = ? WHERE id = ?",
            (str(verdict), float(ts) if ts is not None else time.time(), int(trial_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
