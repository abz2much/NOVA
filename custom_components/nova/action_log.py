"""Nova — Action Audit Log.

Records actions Nova genuinely attempted or performed on the user's behalf
(device controls, bulk controls, scene/script/automation execution, safety
routines, suggested-automation installation, notifications) so the panel can
show what Nova actually did, distinct from what it merely said or reasoned
about. Read-only from the panel's side — no retry/replay/approve/reject
control lives here or is added by this module.

Storage: a dedicated table in the existing conversations database
(database.py's own conversations/activity_log store), the same shape
spoken_history.py and decision_record.py already establish — every entry
point takes ``db_path`` resolved at call time, so tests run fully isolated.

Ownership boundary (the one rule every caller follows): the top-level
component that understands the user's or system's intended action generates
one ``request_id`` and creates the row(s) for it. A lower-level helper that
performs supporting work for that same request (a verification retry, a
confirmation prompt, an automation-reload service call, a volume adjustment
for an announcement) never creates its own row — it either updates the
existing row via its ``action_id``, or does nothing here at all. A helper
that can ALSO be invoked as a genuine top-level action on its own accepts an
optional ``request_id``: reuse it when supplied, generate a fresh one only
when acting as its own entry point.

Every SQLite operation here is a plain synchronous function doing blocking
I/O directly — callers wrap every call in ``hass.async_add_executor_job``
(directly, or via a lambda when keyword arguments are needed), the same
established pattern ``spoken_history.record`` already uses. Never raises:
a logging failure must never alter, delay beyond its own short bounded
timeout, or mask the real action it describes. ``start()``/``start_many()``
return ``None``/``{}`` on any failure; every mutator no-ops safely when
given ``action_id is None`` or a row that has already reached a terminal
state — the exact idiom ``decision_record.set_outcome`` already establishes
for "duplicate callbacks must not create duplicate updates."

Database timeout: this module uses its own short ``busy_timeout`` (250ms),
deliberately shorter than the 10000ms every other Nova SQLite module uses.
A logging write sits in the same await chain as the device action it
describes, so a long wait here is directly user-visible latency on a
device-control path — see module docstring section below and the v3/v4
design report for the full reasoning. A write that can't get a lock within
250ms is abandoned (fail-open), not retried and not waited out further.
"""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

# Overridden by configure(hass) with the instance's own reported config
# directory — this default only applies before configure() has run (e.g.
# isolated unit tests that never call it), mirroring spoken_history.py.
_DEFAULT_DB = "/config/nova/conversations.db"

# Short and bounded on purpose — see module docstring. Not the 10000ms every
# other Nova SQLite module uses; this table sits in a device-control await
# chain, where a long wait is directly user-visible.
_BUSY_TIMEOUT_MS = 250

# Retention is group-aware (v3 correction): pruning by raw row count could
# delete some rows of a bulk action while leaving others. Keep the newest
# N distinct requests instead, dropping every row of any older request as a
# whole. This is a product limit, not a guaranteed window of time covered —
# a day full of large bulk operations can exhaust it well before the day
# ends; a quiet day keeps far more than a day's worth.
_KEEP_REQUESTS = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id            TEXT    NOT NULL,
    ts_created            REAL    NOT NULL,
    ts_updated            REAL    NOT NULL,
    action                TEXT    NOT NULL,
    source                TEXT    NOT NULL,
    requested_by_user_id  TEXT,
    requested_by_name     TEXT,
    request_device_id     TEXT,
    domain                TEXT,
    service               TEXT,
    entity_id             TEXT,
    requested_state       TEXT,
    approval_required     INTEGER NOT NULL DEFAULT 0,
    approval_result       TEXT    NOT NULL DEFAULT 'not_required',
    execution_result      TEXT    NOT NULL DEFAULT 'pending',
    reason_code           TEXT,
    reason_text           TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_log_keyset  ON action_log(ts_created, id);
