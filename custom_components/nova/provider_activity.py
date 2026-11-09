"""Provider activity (Phase 5): daily totals, kept to a bound, of the LLM calls
Nova makes.

providers.activity.execute_chat() records every chat call. The aim is narrow.
It answers how much each provider, model and role is used, from where and how
successfully, and never what was asked or answered.

The privacy limit comes from this module's own signature and not from policy.
record() takes only scalar identifiers and counters: provider, model, role,
location, data_category, success, two optional token counts and one latency.
No parameter could carry a prompt, a response, a tool argument, an image, an
entity state, a credential or the provider's raw response object, and
providers.activity is the only caller (a static test checks that).

There is one row for each (day, provider, model, role, location,
data_category). Calls add to the same row with an upsert, so the table grows
with that number of combinations and not with the number of calls. It needs
only SQLite and the standard library. Every entry point takes a db_path that is
resolved when it is called, so tests can use their own file, in the same way
as decision_record.py and automation_trials.py.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

from . import paths
from .persistence import sqlite as _store

_DEFAULT_DB: Optional[str] = None  # override; None resolves via paths.py


def _default_db() -> str:
    return _DEFAULT_DB or paths.nova_path("provider_activity.db")


_SCHEMA_LOCK = threading.Lock()


def _resolve(db_path: Optional[str]) -> str:
    return db_path or _default_db()


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
    conn = _store.connect(db_path, timeout=10.0, wal=False)
    # WAL negotiation and first-time schema creation themselves take a write
    # lock. Serialize only that tiny setup boundary; the aggregate UPSERTs
    # remain concurrent and are protected by SQLite's own busy timeout.
    with _SCHEMA_LOCK:
        conn.execute("PRAGMA journal_mode=WAL")
        _store.ensure(conn, "provider_activity")
    return conn


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


_UPSERT = """
INSERT INTO provider_activity_daily
  (day, provider, model, role, location, data_category,
   success_count, failure_count, input_tokens, output_tokens,
   latency_ms_sum, call_count)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
ON CONFLICT (day, provider, model, role, location, data_category) DO UPDATE SET
  success_count = success_count + excluded.success_count,
  failure_count = failure_count + excluded.failure_count,
  input_tokens = CASE
    WHEN input_tokens IS NULL AND excluded.input_tokens IS NULL THEN NULL
    ELSE COALESCE(input_tokens, 0) + COALESCE(excluded.input_tokens, 0) END,
  output_tokens = CASE
    WHEN output_tokens IS NULL AND excluded.output_tokens IS NULL THEN NULL
    ELSE COALESCE(output_tokens, 0) + COALESCE(excluded.output_tokens, 0) END,
  latency_ms_sum = latency_ms_sum + excluded.latency_ms_sum,
  call_count = call_count + excluded.call_count
"""


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
    bucket_day = day or _today()
    try:
        conn = _connect(_resolve(db_path))
    except Exception:
        return False
    try:
        conn.execute(_UPSERT, (
            bucket_day, str(provider), str(model), str(role), str(location),
            str(data_category), 1 if success else 0, 0 if success else 1,
            input_tokens, output_tokens, int(latency_ms),
        ))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        _close(conn)


def list_days(days: int = 7, db_path: Optional[str] = None) -> list:
    """Aggregates for the last `days` calendar days, most recent first, with
    per-bucket averages computed at read time from the stored sums — nothing
    beyond the sums themselves is persisted. Pure DB read; never raises."""
    path = _resolve(db_path)
    if not Path(path).exists():
        return []
    try:
        conn = _connect(path)
    except Exception:
        return []
    try:
        first_day = datetime.now() - timedelta(days=max(1, int(days)) - 1)
        rows = conn.execute(
            "SELECT * FROM provider_activity_daily WHERE day >= ? ORDER BY day DESC",
            (first_day.strftime("%Y-%m-%d"),),
        ).fetchall()
    except Exception:
        return []
    finally:
        _close(conn)

    grouped: dict = {}
    for row in rows:
        values = dict(row)
        calls = values["call_count"] or 0
        tokens_in, tokens_out = values["input_tokens"], values["output_tokens"]
        grouped.setdefault(values["day"], []).append({
            "provider": values["provider"],
            "model": values["model"],
            "role": values["role"],
            "location": values["location"],
            "data_category": values["data_category"],
            "success_count": values["success_count"],
            "failure_count": values["failure_count"],
            "call_count": calls,
            "avg_input_tokens": (round(tokens_in / calls, 1)
                                 if tokens_in is not None and calls else None),
            "avg_output_tokens": (round(tokens_out / calls, 1)
                                  if tokens_out is not None and calls else None),
            "avg_latency_ms": round(values["latency_ms_sum"] / calls, 1) if calls else None,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
        })

    return [{"day": name, "entries": entries}
            for name, entries in sorted(grouped.items(), reverse=True)]


# ── Estimated cost ──────────────────────────────────────────────────────────
# Prices are the user's own (per million tokens, in their own currency):
# provider prices change, so Nova ships none and never guesses. A model with
# no price shows no cost.

def parse_prices(raw) -> dict:
    """The ``ai_prices`` setting as {name: {"input": x, "output": y}}, from a
    dict or JSON text. A row that is not two non-negative numbers is left
    out; anything unreadable gives {}."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    prices: dict = {}
    for name, row in raw.items():
        if not isinstance(row, dict):
            continue
        try:
            per_in, per_out = float(row["input"]), float(row["output"])
        except (KeyError, TypeError, ValueError):
            continue
        if per_in >= 0 and per_out >= 0:
            prices[str(name)] = {"input": per_in, "output": per_out}
    return prices


def add_costs(days: list, prices: dict) -> list:
    """Each entry gets ``cost``: tokens times the price for its
    ``provider/model``, else its bare model name. None when the model has no
    price or reported no usage. A side that reported nothing counts as 0."""
    for day in days:
        for entry in day.get("entries", []):
            price = (prices.get(f"{entry.get('provider')}/{entry.get('model')}")
                     or prices.get(str(entry.get("model"))))
            tokens_in, tokens_out = entry.get("input_tokens"), entry.get("output_tokens")
            if not price or (tokens_in is None and tokens_out is None):
                entry["cost"] = None
                continue
            entry["cost"] = round(((tokens_in or 0) * price["input"]
                                   + (tokens_out or 0) * price["output"]) / 1_000_000, 4)
    return days


def report(days: int, raw_prices, currency, db_path: Optional[str] = None) -> dict:
    """What the panel shows: the last ``days`` days with each entry's
    estimated cost, and the currency symbol the user priced in (up to 8
    characters, "$" when none is set). Never raises."""
    symbol = currency.strip()[:8] if isinstance(currency, str) else ""
    return {"days": add_costs(list_days(days, db_path=db_path), parse_prices(raw_prices)),
            "currency": symbol or "$"}
