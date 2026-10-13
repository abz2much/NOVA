"""Fault history ledger for the diagnostics layer.

The infrastructure audit records each alert it raises here, and asks for
earlier faults sharing a keyword so it can say how often a problem has come
back. Stored as one JSON list, newest last, capped at MAX_ENTRIES. Standard
library only at import time; all file I/O is synchronous (run it in the
executor)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# Tests may point this at a temporary file; read at call time.
DEFAULT_FAULT_LOG_PATH: Optional[str] = None
MAX_ENTRIES = 1000


def _default_fault_log_path() -> str:
    from .. import paths
    if DEFAULT_FAULT_LOG_PATH:
        return DEFAULT_FAULT_LOG_PATH
    return paths.nova_path("fault_history.json")


class FaultLog:
    """Append-only list of past faults with keyword recall."""

    def __init__(self, path: Optional[str] = None, *,
                 max_entries: int = MAX_ENTRIES) -> None:
        self.path = path or _default_fault_log_path()
        self.max_entries = max_entries
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError) as exc:
            _LOGGER.warning("fault log unreadable (%s) — starting fresh", exc)
            return []
        return data if isinstance(data, list) else []

    def _save(self, entries: list[dict]) -> None:
        tmp = f"{self.path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(entries, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as exc:
            _LOGGER.error("Failed to persist fault log: %s", exc)

    def commit_event(self, entry_text: str,
                     tags: list[str] | None = None) -> dict:
        """Record one fault and return the stored entry."""
        entry = {"text": str(entry_text), "ts": time.time(),
                 "tags": [str(t).lower() for t in (tags or [])]}
        with self._lock:
            # Slicing keeps the newest max_entries (all of them when it is 0).
            self._save((self._load() + [entry])[-self.max_entries:])
        return entry

    def query_related_faults(self, target_keywords: list[str]) -> list[dict]:
        """Past faults whose text or tags contain any keyword (oldest first)."""
        keywords = [str(k).lower() for k in (target_keywords or [])
                    if str(k).strip()]
        if not keywords:
            return []
        with self._lock:
            entries = self._load()
        hits = []
        for entry in entries:
            haystack = (str(entry.get("text", "")).lower() + " "
                        + " ".join(str(t).lower() for t in entry.get("tags", [])))
            if any(k in haystack for k in keywords):
                hits.append(entry)
        return hits

    def recent(self, limit: int = 10) -> list[dict]:
        """The newest `limit` entries, newest last."""
        with self._lock:
            return self._load()[-limit:]
