"""Decision records (v7.32.0): one row for every proactive decision Nova makes.

An anticipation alert, an intrusion escalation and an automation suggestion
each leave a row holding, kept apart:

  * observation: the facts seen, with no judgement in them
  * interpretation: what Nova took them to mean
  * evidence: the signals it leaned on
  * decision and reason: what it did and why
  * model, tokens and latency: the model call, when there was one
  * confidence: how sure it was

A row never changes after it is written, except that its ``outcome`` may be
set once, later, from a real signal such as a dismissal or a false alarm.
That is what lets a decision be scored after the fact.

SQLite and the standard library only. Every entry point takes ``db_path``
(resolved when called) and swallows its own errors: writing the record must
never break the decision being recorded.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

from . import paths
from .persistence import sqlite as _store

_DEFAULT_DB: Optional[str] = None   # a test override; None means paths.py decides

# Outcome verdicts.
OUTCOME_GOOD = "good"                  # useful, acted on
OUTCOME_UNNECESSARY = "unnecessary"    # not wrong, just noise
OUTCOME_WRONG = "wrong"                # a false alarm
_VERDICTS = (OUTCOME_GOOD, OUTCOME_UNNECESSARY, OUTCOME_WRONG)

# Where a person confirmed an outcome (8.24.0). Learning (the interruption
# budget, adaptive awareness, how picky suggestions are) only reads outcomes
# from these; any other source is kept and shown, never learned from.
SOURCE_PHONE_RATING = "phone"               # Helpful or Not helpful on the phone
SOURCE_CALL_OFF = "dismiss_intrusion"       # a confirmed "it's a false alarm"
SOURCE_PANEL_LABEL = "panel_label"          # real or false on the Intrusion tab
SOURCE_SUGGESTION_ACCEPTED = "installed"
SOURCE_SUGGESTION_DISMISSED = "dismiss_suggestion"
CONFIRMED_SOURCES = frozenset((
    SOURCE_PHONE_RATING, SOURCE_CALL_OFF, SOURCE_PANEL_LABEL,
    SOURCE_SUGGESTION_ACCEPTED, SOURCE_SUGGESTION_DISMISSED))

_TABLE = "decision_records"
_JUDGE = ("UPDATE decision_records SET outcome = ?, outcome_ts = ?, outcome_source = ? "
          "WHERE id = ? AND outcome IS NULL")
_SUMMARY_COLUMNS = ("id, ts, kind, decision, reason, confidence, model, tokens, "
                    "latency_ms, outcome")


def _default_db() -> str:
    return _DEFAULT_DB or paths.nova_path("decisions.db")


def _resolve(db_path: Optional[str]) -> str:
    return db_path or _default_db()


def _confirmed_sql() -> tuple[str, list]:
    """A WHERE fragment limiting rows to confirmed sources, and its values."""
    sources = sorted(CONFIRMED_SOURCES)
    marks = ", ".join("?" for _ in sources)
    return f" AND outcome_source IN ({marks})", sources


def _connect(db_path: str) -> sqlite3.Connection:
    conn = _store.connect(db_path, busy_timeout_ms=None)
    _store.ensure(conn, _TABLE)   # each entry point creates the table if needed
    return conn


def _with_db(db_path: Optional[str], work: Callable, fallback):
    """work(conn) on an open database, or ``fallback`` when the database will
    not open or work raises. The connection is always closed."""
    try:
        conn = _connect(_resolve(db_path))
    except Exception:
        return fallback
    try:
        return work(conn)
    except Exception:
        return fallback
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _exists(db_path: Optional[str]) -> bool:
    """Reads that report on the store never create it."""
    return Path(_resolve(db_path)).exists()


def _js(value) -> str:
    """JSON text for a record part: {} for None or anything unserialisable."""
    if value is None:
        return "{}"
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return "{}"


def _number(cast, value):
    if value is None:
        return None
    try:
        return cast(value)
    except Exception:
        return None


def _int_or_none(value):
    return _number(int, value)


def _float_or_none(value):
    return _number(float, value)


def _now(ts) -> float:
    return time.time() if ts is None else float(ts)


def _row_to_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    for part in ("observation", "interpretation", "evidence"):
        try:
            out[part] = json.loads(out.get(part) or "{}")
        except Exception:
            out[part] = {}
    return out


def _where(kind: Optional[str], only_unjudged: bool) -> tuple[list, list]:
    clauses = ["kind = ?"] if kind else []
    params = [str(kind)] if kind else []
    if only_unjudged:
        clauses.append("outcome IS NULL")
    return clauses, params


def _joined(clauses: list) -> str:
    return " WHERE " + " AND ".join(clauses) if clauses else ""


def _verdict_counts(rows) -> dict:
    counts = dict.fromkeys(_VERDICTS, 0)
    for verdict, count in rows:
        if verdict in counts:
            counts[verdict] = int(count)
    return counts


# ── Writing ─────────────────────────────────────────────────────────────────

def ensure_schema(db_path: Optional[str] = None) -> None:
    """Create the table and its indexes now, so the first write is cheap.
    Optional: every call creates them anyway."""
    _with_db(db_path, lambda conn: None, None)


def record(kind: str, observation=None, interpretation=None, evidence=None,
           decision: str = "", reason: str = "", model: Optional[str] = None,
           tokens=None, latency_ms=None, confidence=None, ref: Optional[str] = None,
           ts: Optional[float] = None, db_path: Optional[str] = None) -> Optional[int]:
    """Write one decision and return its id, or None when it could not be
    written. Never raises."""
    def _insert(conn):
        values = (
            _now(ts), str(kind),
            _js(observation), _js(interpretation), _js(evidence),
            str(decision or ""), str(reason or ""),
            None if model is None else str(model),
            _int_or_none(tokens), _int_or_none(latency_ms), _float_or_none(confidence),
            None if ref is None else str(ref),
        )
        cur = conn.execute(
            "INSERT INTO decision_records (ts, kind, observation, interpretation, "
            "evidence, decision, reason, model, tokens, latency_ms, confidence, ref) "
            f"VALUES ({', '.join('?' * len(values))})", values)
        conn.commit()
        return int(cur.lastrowid)
    return _with_db(db_path, _insert, None)


def _apply_outcome(conn, rid, verdict, source, ts) -> bool:
    """Judge one row if it is still unjudged; True when it was."""
    changed = conn.execute(_JUDGE, (str(verdict), _now(ts), str(source or ""), int(rid)))
    conn.commit()
    return changed.rowcount > 0


def set_outcome(record_id: int, verdict: str, source: str = "",
                ts: Optional[float] = None, db_path: Optional[str] = None) -> bool:
    """Set a record's outcome: the only change a record ever takes, and only
    once. False when the record is missing or already judged."""
    if record_id is None:
        return False
    return _with_db(db_path,
                    lambda conn: _apply_outcome(conn, record_id, verdict, source, ts), False)


def set_outcome_checked(record_id: int, verdict: str, source: str = "",
                        ts: Optional[float] = None, db_path: Optional[str] = None) -> str:
    """set_outcome that says why it did not apply: "ok", "already_judged" or
    "not_found", so a caller can tell the two failures apart. The extra lookup
    only runs when the outcome was not set."""
    if record_id is None:
        return "not_found"

    def _judge(conn):
        if _apply_outcome(conn, record_id, verdict, source, ts):
            return "ok"
        found = conn.execute("SELECT 1 FROM decision_records WHERE id = ?",
                             (int(record_id),)).fetchone()
        return "already_judged" if found else "not_found"
    return _with_db(db_path, _judge, "not_found")


def _judge_newest(db_path, sql: str, params: tuple, verdict, source, ts) -> bool:
    def _judge(conn):
        hit = conn.execute(sql, params).fetchone()
        return bool(hit) and _apply_outcome(conn, hit[0], verdict, source, ts)
    return _with_db(db_path, _judge, False)


def set_outcome_by_ref(ref, verdict, source: str = "", ts: Optional[float] = None,
                       db_path: Optional[str] = None) -> bool:
    """Judge the newest unjudged record carrying ``ref`` (such as
    "suggestion:11"), linking a dismissal or an approval to its decision."""
    if not ref:
        return False
    return _judge_newest(
        db_path, "SELECT id FROM decision_records WHERE ref = ? AND outcome IS NULL "
                 "ORDER BY ts DESC LIMIT 1", (str(ref),), verdict, source, ts)


def set_outcome_recent(kind: str, verdict, source: str = "", max_age: float = 3600.0,
                       ts: Optional[float] = None, db_path: Optional[str] = None) -> bool:
    """Judge the newest unjudged record of ``kind`` from the last ``max_age``
    seconds, for decisions with no stable id (an intrusion just called off)."""
    def _judge(conn):
        oldest = time.time() - float(max_age)
        hit = conn.execute(
            "SELECT id FROM decision_records WHERE kind = ? AND outcome IS NULL "
            "AND ts >= ? ORDER BY ts DESC LIMIT 1", (str(kind), oldest)).fetchone()
        return bool(hit) and _apply_outcome(conn, hit[0], verdict, source, ts)
    return _with_db(db_path, _judge, False)


# ── Reading ─────────────────────────────────────────────────────────────────

def get(record_id: int, db_path: Optional[str] = None) -> Optional[dict]:
    def _one(conn):
        row = conn.execute("SELECT * FROM decision_records WHERE id = ?",
                           (int(record_id),)).fetchone()
        return _row_to_dict(row) if row else None
    return _with_db(db_path, _one, None)


def recent(limit: int = 50, kind: Optional[str] = None, only_unjudged: bool = False,
           db_path: Optional[str] = None) -> list:
    """Full records, newest first, optionally of one kind or still unjudged."""
    def _list(conn):
        clauses, params = _where(kind, only_unjudged)
        rows = conn.execute(
            f"SELECT * FROM decision_records{_joined(clauses)} ORDER BY ts DESC LIMIT ?",
            (*params, int(limit))).fetchall()
        return [_row_to_dict(row) for row in rows]
    return _with_db(db_path, _list, [])


def page(limit: int = 50, kind: Optional[str] = None, only_unjudged: bool = False,
         cursor_ts: Optional[float] = None, cursor_id: Optional[int] = None,
         db_path: Optional[str] = None) -> dict:
    """One page of decision summaries, newest first.

    Rows carry summary columns only (``get()`` has the parts). ``next_cursor``
    is the last row's ``ts`` and ``id``: pass them back as ``cursor_ts`` and
    ``cursor_id`` (both or neither) for the next page; None means the end.
    Pages are ordered and cut on the (ts, id) pair, so two records with the
    same time are never skipped or shown twice."""
    out = {"items": [], "next_cursor": None}

    def _page(conn):
        clauses, params = _where(kind, only_unjudged)
        if cursor_ts is not None and cursor_id is not None:
            clauses.append("(ts, id) < (?, ?)")
            params += [float(cursor_ts), int(cursor_id)]
        size = max(1, int(limit))
        rows = conn.execute(
            f"SELECT {_SUMMARY_COLUMNS} FROM decision_records{_joined(clauses)} "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (*params, size + 1)).fetchall()   # one spare row shows a next page
        out["items"] = [dict(row) for row in rows[:size]]
        if len(rows) > size:
            last = out["items"][-1]
            out["next_cursor"] = {"ts": last["ts"], "id": last["id"]}
        return out
    return _with_db(db_path, _page, out)


def stats(db_path: Optional[str] = None) -> dict:
    """How many records there are and how they were judged."""
    out = {"total": 0, "judged": 0, "good": 0, "unnecessary": 0, "wrong": 0}
    if not _exists(db_path):
        return out

    def _count(conn):
        def scalar(sql, *args):
            return conn.execute(sql, args).fetchone()[0]
        out["total"] = scalar("SELECT COUNT(*) FROM decision_records")
        for verdict in _VERDICTS:
            out[verdict] = scalar(
                "SELECT COUNT(*) FROM decision_records WHERE outcome = ?", verdict)
        out["judged"] = sum(out[verdict] for verdict in _VERDICTS)
        return out
    return _with_db(db_path, _count, out)


def calibration(db_path: Optional[str] = None, kind: Optional[str] = None,
                bins: int = 5, since: Optional[float] = None) -> dict:
    """How well stated confidence matched what happened, over judged records.

    A record counts as right (1.0) when judged good and wrong (0.0) otherwise.
    Confidences are clamped to [0, 1] and put in ``bins`` equal bins; each bin
    reports its mean confidence and how often it was right. Also returned:
    the overall good rate, the Brier score (mean squared gap between
    confidence and result; 0 is perfect) and the expected calibration error
    (the gap per bin, weighted by its share of records). Never raises."""
    count = max(1, int(bins))
    out = {"n": 0, "good_rate": None, "brier": None, "ece": None, "bins": [], "kind": kind}
    if not _exists(db_path):
        return out

    def _read(conn):
        sql = ("SELECT confidence, outcome FROM decision_records "
               "WHERE outcome IS NOT NULL AND confidence IS NOT NULL")
        params: list = []
        if kind:
            sql, params = sql + " AND kind = ?", params + [str(kind)]
        if since is not None:
            sql, params = sql + " AND ts >= ?", params + [float(since)]
        return conn.execute(sql, params).fetchall()
    rows = _with_db(db_path, _read, None)
    if rows is None:
        return out

    scored = []
    for confidence, verdict in rows:
        try:
            clamped = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            continue
        scored.append((clamped, 1.0 if verdict == OUTCOME_GOOD else 0.0))
    total = len(scored)
    out["n"] = total
    if not total:
        return out
    out["good_rate"] = round(sum(right for _, right in scored) / total, 4)
    out["brier"] = round(sum((conf - right) ** 2 for conf, right in scored) / total, 4)

    grouped: list = [[] for _ in range(count)]
    for conf, right in scored:
        grouped[min(count - 1, int(conf * count))].append((conf, right))
    gap = 0.0
    for index, members in enumerate(grouped):
        mean_conf = rate = None
        if members:
            mean_conf = sum(conf for conf, _ in members) / len(members)
            rate = sum(right for _, right in members) / len(members)
            gap += len(members) / total * abs(mean_conf - rate)
        out["bins"].append({
            "lo": round(index / count, 3),
            "hi": round((index + 1) / count, 3),
            "count": len(members),
            "mean_confidence": None if mean_conf is None else round(mean_conf, 3),
            "good_rate": None if rate is None else round(rate, 3),
        })
    out["ece"] = round(gap, 4)
    return out


def _confirmed_counts(db_path, sql: str, params: list):
    """Verdict counts for confirmed outcomes, or None when the read failed."""
    confirmed, sources = _confirmed_sql()
    query = sql + confirmed + " GROUP BY outcome"
    rows = _with_db(db_path, lambda conn: conn.execute(query, params + sources).fetchall(),
                    None)
    return None if rows is None else _verdict_counts(rows)


def interruption_budget(db_path: Optional[str] = None,
                        window_s: float = 86400.0, floor: float = 0.25) -> dict:
    """Whether Nova's recent interruptions were welcome.

    Over confirmed outcomes (CONFIRMED_SOURCES, 8.24.0) from the last
    ``window_s`` seconds, the share judged unnecessary or wrong sets a
    ``multiplier`` between ``floor`` and 1.0, which the announcement gate
    applies to its hourly cap. 1.0, no change, with no data. Never raises."""
    out = {"window_s": window_s, "judged": 0, "good": 0, "unnecessary": 0,
           "wrong": 0, "unwelcome_rate": None, "multiplier": 1.0,
           "assessment": "no data"}
    if not _exists(db_path):
        return out
    since = time.time() - float(window_s)
    counts = _confirmed_counts(
        db_path, "SELECT outcome, COUNT(*) FROM decision_records "
                 "WHERE outcome IS NOT NULL AND ts >= ?", [since])
    if counts is None:
        return out
    judged = sum(counts.values())
    out.update(counts, judged=judged)
    if not judged:
        return out
    unwelcome = (counts[OUTCOME_UNNECESSARY] + counts[OUTCOME_WRONG]) / judged
    lowest = max(0.0, min(1.0, float(floor)))
    out["unwelcome_rate"] = round(unwelcome, 4)
    out["multiplier"] = round(max(lowest, 1.0 - unwelcome * (1.0 - lowest)), 3)
    if unwelcome >= 0.5:
        out["assessment"] = "over-interrupting"
    elif unwelcome >= 0.25:
        out["assessment"] = "borderline"
    else:
        out["assessment"] = "healthy"
    return out


def threshold_delta_from_rate(unwelcome_rate) -> float:
    """How much to move a threshold for an unwelcome rate, in [-0.07, +0.15]:
    much stricter when most output was unwelcome, a little looser when nearly
    all of it landed well. Used by adaptive suggestions and awareness."""
    try:
        rate = float(unwelcome_rate or 0.0)
    except (TypeError, ValueError):
        return 0.0
    for at_least, delta in ((0.5, 0.15), (0.3, 0.07)):
        if rate >= at_least:
            return delta
    return -0.07 if rate <= 0.1 else 0.0


def outcome_rate(kind: str, window_s: Optional[float] = None,
                 db_path: Optional[str] = None, prefix: bool = False) -> dict:
    """Confirmed outcomes (8.24.0) for one ``kind``, optionally from the last
    ``window_s`` seconds; with ``prefix``, every kind starting with it
    ("anticipation" covers anticipation_overdue and the rest). Gives the
    counts, good_rate and unwelcome_rate ((unnecessary + wrong) / judged), so
    a proactive feature can steer by how its own output was received. Never
    raises."""
    out = {"kind": kind, "judged": 0, "good": 0, "unnecessary": 0, "wrong": 0,
           "good_rate": None, "unwelcome_rate": None}
    if not _exists(db_path):
        return out
    base = "SELECT outcome, COUNT(*) FROM decision_records WHERE outcome IS NOT NULL"
    try:
        if prefix:
            literal = str(kind)
            for special in ("\\", "%", "_"):
                literal = literal.replace(special, "\\" + special)
            sql, params = base + " AND kind LIKE ? ESCAPE '\\'", [literal + "%"]
        else:
            sql, params = base + " AND kind = ?", [str(kind)]
        if window_s is not None:
            sql += " AND ts >= ?"
            params.append(time.time() - float(window_s))
    except Exception:
        return out
    counts = _confirmed_counts(db_path, sql, params)
    if counts is None:
        return out
    judged = sum(counts.values())
    out.update(counts, judged=judged)
    if judged:
        out["good_rate"] = round(counts[OUTCOME_GOOD] / judged, 4)
        out["unwelcome_rate"] = round(
            (counts[OUTCOME_UNNECESSARY] + counts[OUTCOME_WRONG]) / judged, 4)
    return out
