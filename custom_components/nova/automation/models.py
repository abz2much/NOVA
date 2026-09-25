"""Typed models shared by Nova's automation capability.

Stdlib only, so every other automation module (and the unit tests) can import
it without Home Assistant. The string constants are the exact values Nova
already stores in SQLite and sends to the panel; they are constants rather
than Enums so a stored or wire value can never drift from its name.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

# ── Match classifications (automation_match["status"]) ──────────────────────
MATCH_NEW = "new"
MATCH_EXACT = "already_automated"
MATCH_OVERLAP = "possible_overlap"
MATCH_OPAQUE = "unknown_overlap"
MATCH_ADVISORY = "advisory"
MATCH_UNAVAILABLE = "inventory_unavailable"

# ── Suggestion statuses (suggestions.status) ────────────────────────────────
SUGGESTION_PENDING = "pending"
SUGGESTION_APPROVED = "approved"
SUGGESTION_DISMISSED = "dismissed"
SUGGESTION_INSTALLED = "installed"
SUGGESTION_COVERED = "already_automated"

# ── Provenance of an observed state change (state_changes.triggered_by) ─────
SOURCE_UNKNOWN = "unknown"
SOURCE_USER = "user"
SOURCE_AUTOMATION = "automation"
SOURCE_NOVA_AUTOMATION = "nova_automation"
SOURCE_DEVICE_OR_INTEGRATION = "device_or_integration"
AUTOMATED_SOURCES = (SOURCE_AUTOMATION, SOURCE_NOVA_AUTOMATION)


@dataclass(slots=True)
class AutomationRecord:
    """One loaded automation. ``raw_config`` never leaves the backend."""

    entity_id: str
    unique_id: str = ""
    name: str = ""
    enabled: bool = False
    last_triggered: Any = None
    origin: str = "existing"
    understanding: str = "metadata_only"
    blueprint: Optional[str] = None
    referenced_entities: tuple[str, ...] = ()
    referenced_devices: tuple[str, ...] = ()
    referenced_areas: tuple[str, ...] = ()
    raw_config: Any = field(default=None, repr=False, compare=False)

    def public_dict(self) -> dict[str, Any]:
        """A bounded panel-safe representation; never expose raw automation data."""
        return {
            "entity_id": self.entity_id,
            "unique_id": self.unique_id,
            "name": self.name or self.entity_id,
            "enabled": bool(self.enabled),
            "last_triggered": self.last_triggered,
            "origin": self.origin,
            "understanding": self.understanding,
            "blueprint": self.blueprint,
            "referenced_entities": list(self.referenced_entities),
            "referenced_devices": list(self.referenced_devices),
            "referenced_areas": list(self.referenced_areas),
        }


@dataclass(frozen=True, slots=True)
class SourceAttribution:
    """Bounded provenance attached to one observed state change."""

    kind: str = "unknown"
    entity_id: str = ""
    confidence: float = 0.0


@dataclass
class DetectedPattern:
    pattern_type: str      # time_routine, sequence, repeated_command, temp_pref, presence
    description: str
    entity_ids: list[str]
    confidence: float
    occurrences: int
    coverage: float = 0.0  # positive days / opportunity days (0 = not computed)
    details: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MatchRef:
    """One loaded automation named by a classification."""

    entity_id: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"entity_id": self.entity_id, "name": self.name}


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Deterministic comparison of a candidate with the loaded automations."""

    status: str
    matches: tuple[MatchRef, ...] = ()
    reason: str = ""

    @property
    def is_exact(self) -> bool:
        return self.status == MATCH_EXACT

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status,
                "matches": [m.to_dict() for m in self.matches],
                "reason": self.reason}


@dataclass(frozen=True, slots=True)
class AdvisoryPayload:
    """A suggestion payload that cannot become an automation."""

    reason: str

    def to_legacy_dict(self) -> dict[str, Any]:
        return {"installable": False, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class NormalizedAutomation:
    """An installable suggestion payload in Home Assistant's modern shape."""

    alias: str
    triggers: tuple = ()
    conditions: tuple = ()
    actions: tuple = ()

    def to_candidate(self) -> dict[str, list]:
        """The behavioural config the classifier compares."""
        return {"triggers": list(self.triggers),
                "conditions": list(self.conditions),
                "actions": list(self.actions)}

    def to_legacy_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"installable": True, "alias": self.alias,
                               "trigger": list(self.triggers),
                               "action": list(self.actions)}
        if self.conditions:
            out["condition"] = list(self.conditions)
        return out

    @classmethod
    def from_legacy_dict(cls, data: dict) -> "NormalizedAutomation | AdvisoryPayload":
        if not data.get("installable"):
            return AdvisoryPayload(str(data.get("reason") or "not installable"))
        return cls(alias=data["alias"], triggers=tuple(data["trigger"]),
                   conditions=tuple(data.get("condition") or ()),
                   actions=tuple(data["action"]))


@dataclass(frozen=True, slots=True)
class InstallationRequest:
    """One request to add an automation to Home Assistant."""

    alias: str
    description: str
    triggers: tuple
    conditions: tuple
    actions: tuple
    mode: str
    request_id: str
    source: str
    requested_by_user_id: Optional[str] = None
    requested_by_name: Optional[str] = None
    request_device_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class InstallationResult:
    """Outcome of one installation. ``reason_code`` is for the action audit
    log only and never appears in the returned dictionary."""

    success: bool
    automation_id: str = ""
    alias: str = ""
    error: str = ""
    reason_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        if self.success:
            return {"success": True, "automation_id": self.automation_id,
                    "alias": self.alias}
        return {"success": False, "error": self.error}


def loads_json(text: Any, default: Any) -> Any:
    """Decode a JSON column, falling back to ``default`` on any problem."""
    try:
        return json.loads(text) if text else default
    except Exception:
        return default
