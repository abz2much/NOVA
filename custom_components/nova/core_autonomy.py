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


# ── Graduated Autonomy (v5.9.07) ────────────────────────────────────────────

class AutonomyManager:
    """
    Counts acceptances per proactive offer. Never grants autonomy.

    A "pattern_key" identifies the kind of action (e.g.
    'lights_on_when_dark:living_room'). Each acceptance increments a counter
    and a no resets it. Nothing here lets an offer run without a yes.
    """

    def __init__(self):
        self._grants: dict[str, dict] = {}  # pattern_key -> {approvals,granted,...}
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(_autonomy_file()):
                with open(_autonomy_file(), "r", encoding="utf-8") as f:
                    self._grants = json.load(f) or {}
        except Exception as exc:
            _LOGGER.warning("Autonomy grants load failed: %s", exc)
            self._grants = {}
        if not isinstance(self._grants, dict):
            self._grants = {}
        # A grant saved before 8.39.1 is cleared once and the file rewritten,
        # so no older "do this on your own" survives the upgrade.
        cleared = [k for k, g in self._grants.items()
                   if isinstance(g, dict) and g.get("granted")]
        for key in cleared:
            self._grants[key]["granted"] = False
            self._grants[key]["revoked_at"] = dt_util.utcnow().isoformat()
        if cleared:
            _LOGGER.info("Cleared %d saved autonomy grant(s): Nova no longer acts "
                         "on its own", len(cleared))
            self._save()

    def _save(self) -> None:
        try:
            _m_common.write_json_atomic(_autonomy_file(), self._grants, indent=2, encoding="utf-8")
        except Exception as exc:
            _LOGGER.warning("Autonomy grants save failed: %s", exc)

    def record_acceptance(self, pattern_key: str, confidence: float = 1.0) -> dict:
        """
        User accepted an offer. Increment its counter and return the record.
        Never grants autonomy.
        """
        if not pattern_key:
            return {}
        g = self._grants.get(pattern_key, {
            "approvals": 0, "granted": False, "confidence": confidence,
            "first_seen": dt_util.utcnow().isoformat(),
        })
        g["approvals"] = g.get("approvals", 0) + 1
        g["confidence"] = max(g.get("confidence", 0.0), confidence)
        g["last_accepted"] = dt_util.utcnow().isoformat()
        g["granted"] = False
        self._grants[pattern_key] = g
        self._save()
        return g

    def record_rejection(self, pattern_key: str) -> None:
        """User declined an offer — reset trust toward this pattern."""
        if pattern_key in self._grants:
            self._grants[pattern_key]["approvals"] = 0
            self._grants[pattern_key]["granted"] = False
            self._grants[pattern_key]["last_rejected"] = dt_util.utcnow().isoformat()
            self._save()

    def is_autonomous(self, pattern_key: str) -> bool:
        """Always False: Nova never performs a proactive offer without a yes
        (8.39.1)."""
        return False

    def revoke(self, pattern_key: str) -> bool:
        """Reset a pattern's acceptance count."""
        if pattern_key in self._grants:
            self._grants[pattern_key]["granted"] = False
            self._grants[pattern_key]["approvals"] = 0
            self._grants[pattern_key]["revoked_at"] = dt_util.utcnow().isoformat()
            self._save()
            return True
        return False

    def list_grants(self) -> list[dict]:
        """All tracked patterns with their acceptance counts."""
        out = []
        for key, g in self._grants.items():
            out.append({
                "pattern_key": key,
                "approvals": g.get("approvals", 0),
                "granted": False,
                "confidence": round(g.get("confidence", 0.0), 2),
            })
        return out
