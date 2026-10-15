"""StateLogger: writes state changes and commands to patterns.db for pattern
learning.

Moved out of cognitive_core.py unchanged in 8.7.17. cognitive_core.py
still exports every name defined here, as the same object.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from .core_bridge import _patterns_db
from .persistence import sqlite as _store

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


# Which domains pattern learning keeps. The history import in
# core_learning.py uses these same two sets (8.49.37).
# Helpers and automations are Nova's own plumbing, never a person's routine.
# Scenes are kept on purpose: "button then scene" needs them.
META_DOMAINS = frozenset({"automation", "script", "input_boolean", "input_number"})
# Chatty sources, kept only for an entity or group the user opted in.
NOISY_DOMAINS = frozenset({"sensor", "binary_sensor", "weather", "sun", "update",
                           "device_tracker", "event"})


# ── State Change Logger ─────────────────────────────────────────────────────

class StateLogger:
    """Logs meaningful state changes for pattern learning."""

    def __init__(self, db_path=None):
        self._last_states: dict[str, str] = {}
        self._db_path = db_path or _patterns_db()
        self._init_db()

    def _init_db(self):
        from pathlib import Path
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            with _store.connect(self._db_path, row_factory=False, mkdir=False,
                                busy_timeout_ms=None) as conn:
                _store.ensure(conn, "pattern_log", "person_patterns")
        except Exception as exc:
            _LOGGER.warning("Pattern DB init failed: %s", exc)

    def log_state_change(self, entity_id: str, old_state: str,
                          new_state: str, area_id: str = "",
                          triggered_by: str = "system",
                          source_entity_id: str = "",
                          source_confidence: float = 0.0,
                          person: str = "unknown",
                          person_confidence: float = 0.0,
                          force_include: bool = False,
                          detection_confidence: Optional[float] = None):
        """Record a state change for pattern analysis. `detection_confidence`
        (v7.109.0) is the SOURCE's own confidence in the event happening at
        all — distinct from person_confidence (confidence in WHO). None
        means the source supplied no score; never invented here."""
        domain = entity_id.split(".")[0]
        if domain in META_DOMAINS:
            return
        if not force_include and domain in NOISY_DOMAINS:
            return

        now = datetime.now()
        try:
            with _store.connect(self._db_path, row_factory=False, mkdir=False,
                                busy_timeout_ms=None) as conn:
                conn.execute(
                    "INSERT INTO state_changes "
                    "(timestamp, entity_id, domain, old_state, new_state, "
                    "area_id, hour, day_of_week, triggered_by, person, "
                    "person_confidence, detection_confidence, source_entity_id, "
                    "source_confidence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (now.isoformat(), entity_id, domain, old_state,
                     new_state, area_id, now.hour, now.weekday(),
                     triggered_by, person, float(person_confidence or 0.0),
                     None if detection_confidence is None else float(detection_confidence),
                     str(source_entity_id or ""), float(source_confidence or 0.0)),
                )
        except Exception:
            pass

    def _open(self):
        """A connection to the pattern store, used as ``with``: it commits
        when the block ends."""
        return _store.connect(self._db_path, row_factory=False, mkdir=False,
                              busy_timeout_ms=None)

    def log_command(self, text: str, handled_by: str = "agent",
                    entity_ids: list = None, person: str = "unknown"):
        """Keep one spoken or typed command for pattern analysis."""
        now = datetime.now()
        row = (now.isoformat(), text, handled_by, json.dumps(entity_ids or []), person,
               now.hour, now.weekday())
        try:
            with self._open() as conn:
                conn.execute("INSERT INTO commands (timestamp, text, handled_by, "
                             "entity_ids, person, hour, day_of_week) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)", row)
        except Exception:
            pass

    def distinct_logged_entities(self) -> set:
        """Every entity already in the store, which a history import skips so
        it never counts twice what live logging already holds."""
        try:
            with self._open() as conn:
                found = conn.execute("SELECT DISTINCT entity_id FROM state_changes")
                return {row[0] for row in found}
        except Exception:
            return set()

    def bulk_insert_history(self, rows: list) -> int:
        """Add imported history: each row is (timestamp_iso, entity_id, domain,
        old_state, new_state). Hour and weekday come from the row's own time,
        and the source is "history". Returns how many went in; a row whose
        time cannot be read is skipped."""
        ready = []
        for row in rows:
            try:
                ts, entity_id, domain, old, new = row
                when = datetime.fromisoformat(ts)
            except Exception:
                continue
            ready.append((ts, entity_id, domain, old, new, "", when.hour, when.weekday(),
                          "history", "unknown", 0.0))
        if not ready:
            return 0
        try:
            with self._open() as conn:
                conn.executemany(
                    "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, "
                    "new_state, area_id, hour, day_of_week, triggered_by, person, "
                    "person_confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ready)
        except Exception:
            return 0
        return len(ready)

    def get_pattern_stats(self) -> dict:
        """Counts for the learning panel, and how many days of data there are."""
        stats = {"state_changes": 0, "commands": 0, "suggestions": 0,
                 "patterns": 0, "days_of_data": 0}
        counts = (("state_changes", "SELECT COUNT(*) FROM state_changes"),
                  ("commands", "SELECT COUNT(*) FROM commands"),
                  ("suggestions", "SELECT COUNT(*) FROM suggestions WHERE status='pending'"),
                  ("patterns", "SELECT COUNT(*) FROM person_patterns"))
        try:
            with self._open() as conn:
                for key, sql in counts:
                    stats[key] = conn.execute(sql).fetchone()[0]
                oldest = conn.execute("SELECT MIN(timestamp) FROM state_changes").fetchone()[0]
                if oldest:
                    stats["days_of_data"] = (datetime.now()
                                             - datetime.fromisoformat(oldest)).days
        except Exception:
            pass
        return stats