CREATE INDEX IF NOT EXISTS idx_action_log_request ON action_log(request_id);
"""

# ── Permitted transitions (two independent state machines) ──────────────────
# Each maps a NEW value to the set of prior values it may legally come from.
# A value not present in a map (e.g. "not_required", "not_requested",
# "pending") is only ever written at row-creation time, never by a mutator.
_APPROVAL_ALLOWED_PRIOR = {
    # The one mutator transition INTO "awaiting" — see mark_awaiting_approval(),
    # a thin wrapper over the shared _update() core below, not a separate
    # state machine. A caller creates the row as "not_required" before
    # knowing whether the action turns out to be protected at all (the
    # corrected creation order: create the row, then run confirm_gate, then
    # update), so "awaiting" is reached by mutation exactly once here.
    "awaiting":     ("not_required",),
    "approved":     ("awaiting",),
    "rejected":     ("awaiting",),
    "expired":      ("awaiting",),
    "deferred":     ("awaiting",),
    "error":        ("awaiting",),
    # confirm_gate's own answer can be "not protected", which is a legitimate
    # exit from "awaiting", not a re-entry into it.
    "not_required": ("awaiting",),
}
_EXECUTION_ALLOWED_PRIOR = {
    "blocked":    ("pending",),
    "accepted":   ("pending",),
    "failed":     ("pending",),
    "verified":   ("accepted",),
    "unverified": ("accepted",),
}


def configure(hass) -> None:
    """Point the default db path at this Home Assistant instance's own
    config directory. Call once, early in async_setup_entry, before any
    action_log access — mirrors spoken_history.py's own configure()."""
    global _DEFAULT_DB
    _DEFAULT_DB = hass.config.path("nova", "conversations.db")


def new_request_id() -> str:
    """A fresh request_id for a top-level action boundary. Pure, no I/O —
    safe to call directly on the event loop. Callers generate this ONCE per
    logical user/system request and pass it to start()/start_many(), and to
    any nested helper that accepts an optional request_id (ownership rule,
    see module docstring)."""
    return uuid.uuid4().hex


def _resolve(db_path: Optional[str]) -> str:
    return db_path or _DEFAULT_DB


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _prune(conn: sqlite3.Connection, keep_requests: Optional[int] = None) -> None:
    """Group-aware retention: keep the newest N distinct requests, deleting
    every row of any older request as a whole — never a partial request.
    Run inside the same transaction as the insert that triggered it.

    `keep_requests` defaults to the module-level _KEEP_REQUESTS, read at
    CALL time (not bound as a default-parameter value at import time) so
    tests can monkeypatch the module attribute and have it actually take
    effect."""
    if keep_requests is None:
        keep_requests = _KEEP_REQUESTS
    conn.execute(
        "DELETE FROM action_log WHERE request_id NOT IN ("
        "  SELECT request_id FROM ("
        "    SELECT request_id, MIN(ts_created) AS group_ts"
        "    FROM action_log GROUP BY request_id"
        "    ORDER BY group_ts DESC LIMIT ?"
        "  )"
        ")",
        (keep_requests,),
    )


