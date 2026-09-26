"""Nova Provider Activity (Phase 5) — bounded daily aggregates of LLM calls.

Recorded by providers.activity.execute_chat() for every chat call Nova
makes. Deliberately narrow: this answers "how much is each provider/model/role
actually being used, from where, and how successfully" — never "what was
asked or answered."

Hard privacy boundary, enforced by this module's own function signature, not
by policy: record() accepts only scalar identifiers and counters (provider,
model, role, location, data_category, success, two optional token ints, one
latency int). There is no parameter through which a prompt, response, tool
argument, image, entity state, credential, or the provider's raw response
object could be passed — and providers.activity is the only caller (a
static test enforces that).

One row per (day, provider, model, role, location, data_category) — calls
accumulate into the same row via an upsert, so growth is bounded by that
cardinality, not by call volume. Dependency-light (SQLite + stdlib only);
every entry point takes db_path resolved at call time for isolated testing,
same shape as decision_record.py / automation_trials.py.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_DEFAULT_DB = "/config/nova/provider_activity.db"
_SCHEMA_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_activity_daily (
    day            TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    role           TEXT NOT NULL,
    location       TEXT NOT NULL,
    data_category  TEXT NOT NULL,
    success_count  INTEGER NOT NULL DEFAULT 0,
    failure_count  INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    latency_ms_sum INTEGER NOT NULL DEFAULT 0,
    call_count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider, model, role, location, data_category)
);
"""


def _resolve(db_path: Optional[str]) -> str:
    return db_path or _DEFAULT_DB


def db_path_for(hass) -> Optional[str]:
    """This Home Assistant instance's Provider Activity database, under its
    own config directory (nova/provider_activity.db, the same file as
    before on a standard install). None when `hass` has no config directory,
    so nothing is written outside one."""
    path = getattr(getattr(hass, "config", None), "path", None)
    if not callable(path):
        return None
    try:
        return str(path("nova", "provider_activity.db"))
    except Exception:
        return None


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    # WAL negotiation and first-time schema creation themselves take a write
    # lock. Serialize only that tiny setup boundary; the aggregate UPSERTs
    # remain concurrent and are protected by SQLite's own busy timeout.
    with _SCHEMA_LOCK:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
    return conn


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def record(
    provider: str,
    model: str,
    role: str,
    location: str,
    data_category: str,
    success: bool,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    latency_ms: int = 0,
    day: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Fold one call's outcome into today's aggregate row. Best-effort by
    design — a logging failure must never affect the call it's recording.

    A day/provider/model/role/location/data_category bucket's token sums
    stay NULL until at least one call in it actually reports usage; once any
    does, further calls without usage simply contribute 0 rather than
    resetting the sum to NULL — this matches decision_record.py's own
    "never crash the caller, degrade gracefully" philosophy.

    Uses a single atomic ``INSERT ... ON CONFLICT DO UPDATE`` rather than a
    separate UPDATE-then-INSERT-if-zero-rows: two concurrent calls to the
    same bucket (a real scenario — Nova can run several LLM calls close
    together) could otherwise both see zero rows updated and both attempt
    the INSERT, and the second would fail on the primary key, silently
    losing that call's contribution. One statement makes the accumulate
    atomic from SQLite's own perspective, so no write is ever lost or raises
    under concurrent access.
    """
    db = _resolve(db_path)
    d = day or _today()
    try:
        conn = _connect(db)
    except Exception:
        return False
    try:
        success_inc = 1 if success else 0
        failure_inc = 0 if success else 1
        conn.execute(
            "INSERT INTO provider_activity_daily "
            "(day, provider, model, role, location, data_category, "
            " success_count, failure_count, input_tokens, output_tokens, "
            " latency_ms_sum, call_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT (day, provider, model, role, location, data_category) DO UPDATE SET "
            "  success_count = success_count + excluded.success_count, "
            "  failure_count = failure_count + excluded.failure_count, "
            "  input_tokens = CASE "
            "    WHEN input_tokens IS NULL AND excluded.input_tokens IS NULL THEN NULL "
            "    ELSE COALESCE(input_tokens, 0) + COALESCE(excluded.input_tokens, 0) END, "
            "  output_tokens = CASE "
            "    WHEN output_tokens IS NULL AND excluded.output_tokens IS NULL THEN NULL "
            "    ELSE COALESCE(output_tokens, 0) + COALESCE(excluded.output_tokens, 0) END, "
            "  latency_ms_sum = latency_ms_sum + excluded.latency_ms_sum, "
            "  call_count = call_count + excluded.call_count",
            (
                d, str(provider), str(model), str(role), str(location), str(data_category),
                success_inc, failure_inc, input_tokens, output_tokens, int(latency_ms),
            ),
        )
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_days(days: int = 7, db_path: Optional[str] = None) -> list:
    """Aggregates for the last `days` calendar days, most recent first, with
    per-bucket averages computed at read time from the stored sums — nothing
    beyond the sums themselves is persisted. Pure DB read; never raises."""
    db = _resolve(db_path)
    if not Path(db).exists():
        return []
    try:
        conn = _connect(db)
    except Exception:
        return []
    try:
        cutoff = (datetime.now() - timedelta(days=max(1, int(days)) - 1)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM provider_activity_daily WHERE day >= ? ORDER BY day DESC",
            (cutoff,),
        ).fetchall()
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    by_day: dict = {}
    for r in rows:
        d = dict(r)
        count = d["call_count"] or 0
        entry = {
            "provider": d["provider"],
            "model": d["model"],
            "role": d["role"],
            "location": d["location"],
            "data_category": d["data_category"],
            "success_count": d["success_count"],
            "failure_count": d["failure_count"],
            "call_count": count,
            "avg_input_tokens": (round(d["input_tokens"] / count, 1)
                                 if d["input_tokens"] is not None and count else None),
            "avg_output_tokens": (round(d["output_tokens"] / count, 1)
                                  if d["output_tokens"] is not None and count else None),
            "avg_latency_ms": round(d["latency_ms_sum"] / count, 1) if count else None,
        }
        by_day.setdefault(d["day"], []).append(entry)

    return [{"day": day, "entries": entries}
            for day, entries in sorted(by_day.items(), reverse=True)]
