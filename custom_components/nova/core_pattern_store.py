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


# ── State Change Logger ─────────────────────────────────────────────────────

class StateLogger:
    """Logs meaningful state changes for pattern learning."""

    def __init__(self, db_path=None):
        self._last_states: dict[str, str] = {}
        self._db_path = db_path or _patterns_db()
        self._init_db()

    def _init_db(self):
        import sqlite3
        from pathlib import Path
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(self._db_path) as conn:
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
        import sqlite3
        domain = entity_id.split(".")[0]
        if domain in ("automation", "script", "input_boolean", "input_number"):
            return  # Meta entities, not useful for patterns
        # scene is deliberately NOT skipped: a scene activation is a real,
        # low-volume action worth learning as the target of "button → scene".

        # Noisy domains are skipped for pattern learning UNLESS the user opted
        # this entity/group in (e.g. garage door contacts, presence) — v7.11.0.
        if not force_include and domain in ("sensor", "binary_sensor", "weather",
                                            "sun", "update", "device_tracker",
                                            "event"):
            return  # Too noisy for pattern learning

        now = datetime.now()
        try:
            with sqlite3.connect(self._db_path) as conn:
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

    def log_command(self, text: str, handled_by: str = "agent",
                     entity_ids: list = None, person: str = "unknown"):
        """Record a voice/text command for pattern analysis."""
        import sqlite3
        now = datetime.now()
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO commands "
                    "(timestamp, text, handled_by, entity_ids, person, "
                    "hour, day_of_week) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (now.isoformat(), text, handled_by,
                     json.dumps(entity_ids or []), person,
                     now.hour, now.weekday()),
                )
        except Exception:
            pass

    def distinct_logged_entities(self) -> set:
        """Entities already present in the pattern store — skipped on backfill so
        importing history can't double-count what live logging already covers."""
        import sqlite3
        try:
            with sqlite3.connect(self._db_path) as conn:
                return {r[0] for r in conn.execute(
                    "SELECT DISTINCT entity_id FROM state_changes")}
        except Exception:
            return set()

    def bulk_insert_history(self, rows: list) -> int:
        """Bulk-insert backfilled historical state changes. Each row is
        (timestamp_iso, entity_id, domain, old_state, new_state); hour and
        day_of_week are derived from the historical timestamp (not now), and the
        source is tagged 'history'. Returns the number inserted."""
        import sqlite3
        prepared = []
        for row in rows:
            try:
                ts, eid, dom, old, new = row
                dt = datetime.fromisoformat(ts)
            except Exception:
                continue
            prepared.append((ts, eid, dom, old, new, "", dt.hour, dt.weekday(),
                             "history", "unknown", 0.0))
        if not prepared:
            return 0
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.executemany(
                    "INSERT INTO state_changes "
                    "(timestamp, entity_id, domain, old_state, new_state, "
                    "area_id, hour, day_of_week, triggered_by, person, "
                    "person_confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    prepared)
            return len(prepared)
        except Exception:
            return 0

    def get_pattern_stats(self) -> dict:
        """Return learning statistics."""
        import sqlite3
        stats = {"state_changes": 0, "commands": 0, "suggestions": 0,
                 "patterns": 0, "days_of_data": 0}
        try:
            with sqlite3.connect(self._db_path) as conn:
                stats["state_changes"] = conn.execute(
                    "SELECT COUNT(*) FROM state_changes").fetchone()[0]
                stats["commands"] = conn.execute(
                    "SELECT COUNT(*) FROM commands").fetchone()[0]
                stats["suggestions"] = conn.execute(
                    "SELECT COUNT(*) FROM suggestions WHERE status='pending'"
                ).fetchone()[0]
                stats["patterns"] = conn.execute(
                    "SELECT COUNT(*) FROM person_patterns").fetchone()[0]
                oldest = conn.execute(
                    "SELECT MIN(timestamp) FROM state_changes"
                ).fetchone()[0]
                if oldest:
                    days = (datetime.now() - datetime.fromisoformat(oldest)).days
                    stats["days_of_data"] = days
        except Exception:
            pass
        return stats
