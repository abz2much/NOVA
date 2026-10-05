"""Graduated autonomy: which proactive offers Nova may carry out on its own.

Moved out of cognitive_core.py unchanged in 8.7.17. cognitive_core.py
still exports every name defined here, as the same object.
"""
from __future__ import annotations

import json
import logging
import os

from homeassistant.util import dt as dt_util

from . import core_common as _m_common
from .core_bridge import _autonomy_file
from .core_common import AUTONOMY_MIN_CONFIDENCE, AUTONOMY_TRUST_THRESHOLD

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


# ── Graduated Autonomy (v5.9.07) ────────────────────────────────────────────

class AutonomyManager:
    """
    Tracks which proactive offers the user trusts Nova to perform alone.

    Graduated trust model:
      Tier 0 (default) — Nova OFFERS; user must accept each time.
      Tier 1 (trusted) — after the user accepts the same offer
                         AUTONOMY_TRUST_THRESHOLD times, Nova may perform
                         it silently (still logs it; user can revoke).

    A "pattern_key" identifies the kind of action (e.g.
    'lights_on_when_dark:living_room'). Each acceptance increments a counter;
    once it crosses the threshold the key is granted autonomy. Revoking
    resets it to Tier 0. Grants persist across restarts.
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

    def _save(self) -> None:
        try:
            _m_common.write_json_atomic(_autonomy_file(), self._grants, indent=2, encoding="utf-8")
        except Exception as exc:
            _LOGGER.warning("Autonomy grants save failed: %s", exc)

    def record_acceptance(self, pattern_key: str, confidence: float = 1.0) -> dict:
        """
        User accepted an offer. Increment its trust counter; may promote to
        autonomous. Returns the updated grant record.
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
        if (not g["granted"]
                and g["approvals"] >= AUTONOMY_TRUST_THRESHOLD
                and g["confidence"] >= AUTONOMY_MIN_CONFIDENCE):
            g["granted"] = True
            g["granted_at"] = dt_util.utcnow().isoformat()
            _LOGGER.info(
                "Autonomy GRANTED for '%s' after %d acceptances",
                pattern_key, g["approvals"],
            )
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
        """True if Nova may perform this convenience action without asking.
        The active operational mode can suppress convenience auto-actions
        (party/movie/lab) — this only affects graduated convenience patterns;
        safety events never route through here, so they act regardless of mode."""
        g = self._grants.get(pattern_key)
        if not (g and g.get("granted")):
            return False
        try:
            from . import modes
            if not modes.mode_allows_auto_actions():
                return False
        except Exception:
            pass
        return True

    def revoke(self, pattern_key: str) -> bool:
        """Manually revoke autonomy for a pattern (back to offer-only)."""
        if pattern_key in self._grants:
            self._grants[pattern_key]["granted"] = False
            self._grants[pattern_key]["approvals"] = 0
            self._grants[pattern_key]["revoked_at"] = dt_util.utcnow().isoformat()
            self._save()
            return True
        return False

    def list_grants(self) -> list[dict]:
        """All tracked patterns with their trust state."""
        out = []
        for key, g in self._grants.items():
            out.append({
                "pattern_key": key,
                "approvals": g.get("approvals", 0),
                "granted": g.get("granted", False),
                "threshold": AUTONOMY_TRUST_THRESHOLD,
                "confidence": round(g.get("confidence", 0.0), 2),
            })
        return out
