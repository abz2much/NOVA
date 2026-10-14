"""Goal planner: outcomes Nova keeps working toward until they are closed.

A goal carries a title, an outcome, a numbered plan, a progress log, a
re-engagement cadence, an optional deadline, a run counter and a last
result. The cognitive tick calls :func:`async_process_due`, which engages
each due goal through the headless agent runner with a prompt asking the
model to record progress via ``update_goal``. Goals stay quiet: only a goal
that ends (done or failed) produces an announce action. Safety valves are
an active-goal cap, a per-goal run budget, a re-engagement armed before each
run and forced closure once a deadline has passed. The CRUD helpers are
blocking SQLite calls for the executor and never raise.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from . import paths
from .persistence import sqlite as _store

_LOGGER = logging.getLogger(__name__)

DB_PATH: Optional[str] = None

MAX_ACTIVE = 10
MAX_RUNS = 120
DEFAULT_INTERVAL_MIN = 30
MAX_DEADLINE_MIN = 30 * 24 * 60
PROGRESS_TAIL = 5
STATUSES = ("active", "done", "failed", "cancelled")

_STEP_STATUSES = ("pending", "done", "failed", "skipped")
_STEP_MARKS = {"pending": " ", "done": "x", "failed": "!", "skipped": "-"}
_PROGRESS_KEEP = 50


def _db_path() -> str:
    """The store location, resolved on every call."""
    return DB_PATH if DB_PATH else paths.patterns_db()


def _open(db_path: Optional[str]) -> sqlite3.Connection:
    conn = _store.connect(db_path or _db_path(), mkdir=False)
    _store.ensure(conn, "goals")
    return conn


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now()


def _iso(moment: datetime) -> str:
    return moment.isoformat(sep=" ")


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def _decode_list(raw: Any) -> list:
    try:
        decoded = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) else []


def _as_goal(row: Any) -> dict:
    goal = dict(row)
    goal["steps"] = _decode_list(goal.get("steps"))
    goal["progress"] = _decode_list(goal.get("progress"))
    return goal


def _select(sql: str, args: tuple, db_path: Optional[str]) -> list[dict]:
    try:
        conn = _open(db_path)
        with conn:
            rows = conn.execute(sql, args).fetchall()
        return [_as_goal(row) for row in rows]
    except Exception:  # noqa: BLE001
        return []


def create(title: str, outcome: str, steps: Optional[list] = None, *,
           check_interval_min: float = DEFAULT_INTERVAL_MIN,
           deadline_minutes: Optional[float] = None,
           now: Optional[datetime] = None,
           db_path: Optional[str] = None) -> dict:
    """Open a new active goal, due for its first engagement immediately."""
    outcome = (outcome or "").strip()
    title = (title or outcome[:60] or "").strip()
    if not outcome:
        return {"error": "empty outcome"}

    try:
        interval = float(check_interval_min or DEFAULT_INTERVAL_MIN)
    except (TypeError, ValueError):
        interval = float(DEFAULT_INTERVAL_MIN)
    interval = _clamp(interval, 1, 1440)

    moment = _now(now)
    deadline_ts: Optional[str] = None
    if deadline_minutes is not None:
        try:
            span = _clamp(float(deadline_minutes), 1, MAX_DEADLINE_MIN)
            deadline_ts = _iso(moment + timedelta(minutes=span))
        except (TypeError, ValueError):
            deadline_ts = None

    plan: list[dict] = []
    for index, item in enumerate(steps or []):
        text = str(item).strip()
        if text:
            plan.append({"n": index + 1, "step": text,
                         "status": "pending", "note": ""})

    stamp = _iso(moment)
    try:
        conn = _open(db_path)
        with conn:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM goals WHERE status = 'active'"
            ).fetchone()
            cap = MAX_ACTIVE
            if count >= cap:
                return {"error": f"goal list is full ({cap} active)"}
            cur = conn.execute(
                "INSERT INTO goals (created_ts, updated_ts, title, outcome, steps, "
                "next_check_ts, check_interval_min, deadline_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (stamp, stamp, title, outcome, json.dumps(plan), stamp,
                 interval, deadline_ts),
            )
            new_id = cur.lastrowid
        return {"id": new_id, "title": title, "outcome": outcome, "steps": plan,
                "next_check_ts": stamp, "deadline_ts": deadline_ts}
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Nova goals: could not create goal: %s", exc)
        return {"error": str(exc)}


def get(goal_id: int, *, db_path: Optional[str] = None) -> Optional[dict]:
    """One goal by id, or None."""
    try:
        target = int(goal_id)
        conn = _open(db_path)
        with conn:
            row = conn.execute("SELECT * FROM goals WHERE id = ?",
                               (target,)).fetchone()
        return _as_goal(row) if row is not None else None
    except Exception:  # noqa: BLE001
        return None


def active(*, db_path: Optional[str] = None) -> list[dict]:
    """Active goals, next engagement first."""
    return _select(
        "SELECT * FROM goals WHERE status = 'active' "
        "ORDER BY datetime(next_check_ts) ASC",
        (), db_path,
    )


def due(*, now: Optional[datetime] = None,
        db_path: Optional[str] = None) -> list[dict]:
    """Active goals whose next engagement has arrived."""
    return _select(
        "SELECT * FROM goals WHERE status = 'active' "
        "AND datetime(next_check_ts) <= datetime(?) "
        "ORDER BY datetime(next_check_ts) ASC",
        (_iso(_now(now)),), db_path,
    )


def recent(*, limit: int = 20, db_path: Optional[str] = None) -> list[dict]:
    """Goals of any status, most recently touched first."""
    try:
        cap = int(_clamp(int(limit), 1, 100))
    except Exception:  # noqa: BLE001
        return []
    return _select(
        "SELECT * FROM goals ORDER BY datetime(updated_ts) DESC LIMIT ?",
        (cap,), db_path,
    )


def update(goal_id: int, *, status: Optional[str] = None,
           step_updates: Optional[list] = None, progress_note: Optional[str] = None,
           result: Optional[str] = None, next_check_minutes: Optional[float] = None,
           db_path: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """Apply the model's (or the user's) progress to a goal."""
    goal = get(goal_id, db_path=db_path)
    if goal is None:
        return {"error": f"no goal #{goal_id}"}
    moment = _now(now)
    try:
        plan = goal["steps"]
        for change in step_updates or []:
            wanted = int(change.get("n", 0))
            step = next((s for s in plan if s.get("n") == wanted), None)
            if step is None:
                continue
            if change.get("status") in _STEP_STATUSES:
                step["status"] = change["status"]
            if change.get("note"):
                step["note"] = str(change["note"])[:300]

        log = goal["progress"]
        if progress_note:
            log.append({"t": _iso(moment), "note": str(progress_note)[:500]})
            log = log[-_PROGRESS_KEEP:]

        fields: dict[str, Any] = {
            "updated_ts": _iso(moment),
            "steps": json.dumps(plan),
            "progress": json.dumps(log),
        }
        if next_check_minutes is not None:
            try:
                span = _clamp(float(next_check_minutes), 1, 10080)
                fields["next_check_ts"] = _iso(moment + timedelta(minutes=span))
            except (TypeError, ValueError):
                pass
        if status in STATUSES:
            fields["status"] = status
        if result is not None:
            fields["last_result"] = str(result)[:1000]

        assignments = ", ".join(f"{column} = ?" for column in fields)
        conn = _open(db_path)
        with conn:
            conn.execute(f"UPDATE goals SET {assignments} WHERE id = ?",
                         (*fields.values(), goal["id"]))
        return {"ok": True, "id": int(goal_id)}
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Nova goals: update of #%s failed: %s", goal_id, exc)
        return {"error": str(exc)}


def cancel(goal_id: int, *, db_path: Optional[str] = None) -> bool:
    """Cancel an active goal. True only when a row changed."""
    try:
        target = int(goal_id)
        conn = _open(db_path)
        with conn:
            cur = conn.execute(
                "UPDATE goals SET status = 'cancelled', updated_ts = ? "
                "WHERE id = ? AND status = 'active'",
                (_iso(datetime.now()), target),
            )
            return cur.rowcount > 0
    except Exception:  # noqa: BLE001
        return False


def delete(goal_id: int, *, db_path: Optional[str] = None) -> bool:
    """Remove a goal in any status. True only when a row was removed."""
    try:
        target = int(goal_id)
        conn = _open(db_path)
        with conn:
            cur = conn.execute("DELETE FROM goals WHERE id = ?", (target,))
            return cur.rowcount > 0
    except Exception:  # noqa: BLE001
        return False


def _arm(goal_id: int, when: datetime, runs: int, *,
         db_path: Optional[str] = None) -> None:
    """Set the next engagement time and run counter."""
    try:
        conn = _open(db_path)
        with conn:
            conn.execute(
                "UPDATE goals SET next_check_ts = ?, runs = ? WHERE id = ?",
                (_iso(when), runs, goal_id),
            )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova goals: arming #%s failed: %s", goal_id, exc)


def build_prompt(goal: dict, *, deadline_passed: bool = False) -> str:
    """The instruction handed to the runner for one engagement."""
    gid = goal["id"]
    title = goal["title"]
    outcome = goal["outcome"]
    plan = goal["steps"]
    log = goal["progress"]

    lines = [
        f"You are working on YOUR OWN goal #{gid}: {title}",
        f"OUTCOME to achieve: {outcome}",
    ]
    if goal.get("deadline_ts"):
        lines.append(f"Deadline: {goal['deadline_ts']}")
    if deadline_passed:
        lines.append(
            "THE DEADLINE HAS PASSED: wrap up now and close the goal "
            "(status 'done' or 'failed') with an honest result."
        )
    if plan:
        lines.append("Plan:")
        for step in plan:
            mark = _STEP_MARKS.get(step.get("status"), " ")
            line = f"  [{mark}] {step.get('n')}. {step.get('step')}"
            if step.get("note"):
                line += f" — {step['note']}"
            lines.append(line)
    if log:
        lines.append("Recent progress:")
        for entry in log[-PROGRESS_TAIL:]:
            lines.append(f"  {entry.get('t')}: {entry.get('note')}")
    lines.append(
        "Advance this goal now. Check the relevant states and verify "
        "rather than assume. A scheduled run can look and report but cannot "
        "control devices, so if something needs doing, say so in the note. "
        f"Before you finish you MUST call update_goal(goal_id={gid}, ...) "
        "exactly once: mark the step statuses, add a progress_note, and "
        "EITHER set next_check_minutes OR set status='done'/'failed' with a "
        "result the user will hear. If nothing needs doing yet, say so and "
        "set a sensible next check."
    )
    return "\n".join(lines)


def _finish_action(goal: Optional[dict]) -> dict:
    """Announce action for a goal that has ended."""
    goal = goal or {}
    title = goal.get("title", "")
    message = goal.get("last_result")
    if not message:
        if goal.get("status") == "done":
            message = f"Goal '{title}' is complete."
        else:
            message = f"Goal '{title}' is closed."
    return {"type": "goal", "urgency": "medium", "message": message,
            "auto_act": True}


async def async_process_due(
    hass: Any,
    config: dict,
    runner: Optional[Callable[[str, str], Awaitable[str]]] = None,
    *,
    now: Optional[datetime] = None,
    db_path: Optional[str] = None,
) -> list[dict]:
    """Engage every due goal once; return actions for goals that ended."""
    t = _now(now)
    try:
        todo = await hass.async_add_executor_job(
            lambda: due(now=t, db_path=db_path)
        )
    except Exception:  # noqa: BLE001
        todo = []
    if not todo or runner is None:
        return []

    def _reread(gid: int):
        return hass.async_add_executor_job(lambda: get(gid, db_path=db_path))

    def _apply(gid: int, **changes):
        return hass.async_add_executor_job(
            lambda: update(gid, now=t, db_path=db_path, **changes)
        )

    actions: list[dict] = []
    for goal in todo:
        gid = goal["id"]
        title = goal["title"]
        runs = (goal["runs"] or 0) + 1
        interval = float(goal["check_interval_min"] or 30)
        await hass.async_add_executor_job(
            lambda: _arm(gid, t + timedelta(minutes=interval), runs,
                         db_path=db_path)
        )

        if runs > MAX_RUNS:
            await _apply(
                gid,
                status="failed",
                result=(f"I've engaged this goal {MAX_RUNS} times without "
                        f"completing it, so I'm setting it aside: {title}"),
                progress_note="run budget exhausted",
            )
            actions.append(_finish_action(await _reread(gid)))
            continue

        deadline_ts = goal.get("deadline_ts")
        deadline_passed = bool(deadline_ts) and _iso(t) > str(deadline_ts)

        try:
            prompt = build_prompt(goal, deadline_passed=deadline_passed)
            result = ((await runner(prompt, "")) or "").strip()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Nova goal #%s run failed: %s", gid, exc)
            await _apply(gid, progress_note=f"run error: {exc}")
            continue

        latest = await _reread(gid)
        if latest is None:
            continue
        if deadline_passed and latest.get("status") == "active":
            await _apply(
                gid,
                status="failed",
                result=result or f"The deadline for '{title}' passed.",
                progress_note="deadline passed — force-closed",
            )
            latest = await _reread(gid)

        try:
            from . import database
            database.save_activity(
                category="goal",
                urgency="low",
                message=(f"Goal #{gid} '{title}' engaged (run {runs}): "
                         f"{(result or 'no report')[:160]}"),
                source="agent",
            )
        except Exception:  # noqa: BLE001
            pass

        if latest["status"] in ("done", "failed"):
            actions.append(_finish_action(latest))
    return actions
