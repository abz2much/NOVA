"""The convenience path (design D2), shadowing anticipation alerts (8.48.0).

Anticipation alerts ("usually has activity by now", "usually home by now",
routine starts, departure reminders, someone heading home) are convenience,
not safety. This path will decide them on its own. Until you switch it on
for a pattern, it only shadows the current path:

* Both paths judge the same alert from the same reading of the house (the
  30 second check's snapshot). The new path's verdict is worked out first
  and sends nothing; then the current path delivers as before. Each pair is
  kept with its reasons, so the panel can show them side by side.
* After at least MIN_DAYS days and MIN_DECISIONS comparisons for a pattern,
  the panel offers to let the new path decide that pattern. Only you can
  switch it on, one pattern at a time, and you can switch it back.
* Two Not helpful ratings in a row on one pattern (your phone taps, the
  only ratings Nova learns from) make the panel ask whether to stop that
  pattern. Stopping is your choice, for that pattern only, and you can
  restart it. Nothing stops on its own, and no category is ever muted.
* Safety and security alerts are never here: an alert about a lock, a
  cover or the alarm, the "less secure than usual" anticipation alert and
  anything critical stay on the current path, out of reach of learning.

Rows live in decisions.db and are pruned after KEEP_DAYS days.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

_LOGGER = logging.getLogger(__name__)

KINDS = ("anticipation_overdue", "anticipation_routine", "anticipation_departure",
         "anticipation_presence", "anticipation_arriving")
MIN_DAYS = 14
MIN_DECISIONS = 10
KEEP_DAYS = 180
RECENT = 50

SAID, HELD = "said", "held"
CURRENT, NEW, STOPPED = "current", "new", "stopped"   # who decided what happened
STOPPED_REASON = "you stopped this pattern"
_NOT_HELPFUL = ("unnecessary", "wrong")


@dataclass(frozen=True)
class Verdict:
    """One path's answer for one alert."""
    say: bool
    reason: str = ""

    @property
    def word(self) -> str:
        return SAID if self.say else HELD


def eligible(action: dict) -> bool:
    """Whether this alert belongs on the convenience path."""
    kind = str(action.get("type") or "")
    if kind not in KINDS or not action.get("pattern_key"):
        return False
    try:
        from . import habituation
        return not habituation.exempt(urgency=action.get("urgency", ""), kind=kind,
                                      entity_id=action.get("entity_id", ""))
    except Exception:
        return False


def decide(hass, config: dict, action: dict, sleeping: bool, reading) -> Verdict:
    """The new path's verdict for one alert, from the given reading. Reads
    only: nothing is spoken, pushed or recorded."""
    from . import core_delivery, delivery, habituation
    key = core_delivery.habit_key(action)
    if key and habituation.is_quiet(key):
        return Verdict(False, "normal for this home (given three days running)")
    req = core_delivery.request_for(config, action, sleeping, reading=reading)
    cond = delivery.conditions(hass, req)
    d = delivery.decide(req, cond)
    if d.speak:
        # Where it would go, the way delivery routes it: a speaker, else the
        # phones when routing or the fallback sends it there, else nowhere.
        targets, mode = delivery._speakers(hass, req, cond)
        if targets:
            return Verdict(True, d.reason or "spoken")
        if mode == "notify_only" and req.push != delivery.PUSH_NEVER:
            return Verdict(True, d.reason or "routed to the phones")
        if delivery._falls_back(req) or d.push:
            return Verdict(True, d.reason or "no speaker took it")
        return Verdict(False, d.reason or "not delivered")
    return Verdict(d.delivered, d.reason or ("pushed" if d.push else "not delivered"))


async def async_handle(hass, config: dict, action: dict, sleeping: bool, reading) -> None:
    """Shadow, then deliver, one convenience alert, and keep the pair."""
    from . import core_delivery
    key = str(action["pattern_key"])
    stopped, active = await hass.async_add_executor_job(choice, key)
    try:
        shadow: Optional[Verdict] = decide(hass, config, action, sleeping, reading)
    except Exception as exc:
        _LOGGER.debug("convenience: shadow verdict failed: %s", exc)
        shadow = None
    if stopped:
        actual, by = Verdict(False, STOPPED_REASON), STOPPED
    elif active and shadow is not None and not shadow.say:
        actual, by = shadow, NEW
    else:
        said, why = await core_delivery._emit_action(hass, config, action, sleeping,
                                                     reading=reading)
        actual, by = Verdict(said, why), (NEW if active and shadow is not None else CURRENT)
    if stopped and shadow is not None:
        shadow = Verdict(False, STOPPED_REASON)
    await hass.async_add_executor_job(lambda: record(
        key, str(action.get("type") or ""), getattr(reading, "snapshot_id", "") or "",
        action.get("decision_id"), str(action.get("message") or "")[:200], actual,
        shadow if shadow is not None else Verdict(False, "the new path could not decide"),
        by))


# ── storage (decisions.db) ──────────────────────────────────────────────────

def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    from . import decision_record
    from .persistence import sqlite as _store
    conn = _store.connect(db_path or decision_record._default_db(), busy_timeout_ms=None)
    _store.ensure(conn, "decision_records", "convenience")
    conn.row_factory = sqlite3.Row
    return conn


def choice(pattern_key: str, db_path: Optional[str] = None) -> tuple:
    """(stopped, active) for one pattern. Never raises: unreadable is
    (False, False), so the current path carries on as before."""
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute("SELECT stopped, active FROM convenience_patterns "
                               "WHERE pattern_key = ?", (pattern_key,)).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        _LOGGER.debug("convenience: choice unreadable: %s", exc)
        return False, False
    return (bool(row["stopped"]), bool(row["active"])) if row else (False, False)


