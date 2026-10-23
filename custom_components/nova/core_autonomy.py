"""How often each proactive offer was accepted.

Nova never carries out a proactive offer on its own (8.39.1). Graduated
autonomy, which let an offer accepted three times run silently from then
on, is gone: every proactive action needs a fresh yes. This module only
counts acceptances, for the manage_autonomy tool's list, and clears any
grant saved by an older version the first time it loads.
"""
from __future__ import annotations

import json
import logging
import os

from homeassistant.util import dt as dt_util

from . import core_common as _m_common
from .core_bridge import _autonomy_file

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


def _now() -> str:
    return dt_util.utcnow().isoformat()


# ── Acceptance counts (kept from graduated autonomy, v5.9.07) ───────────────

class AutonomyManager:
    """
    Counts how often each proactive offer was accepted. It never grants
    autonomy.

    A "pattern_key" names a kind of action, for example
    'lights_on_when_dark:living_room'. Every acceptance adds one to its count
    and a refusal sets it back to zero. Nothing here lets an offer run
    without a yes.
    """

    def __init__(self):
        # pattern_key -> {approvals, granted, confidence, ...}
        self._grants: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(_autonomy_file()):
                with open(_autonomy_file(), "r", encoding="utf-8") as handle:
                    self._grants = json.load(handle) or {}
        except Exception as exc:
            _LOGGER.warning("Autonomy grants load failed: %s", exc)
            self._grants = {}
        if not isinstance(self._grants, dict):
            self._grants = {}
        # Before 8.39.1 a grant could be saved. Each one is switched off once
        # and the file rewritten, so no older "do this on your own" survives
        # an upgrade.
        stale = [key for key, record in self._grants.items()
                 if isinstance(record, dict) and record.get("granted")]
        if not stale:
            return
        for key in stale:
            self._grants[key]["granted"] = False
            self._grants[key]["revoked_at"] = _now()
        _LOGGER.info("Cleared %d saved autonomy grant(s): Nova no longer acts "
                     "on its own", len(stale))
        self._save()

    def _save(self) -> None:
        try:
            _m_common.write_json_atomic(
                _autonomy_file(), self._grants, indent=2, encoding="utf-8")
        except Exception as exc:
            _LOGGER.warning("Autonomy grants save failed: %s", exc)

    def record_acceptance(self, pattern_key: str, confidence: float = 1.0) -> dict:
        """
        The user accepted an offer. Add one to its count and return its
        record. This never grants autonomy.
        """
        if not pattern_key:
            return {}
        record = self._grants.get(pattern_key, {
            "approvals": 0, "granted": False, "confidence": confidence,
            "first_seen": _now(),
        })
        record["approvals"] = record.get("approvals", 0) + 1
        record["confidence"] = max(record.get("confidence", 0.0), confidence)
        record["last_accepted"] = _now()
        record["granted"] = False
        self._grants[pattern_key] = record
        self._save()
        return record

    def record_rejection(self, pattern_key: str) -> None:
        """The user declined an offer, so trust in that pattern starts again."""
        if pattern_key not in self._grants:
            return
        record = self._grants[pattern_key]
        record["approvals"] = 0
        record["granted"] = False
        record["last_rejected"] = _now()
        self._save()

    def is_autonomous(self, pattern_key: str) -> bool:
        """Always False. Nova never carries out a proactive offer without a
        yes (8.39.1)."""
        return False

    def revoke(self, pattern_key: str) -> bool:
        """Set a pattern's acceptance count back to zero."""
        if pattern_key not in self._grants:
            return False
        record = self._grants[pattern_key]
        record["granted"] = False
        record["approvals"] = 0
        record["revoked_at"] = _now()
        self._save()
        return True

    def list_grants(self) -> list[dict]:
        """Every tracked pattern with its acceptance count."""
        return [
            {
                "pattern_key": key,
                "approvals": record.get("approvals", 0),
                "granted": False,
                "confidence": round(record.get("confidence", 0.0), 2),
            }
            for key, record in self._grants.items()
        ]
