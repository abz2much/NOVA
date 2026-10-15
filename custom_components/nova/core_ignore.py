"""The ignore system: "ignore X for Y minutes" rules, and the outdoor event
filter delegate.

Moved out of cognitive_core.py unchanged in 8.7.17. cognitive_core.py
still exports every name defined here, as the same object.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import time
from dataclasses import dataclass

from . import core_common as _m_common
from .core_bridge import _ignore_file

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


# ── Ignore System ───────────────────────────────────────────────────────────

# Every ignore rule expires (8.7.20). A request with no duration (or 0) gets
# the same default as nova.nap's manual mute: 30 minutes.
DEFAULT_IGNORE_MINUTES = 30

# Entity ids of different kinds. A pattern that matches all of them, or has no
# letter or digit at all ("*", "*.*", "?*"), would mute the whole house.
_EVERYTHING_PROBES = ("lock.front_door", "binary_sensor.kitchen_leak",
                      "alarm_control_panel.home", "sensor.outdoor_temperature",
                      "cover.garage_door", "light.x")


def matches_everything(pattern: str) -> bool:
    """True for a pattern that would ignore every entity (8.7.20)."""
    text = str(pattern or "").strip()
    if not re.search(r"[A-Za-z0-9]", text):
        return True
    return all(fnmatch.fnmatch(eid, text) for eid in _EVERYTHING_PROBES)


@dataclass
class IgnoreRule:
    entity_pattern: str   # an entity_id or a glob ("binary_sensor.garage*")
    reason: str
    expires_at: float     # unix time; 0 would mean never (no longer written)
    created_at: float = 0.0

    def is_expired(self) -> bool:
        return self.expires_at != 0 and time.time() > self.expires_at

    def matches(self, entity_id: str) -> bool:
        return fnmatch.fnmatch(entity_id, self.entity_pattern)

    def as_stored(self) -> dict:
        return {"entity_pattern": self.entity_pattern, "reason": self.reason,
                "expires_at": self.expires_at, "created_at": self.created_at}


class IgnoreManager:
    """The "ignore X for Y minutes" rules, kept in a JSON file."""

    def __init__(self):
        self._rules: list[IgnoreRule] = []
        self._load()

    def _load(self):
        """Read the saved rules, dropping expired ones and any that would mute
        the whole house. A rule from an older version with no expiry gets
        the default from now (8.7.20). An unreadable file means no rules."""
        try:
            path = _ignore_file()
            if not os.path.exists(path):
                return
            with open(path) as fh:
                stored = json.load(fh)
            kept = []
            for raw in stored:
                rule = IgnoreRule(**raw)
                if rule.is_expired() or matches_everything(raw.get("entity_pattern", "")):
                    continue
                kept.append(rule)
            for rule in kept:
                if rule.expires_at == 0:
                    rule.expires_at = time.time() + DEFAULT_IGNORE_MINUTES * 60
            self._rules = kept
        except Exception:
            self._rules = []

    def _prune(self) -> list[IgnoreRule]:
        self._rules = [rule for rule in self._rules if not rule.is_expired()]
        return self._rules

    def _save(self):
        live = [rule.as_stored() for rule in self._rules if not rule.is_expired()]
        try:
            _m_common.write_json_atomic(_ignore_file(), live, indent=2)
        except Exception as exc:
            _LOGGER.warning("Failed to save ignore rules: %s", exc)

    def add(self, entity_pattern: str, duration_minutes: int = 0,
            reason: str = "") -> IgnoreRule:
        """Ignore matching entities for ``duration_minutes`` (the default 30
        when 0 or less). A pattern that covers everything is refused."""
        if matches_everything(entity_pattern):
            raise ValueError(
                f"'{entity_pattern}' would ignore every entity in the home; name the "
                f"entity or a narrower pattern")
        minutes = duration_minutes if duration_minutes > 0 else DEFAULT_IGNORE_MINUTES
        now = time.time()
        rule = IgnoreRule(entity_pattern, reason, now + minutes * 60, now)
        self._rules.append(rule)
        self._save()
        _LOGGER.info("Cognitive: ignore '%s' for %s (%s)", entity_pattern,
                     f"{minutes}min" if minutes else "indefinitely", reason)
        return rule

    def remove(self, entity_pattern: str) -> bool:
        kept = [rule for rule in self._rules if rule.entity_pattern != entity_pattern]
        if len(kept) == len(self._rules):
            return False
        self._rules = kept
        self._save()
        return True

    def clear_all(self):
        self._rules.clear()
        self._save()

    def is_ignored(self, entity_id: str) -> bool:
        return any(rule.matches(entity_id) for rule in self._prune())

    def list_rules(self) -> list[dict]:
        now = time.time()
        return [{
            "pattern": rule.entity_pattern,
            "reason": rule.reason,
            "expires_at": rule.expires_at,
            "remaining_min": (max(0, int((rule.expires_at - now) / 60))
                              if rule.expires_at > 0 else "permanent"),
        } for rule in self._prune()]


# ── outdoor events ──────────────────────────────────────────────────────────
# outdoor.py owns the classifier and the notable event policy; this delegate
# stays for the names that import it from here.

def is_outdoor_notable(entity_id: str, area_name: str,
                       detection_type: str = "motion") -> bool:
    """Whether an outdoor event is worth raising (outdoor.py decides)."""
    from . import outdoor
    return outdoor.notable(None, entity_id, detection_type, area_name=area_name)