def record(pattern_key: str, kind: str, snapshot_id: str, decision_id, message: str,
           actual: Verdict, shadow: Verdict, decided_by: str,
           db_path: Optional[str] = None, now: Optional[float] = None) -> None:
    """Keep one pair of verdicts. Never raises."""
    now = now or time.time()
    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO convenience_shadow (ts, pattern_key, kind, snapshot_id, "
                "decision_id, message, actual, actual_reason, shadow, shadow_reason, "
                "decided_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, pattern_key, kind, snapshot_id,
                 int(decision_id) if decision_id is not None else None, message,
                 actual.word, actual.reason, shadow.word, shadow.reason, decided_by))
            conn.execute("DELETE FROM convenience_shadow WHERE ts < ?",
                         (now - KEEP_DAYS * 86400.0,))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        _LOGGER.debug("convenience: record failed: %s", exc)


def _patterns(conn) -> dict:
    out: dict = {}
    for r in conn.execute(
            "SELECT pattern_key, kind, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
            "SUM(CASE WHEN actual = shadow THEN 1 ELSE 0 END) AS agree "
            "FROM convenience_shadow GROUP BY pattern_key"):
        out[r["pattern_key"]] = {
            "pattern_key": r["pattern_key"], "kind": r["kind"], "decisions": r["n"],
            "agree": r["agree"] or 0, "first_ts": r["first_ts"], "last_ts": r["last_ts"],
            "stopped": False, "active": False, "kept_ts": None}
    for r in conn.execute("SELECT pattern_key, stopped, active, kept_ts "
                          "FROM convenience_patterns"):
        p = out.setdefault(r["pattern_key"], {
            "pattern_key": r["pattern_key"], "kind": "", "decisions": 0, "agree": 0,
            "first_ts": None, "last_ts": None})
        p.update(stopped=bool(r["stopped"]), active=bool(r["active"]), kept_ts=r["kept_ts"])
    return out


def _last_ratings(conn, pattern_key: str, since: Optional[float]) -> list:
    """The pattern's confirmed ratings, newest first, given after `since`."""
    from . import decision_record
    sources = sorted(decision_record.CONFIRMED_SOURCES)
    rows = conn.execute(
        "SELECT d.outcome FROM convenience_shadow s JOIN decision_records d "
        "ON d.id = s.decision_id WHERE s.pattern_key = ? AND d.outcome IS NOT NULL "
        "AND d.outcome_source IN (%s) AND d.outcome_ts > ? "
        "ORDER BY d.outcome_ts DESC LIMIT 2" % ", ".join("?" * len(sources)),
        [pattern_key, *sources, float(since or 0.0)]).fetchall()
    return [r["outcome"] for r in rows]


def _ready(p: dict, now: float) -> bool:
    return (p["decisions"] >= MIN_DECISIONS and p["first_ts"] is not None
            and now - p["first_ts"] >= MIN_DAYS * 86400.0)


def status(db_path: Optional[str] = None, now: Optional[float] = None) -> dict:
    """What the panel shows: each pattern with its comparison so far, whether
    it is ready to switch over and whether to ask about stopping it, plus the
    latest pairs of verdicts."""
    now = now or time.time()
    conn = _connect(db_path)
    try:
        patterns = _patterns(conn)
        last_msgs = {r["pattern_key"]: r["message"] for r in conn.execute(
            "SELECT pattern_key, message FROM convenience_shadow s WHERE ts = "
            "(SELECT MAX(ts) FROM convenience_shadow WHERE pattern_key = s.pattern_key)")}
        for key, p in patterns.items():
            p["example"] = last_msgs.get(key, "")
            p["ready"] = _ready(p, now)
            last = [] if p["stopped"] else _last_ratings(conn, key, p["kept_ts"])
            p["ask_stop"] = len(last) == 2 and all(o in _NOT_HELPFUL for o in last)
        recent = [dict(r) for r in conn.execute(
            "SELECT ts, pattern_key, kind, snapshot_id, message, actual, actual_reason, "
            "shadow, shadow_reason, decided_by FROM convenience_shadow "
            "ORDER BY ts DESC, id DESC LIMIT ?", (RECENT,))]
    finally:
        conn.close()
    return {"patterns": sorted(patterns.values(), key=lambda p: -(p["last_ts"] or 0)),
            "recent": recent, "min_days": MIN_DAYS, "min_decisions": MIN_DECISIONS}


def set_choice(pattern_key: str, what: str, db_path: Optional[str] = None,
               now: Optional[float] = None) -> str:
    """Your choice for one pattern: "stop", "restart", "keep" (answering the
    stop question with no), "activate" or "deactivate". Returns "ok",
    "not_ready" (activate before the comparison is enough) or "unknown"."""
    now = now or time.time()
    conn = _connect(db_path)
    try:
        if what == "activate":
            p = _patterns(conn).get(pattern_key)
            if p is None or not _ready(p, now):
                return "not_ready"
        column, value = {"stop": ("stopped", 1), "restart": ("stopped", 0),
                         "keep": ("kept_ts", now), "activate": ("active", 1),
                         "deactivate": ("active", 0)}.get(what, (None, None))
        if column is None:
            return "unknown"
        conn.execute(
            "INSERT INTO convenience_patterns (pattern_key, updated_ts) VALUES (?, ?) "
            "ON CONFLICT(pattern_key) DO NOTHING", (pattern_key, now))
        conn.execute(f"UPDATE convenience_patterns SET {column} = ?, updated_ts = ? "
                     "WHERE pattern_key = ?", (value, now, pattern_key))
        if what == "restart":
            # Ratings given before the restart are not asked about again.
            conn.execute("UPDATE convenience_patterns SET kept_ts = ? WHERE pattern_key = ?",
                         (now, pattern_key))
        conn.commit()
    finally:
        conn.close()
    return "ok"
