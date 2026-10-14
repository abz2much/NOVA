"""Write-ahead ledger for high-stakes device actions.

Before a lock, cover or alarm command is sent, the caller records an
*intent*; once the command is accepted it records a *completion*. Intents
left without a completion (a crash or power loss in between) are reported
at boot so they can be compared with the device's real state.

Storage is a single append-only JSON-lines file under the Nova data folder,
periodically compacted down to the intents that are still outstanding.
Only the standard library is imported at module level, so the file can be
loaded on its own outside the package.
"""
from __future__ import annotations

import json
import logging
import os
import time
from threading import Lock
from typing import Optional

_LOGGER = logging.getLogger(__name__)

DEFAULT_LEDGER_PATH: Optional[str] = None
MAX_RECORDS = 5000

_OP_INTENT = "intent"
_OP_COMPLETE = "complete"


def _default_ledger_path() -> str:
    """Return the ledger location: the module override, else the Nova path."""
    # Imported lazily so a standalone load of this file stays importable.
    from . import paths

    if DEFAULT_LEDGER_PATH:
        return DEFAULT_LEDGER_PATH
    return paths.nova_path("state_ledger.jsonl")


def _ensure_parent(target: str) -> None:
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)


def _encode(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False) + "\n"


def _outstanding(records: list) -> list:
    """Intents in file order whose txn never appears on a completion."""
    done = {rec.get("txn") for rec in records if rec.get("op") == _OP_COMPLETE}
    return [
        rec
        for rec in records
        if rec.get("op") == _OP_INTENT and rec.get("txn") not in done
    ]


class StateLedger:
    """Durable intent/completion log; all state lives in the file."""

    def __init__(
        self, path: Optional[str] = None, *, max_records: int = MAX_RECORDS
    ) -> None:
        # No filesystem access here: construction happens on the event loop.
        self.path: str = path or _default_ledger_path()
        self.max_records = max_records
        self._lock = Lock()
        self._seq = 0

    # -- low-level file helpers (callers hold the lock where needed) -------

    def _load(self) -> list:
        records: list = []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for raw in handle:
                    text = raw.strip()
                    if not text:
                        continue
                    try:
                        records.append(json.loads(text))
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            return []
        except OSError as err:
            _LOGGER.warning("Cannot read state ledger %s: %s", self.path, err)
            return []
        return records

    def _append(self, record: dict) -> None:
        line = _encode(record)
        try:
            _ensure_parent(self.path)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as err:
            _LOGGER.error("Cannot append to state ledger %s: %s", self.path, err)

    def _rewrite(self, records: list) -> None:
        tmp_path = self.path + ".tmp"
        try:
            _ensure_parent(self.path)
            with open(tmp_path, "w", encoding="utf-8") as handle:
                for rec in records:
                    handle.write(_encode(rec))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except OSError as err:
            _LOGGER.error("Cannot compact state ledger %s: %s", self.path, err)

    def _compact_unlocked(self) -> None:
        self._rewrite(_outstanding(self._load()))

    # -- public API ---------------------------------------------------------

    def record_intent(self, entity_id: str, desired_state: str, **meta) -> str:
        """Durably note that ``entity_id`` is about to be set; return the txn."""
        with self._lock:
            self._seq += 1
            txn = f"{time.time():.6f}-{entity_id}-{self._seq}"
            record = {
                "txn": txn,
                "op": _OP_INTENT,
                "entity_id": entity_id,
                "desired_state": str(desired_state),
                "meta": dict(meta),
                "ts": time.time(),
            }
            self._append(record)
            if len(self._load()) > self.max_records:
                self._compact_unlocked()
        return txn

    def mark_complete(self, txn: str) -> None:
        """Note that the action for ``txn`` was accepted."""
        with self._lock:
            self._append({"txn": txn, "op": _OP_COMPLETE, "ts": time.time()})

    def pending_intents(self) -> list[dict]:
        """Intents that have no matching completion, in file order."""
        return _outstanding(self._load())

    def reconcile(self, verify_fn) -> list[dict]:
        """Compare each pending intent with ``verify_fn(entity_id)``.

        Returns the intents whose actual state differs (string comparison),
        each copied with an added ``"actual"`` key. Nothing is written.
        """
        mismatches: list[dict] = []
        for intent in self.pending_intents():
            entity_id = intent["entity_id"]
            try:
                actual = verify_fn(entity_id)
            except Exception:  # noqa: BLE001 - a failing probe counts as unknown
                actual = None
            if str(actual) != intent["desired_state"]:
                mismatches.append({**intent, "actual": actual})
        return mismatches

    def compact(self) -> None:
        """Rewrite the ledger so it holds only the outstanding intents."""
        with self._lock:
            self._compact_unlocked()
