"""The ignore system: "ignore X for Y minutes" rules, and the outdoor event
filter delegate.

Moved out of cognitive_core.py unchanged in 8.7.17. cognitive_core.py
still exports every name defined here, as the same object.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

from . import core_common as _m_common
from .core_bridge import _ignore_file

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


# ── Ignore System ───────────────────────────────────────────────────────────

@dataclass
class IgnoreRule:
    entity_pattern: str   # entity_id or glob pattern ("binary_sensor.garage*")
    reason: str
    expires_at: float     # unix timestamp, 0 = permanent until cleared
    created_at: float = 0.0

    def is_expired(self) -> bool:
        if self.expires_at == 0:
            return False
        return time.time() > self.expires_at

    def matches(self, entity_id: str) -> bool:
        import fnmatch
        return fnmatch.fnmatch(entity_id, self.entity_pattern)


class IgnoreManager:
    """Manages entity/event ignore rules with duration."""

    def __init__(self):
        self._rules: list[IgnoreRule] = []
        self._load()

    def _load(self):
        try:
            if os.path.exists(_ignore_file()):
                with open(_ignore_file()) as f:
                    data = json.load(f)
                self._rules = [
                    IgnoreRule(**r) for r in data
                    if not IgnoreRule(**r).is_expired()
                ]
        except Exception:
            self._rules = []

    def _save(self):
        try:
            data = [
                {
                    "entity_pattern": r.entity_pattern,
                    "reason": r.reason,
                    "expires_at": r.expires_at,
                    "created_at": r.created_at,
                }
                for r in self._rules if not r.is_expired()
            ]
            _m_common.write_json_atomic(_ignore_file(), data, indent=2)
        except Exception as exc:
            _LOGGER.warning("Failed to save ignore rules: %s", exc)

    def add(self, entity_pattern: str, duration_minutes: int = 0,
            reason: str = "") -> IgnoreRule:
        expires = (time.time() + duration_minutes * 60) if duration_minutes > 0 else 0
        rule = IgnoreRule(
            entity_pattern=entity_pattern,
            reason=reason,
            expires_at=expires,
            created_at=time.time(),
        )
        self._rules.append(rule)
        self._save()
        _LOGGER.info(
            "Cognitive: ignore '%s' for %s (%s)",
            entity_pattern,
            f"{duration_minutes}min" if duration_minutes else "indefinitely",
            reason,
        )
        return rule

    def remove(self, entity_pattern: str) -> bool:
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.entity_pattern != entity_pattern]
        if len(self._rules) < before:
            self._save()
            return True
        return False

    def clear_all(self):
        self._rules.clear()
        self._save()

    def is_ignored(self, entity_id: str) -> bool:
        self._rules = [r for r in self._rules if not r.is_expired()]
        return any(r.matches(entity_id) for r in self._rules)

    def list_rules(self) -> list[dict]:
        self._rules = [r for r in self._rules if not r.is_expired()]
        return [
            {
                "pattern": r.entity_pattern,
                "reason": r.reason,
                "expires_at": r.expires_at,
                "remaining_min": max(0, int((r.expires_at - time.time()) / 60))
                if r.expires_at > 0 else "permanent",
            }
            for r in self._rules
        ]


# ── Outdoor Event Filter ────────────────────────────────────────────────────
# The classifier and notable-event policy live in outdoor.py (the single source
# of truth — the intrusion paths below use it too). Thin delegates kept here for
# import compatibility.

def is_outdoor_notable(entity_id: str, area_name: str,
                       detection_type: str = "motion") -> bool:
    """Decide if an outdoor event is worth surfacing. Delegates to outdoor.py."""
    from . import outdoor
    return outdoor.notable(None, entity_id, detection_type, area_name=area_name)