def start(
    request_id: str,
    action: str,
    source: str,
    *,
    requested_by_user_id: Optional[str] = None,
    requested_by_name: Optional[str] = None,
    request_device_id: Optional[str] = None,
    domain: Optional[str] = None,
    service: Optional[str] = None,
    entity_id: Optional[str] = None,
    requested_state: Optional[str] = None,
    approval_required: bool = False,
    approval_result: str = "not_required",
    execution_result: str = "pending",
    reason_code: Optional[str] = None,
    reason_text: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Optional[int]:
    """Insert one action row for a single-target action. Returns its id, or
    None on any failure — the action this describes continues either way.
    A thin, single-target wrapper over start_many() — same insert, same
    connect/commit/retention machinery, exactly one target."""
    ids = start_many(
        request_id, action, source,
        [{
            "domain": domain, "service": service, "entity_id": entity_id,
            "requested_state": requested_state,
            "approval_required": approval_required,
            "approval_result": approval_result,
            "execution_result": execution_result,
            "reason_code": reason_code, "reason_text": reason_text,
        }],
        requested_by_user_id=requested_by_user_id,
        requested_by_name=requested_by_name,
        request_device_id=request_device_id,
        db_path=db_path,
    )
    return next(iter(ids.values()), None)


def start_many(
    request_id: str,
    action: str,
    source: str,
    targets: list[dict[str, Any]],
    *,
    requested_by_user_id: Optional[str] = None,
    requested_by_name: Optional[str] = None,
    request_device_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[Any, int]:
    """Insert every target row for one grouped request (bulk control, an
    execution plan, a lockdown sweep, a routine run) in ONE transaction, and
    apply retention once for the whole request, not once per target.

    `targets` is a list of per-target dicts. Each may set any of: `key`
    (the value used as this target's key in the returned mapping — an
    entity_id or a step index; defaults to the target's own `entity_id` if
    `key` is omitted), `domain`, `service`, `entity_id`, `requested_state`,
    `approval_required`, `approval_result`, `execution_result`,
    `reason_code`, `reason_text`.

    Returns {key: row_id} for every target that was successfully inserted.
    On any failure (connect, transaction, or partial insert), returns {}
    unchanged and the caller's real action continues — this function never
    raises and is never awaited-per-target; the whole batch is one bounded
    executor call, not N.
    """
    if not targets:
        return {}
    db = _resolve(db_path)
    now = time.time()
    try:
        conn = _connect(db)
    except Exception as exc:
        _LOGGER.warning("Nova action_log: connect failed (start_many): %s", exc)
        return {}
    try:
        ids: dict[Any, int] = {}
        for i, t in enumerate(targets):
            key = t.get("key", t.get("entity_id", i))
            cur = conn.execute(
                "INSERT INTO action_log "
                "(request_id, ts_created, ts_updated, action, source, "
                " requested_by_user_id, requested_by_name, request_device_id, "
                " domain, service, entity_id, requested_state, "
                " approval_required, approval_result, execution_result, "
                " reason_code, reason_text) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id, now, now, action, source,
                    requested_by_user_id, requested_by_name, request_device_id,
                    t.get("domain"), t.get("service"), t.get("entity_id"),
                    t.get("requested_state"),
                    1 if t.get("approval_required") else 0,
                    t.get("approval_result", "not_required"),
                    t.get("execution_result", "pending"),
                    t.get("reason_code"), t.get("reason_text"),
                ),
            )
            ids[key] = int(cur.lastrowid)
        _prune(conn)
        conn.commit()
        return ids
    except Exception as exc:
        _LOGGER.warning("Nova action_log: start_many failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _update(
    action_id: Optional[int],
    column: str,
    new_value: str,
    allowed_prior: dict[str, tuple],
    *,
    reason_code: Optional[str],
    reason_text: Optional[str],
    db_path: Optional[str],
    approval_required: Optional[bool] = None,
) -> bool:
    """Shared conditional-update core for set_approval/set_execution. Only
    applies when the row is currently in one of `new_value`'s permitted
    prior states — a late or duplicate callback that arrives after the row
    is already terminal silently no-ops (rowcount 0), never overwrites.

    `approval_required` (set_approval only): the row is created before it's
    known whether the action actually turned out to be protected (see the
    corrected creation order in the module docstring), so the final,
    resolved value is settled here, alongside approval_result, in the same
    guarded UPDATE — not a separate state machine, just a plain flag that
    settles once."""
    if action_id is None:
        return False
    priors = allowed_prior.get(new_value)
    if not priors:
        _LOGGER.warning("Nova action_log: unknown %s value %r", column, new_value)
        return False
    db = _resolve(db_path)
    try:
        conn = _connect(db)
    except Exception as exc:
        _LOGGER.warning("Nova action_log: connect failed (update): %s", exc)
        return False
    try:
        placeholders = ",".join("?" for _ in priors)
        approval_required_sql = (
            "approval_required = ?, " if approval_required is not None else ""
        )
        params: list = [new_value]
        if approval_required is not None:
            params.append(1 if approval_required else 0)
        params.append(time.time())
        params.extend([reason_code, reason_text, action_id, *priors])
        cur = conn.execute(
            f"UPDATE action_log SET {column} = ?, {approval_required_sql}"
            f"ts_updated = ?, "
            f"reason_code = COALESCE(?, reason_code), "
            f"reason_text = COALESCE(?, reason_text) "
            f"WHERE id = ? AND {column} IN ({placeholders})",
            params,
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception as exc:
        _LOGGER.warning("Nova action_log: update failed: %s", exc)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def set_approval(
    action_id: Optional[int],
    approval_result: str,
    *,
    approval_required: Optional[bool] = None,
    reason_code: Optional[str] = None,
    reason_text: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Move a row from 'awaiting' to a TERMINAL approval_result (approved/
    rejected/expired/deferred/error/not_required) — exactly once. Returns
    True if this call applied the change, False if the row was missing,
    already resolved, or `action_id` is None. Pass `approval_required` to
    settle that flag in the same update (e.g. once confirm_gate reveals
    the action wasn't actually protected).

    To move a freshly-created row INTO 'awaiting' before running
    confirm_gate, use mark_awaiting_approval() instead — 'awaiting' is
    never a legal target of this function (it's the starting point these
    terminal transitions leave FROM, never a value written back to)."""
    return _update(
        action_id, "approval_result", approval_result,
        _APPROVAL_ALLOWED_PRIOR,
        reason_code=reason_code, reason_text=reason_text, db_path=db_path,
        approval_required=approval_required,
    )


def mark_awaiting_approval(action_id: Optional[int], db_path: Optional[str] = None) -> bool:
    """Move a freshly-created row from its 'not_required' default into
    'awaiting', for the corrected creation order: create the row before
    knowing whether the action turns out to be protected, then mark it
    awaiting right before running confirm_gate, then resolve it to a
    terminal value once confirm_gate returns (via set_approval). Thin
    wrapper over the same guarded-UPDATE core set_approval/set_execution
    use — the WHERE ... IN (...) guard makes a second call safely no-op."""
    return _update(
        action_id, "approval_result", "awaiting", _APPROVAL_ALLOWED_PRIOR,
        reason_code=None, reason_text=None, db_path=db_path,
        approval_required=True,
    )


def set_execution(
    action_id: Optional[int],
    execution_result: str,
    *,
    reason_code: Optional[str] = None,
    reason_text: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Move a row's execution_result forward one permitted step (pending ->
    blocked/accepted/failed, or accepted -> verified/unverified). Used both
    for the immediate execution outcome and, later, for an async
    verification result landing on the same row — the guard makes a late or
    duplicate verification callback safely no-op once the row is already
    terminal."""
    return _update(
        action_id, "execution_result", execution_result,
        _EXECUTION_ALLOWED_PRIOR,
        reason_code=reason_code, reason_text=reason_text, db_path=db_path,
    )


def _aggregate_status(targets: list[dict]) -> str:
    """Derived, panel-facing rollup for one request's targets — kept
    separate from each target's own approval_result/execution_result so a
    mixed bulk outcome renders as 'partial', never as a false full success
    or full failure."""
    if not targets:
        return "pending"
    if any(t["approval_result"] == "awaiting" for t in targets):
        return "awaiting"
    execs = {t["execution_result"] for t in targets}
    good = {"verified", "accepted"}
    bad = {"blocked", "failed"}
    if execs <= good:
        return "success"
    if execs <= bad:
        return "failed"
    return "partial"


def page_requests(
    limit: int = 20,
    cursor_ts: Optional[float] = None,
    cursor_request_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Request-level, keyset-paginated read: one page of COMPLETE request
    groups, newest first, never splitting one request's target rows across
    two pages. Raises on a genuine connect/schema/read failure so the
    websocket handler can report a distinct error state — same convention
    as spoken_history.list_recent/decision_record.page.
    """
    limit = max(1, min(int(limit), 100))
    db = _resolve(db_path)
    conn = _connect(db)
    try:
        if cursor_ts is None or cursor_request_id is None:
            group_rows = conn.execute(
                "SELECT request_id, MIN(ts_created) AS group_ts "
                "FROM action_log GROUP BY request_id "
                "ORDER BY group_ts DESC, request_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            group_rows = conn.execute(
                "SELECT request_id, group_ts FROM ("
                "  SELECT request_id, MIN(ts_created) AS group_ts"
                "  FROM action_log GROUP BY request_id"
                ") WHERE (group_ts, request_id) < (?, ?) "
                "ORDER BY group_ts DESC, request_id DESC LIMIT ?",
                (cursor_ts, cursor_request_id, limit),
            ).fetchall()

        request_order = [r["request_id"] for r in group_rows]
        if not request_order:
            return {"requests": [], "next_cursor": None}

        placeholders = ",".join("?" for _ in request_order)
        rows = conn.execute(
            f"SELECT * FROM action_log WHERE request_id IN ({placeholders}) "
            f"ORDER BY id ASC",
            tuple(request_order),
        ).fetchall()
    finally:
        conn.close()

    by_request: dict[str, list[dict]] = {rid: [] for rid in request_order}
    for row in rows:
        d = _row_to_dict(row)
        by_request.setdefault(d["request_id"], []).append(d)

    requests = []
    for rid in request_order:
        targets = by_request.get(rid, [])
        if not targets:
            continue
        head = targets[0]
        requests.append({
            "request_id": rid,
            "ts_created": min(t["ts_created"] for t in targets),
            "action": head["action"],
            "source": head["source"],
            "requested_by_user_id": head["requested_by_user_id"],
            "requested_by_name": head["requested_by_name"],
            "request_device_id": head["request_device_id"],
            "status": _aggregate_status(targets),
            "targets": targets,
        })

    next_cursor = None
    if len(group_rows) == limit:
        last = group_rows[-1]
        next_cursor = {"ts": last["group_ts"], "request_id": last["request_id"]}

    return {"requests": requests, "next_cursor": next_cursor}


def get_request(request_id: str, db_path: Optional[str] = None) -> Optional[dict]:
    """All rows for one request_id, or None if it has none. Raises on a
    genuine connect/schema failure (same convention as page_requests)."""
    db = _resolve(db_path)
    conn = _connect(db)
    try:
        rows = conn.execute(
            "SELECT * FROM action_log WHERE request_id = ? ORDER BY id ASC",
            (request_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    targets = [_row_to_dict(r) for r in rows]
    head = targets[0]
    return {
        "request_id": request_id,
        "ts_created": min(t["ts_created"] for t in targets),
        "action": head["action"],
        "source": head["source"],
        "requested_by_user_id": head["requested_by_user_id"],
        "requested_by_name": head["requested_by_name"],
        "request_device_id": head["request_device_id"],
        "status": _aggregate_status(targets),
        "targets": targets,
    }
