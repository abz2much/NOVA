"""Habit model for pre-emptive actions.

Records time-stamped observations (for example "office_entry") to a bounded
JSON log and estimates, per key and time-of-day bucket, the share of observed
days on which the key happened in that bucket. Keys at or above a threshold
for the bucket a few minutes ahead are reported as candidates; the caller
decides what to do with them. Standard library only at import time; file I/O
is synchronous (run it in the executor). Buckets use the machine's local time.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import time
from threading import Lock
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# Tests may point this at a temporary file; read at call time.
DEFAULT_MATRIX_PATH: Optional[str] = None

DEFAULT_BUCKET_MINUTES = 30
DEFAULT_THRESHOLD = 0.90
DEFAULT_LEAD_MINUTES = 7
MAX_EVENTS = 5000


def _default_matrix_path() -> str:
    from .. import paths
    if DEFAULT_MATRIX_PATH:
        return DEFAULT_MATRIX_PATH
    return paths.nova_path("habit_matrix.json")


def _day_key(ts: float) -> str:
    return datetime.date.fromtimestamp(ts).isoformat()


class PredictiveHabitMatrix:
    """Per-key, per-bucket habit probabilities from an event log."""

    def __init__(self, path: Optional[str] = None, *,
                 bucket_minutes: int = DEFAULT_BUCKET_MINUTES,
                 max_events: int = MAX_EVENTS) -> None:
        self.path = path or _default_matrix_path()
        self.bucket_minutes = max(1, int(bucket_minutes))
        self.max_events = max_events
        self._lock = Lock()

    def _slot(self, ts: float) -> int:
        local = datetime.datetime.fromtimestamp(ts)
        return (local.hour * 60 + local.minute) // self.bucket_minutes

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError) as exc:
            _LOGGER.warning("habit matrix unreadable (%s) — starting fresh", exc)
            return []
        return data if isinstance(data, list) else []

    def _save(self, events: list[dict]) -> None:
        tmp = f"{self.path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(events, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as exc:
            _LOGGER.error("Failed to persist habit matrix: %s", exc)

    def record_event(self, key: str, ts: float | None = None) -> dict:
        """Log one observation of `key` (now, unless `ts` is given)."""
        when = time.time() if ts is None else float(ts)
        event = {"key": str(key), "ts": when, "slot": self._slot(when),
                 "day": _day_key(when)}
        with self._lock:
            # Slicing keeps the newest max_events (all of them when it is 0).
            self._save((self._load() + [event])[-self.max_events:])
        return event

    def probability(self, key: str, at_ts: float) -> float:
        """Share of observed days on which `key` happened in the bucket of `at_ts`."""
        slot = self._slot(at_ts)
        with self._lock:
            events = self._load()
        if not events:
            return 0.0
        all_days = {e.get("day") for e in events if e.get("day")}
        if not all_days:
            return 0.0
        key_days = {e.get("day") for e in events
                    if e.get("key") == key and e.get("slot") == slot and e.get("day")}
        return round(len(key_days) / len(all_days), 4)

    def should_preempt(self, key: str, at_ts: float, *,
                       threshold: float = DEFAULT_THRESHOLD) -> bool:
        return self.probability(key, at_ts) >= threshold

    def due_preemptions(self, now_ts: float | None = None, *,
                        lead_minutes: int = DEFAULT_LEAD_MINUTES,
                        threshold: float = DEFAULT_THRESHOLD) -> list[dict]:
        """Keys likely to happen `lead_minutes` from now, most likely first."""
        now = time.time() if now_ts is None else float(now_ts)
        target = now + lead_minutes * 60
        with self._lock:
            keys: set[str] = {e["key"] for e in self._load() if e.get("key")}
        due: list[dict] = []
        for key in keys:
            prob = self.probability(key, target)
            if prob >= threshold:
                due.append({"key": key, "probability": prob,
                            "lead_minutes": lead_minutes})
        due.sort(key=lambda d: d["probability"], reverse=True)
        return due

    def observed_days(self) -> int:
        with self._lock:
            return len({e.get("day") for e in self._load() if e.get("day")})
