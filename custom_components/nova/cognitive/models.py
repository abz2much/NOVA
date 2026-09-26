"""Typed, immutable models for Nova's cognitive decisions.

Stdlib only, so every evaluator and every test can use them without Home
Assistant. The string constants are the exact values that reach logs,
diagnostics and the legacy decision dicts; they are constants rather than
Enums so a value on the wire can never drift from its name.

Confidence is only ever carried when the source genuinely supplies one (a
recognition score, an attribution certainty). Nothing here invents one to
fill a field.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional

# ── Actions ────────────────────────────────────────────────────────────────
ACTION_SPEAK = "speak"
ACTION_SILENT = "stay_silent"
ACTION_DEFER = "defer"          # no local rule applies; the cache or provider decides
ACTION_CLARIFY = "clarify"
ACTION_ESCALATE = "escalate"
ACTIONS = (ACTION_SPEAK, ACTION_SILENT, ACTION_DEFER, ACTION_CLARIFY, ACTION_ESCALATE)

# ── Urgency ────────────────────────────────────────────────────────────────
URGENCY_LOW = "low"
URGENCY_MEDIUM = "medium"
URGENCY_HIGH = "high"
URGENCY_CRITICAL = "critical"
URGENCIES = (URGENCY_LOW, URGENCY_MEDIUM, URGENCY_HIGH, URGENCY_CRITICAL)

# ── Where a decision came from ─────────────────────────────────────────────
ORIGIN_RULE = "rule"            # a local deterministic evaluator
ORIGIN_CACHE = "cache"          # replayed from a learned provider decision
ORIGIN_PROVIDER = "provider"    # a validated provider reply
ORIGIN_LOCAL_MIND = "local_mind"
ORIGIN_FALLBACK = "fallback"    # the last-ditch urgency switch

# ── Evidence sources, provenance and trust ─────────────────────────────────
SOURCE_STATE = "state_machine"
SOURCE_CLASSIFIER = "classifier"
SOURCE_ANNOUNCEMENTS = "announcement_log"
SOURCE_HISTORY = "history"
SOURCE_CASE_MEMORY = "case_memory"
SOURCE_PROVIDER = "provider"
SOURCE_ATTRIBUTION = "attribution"

PROVENANCE_DEVICE_CLASS = "device_class"
PROVENANCE_STATE = "state"
PROVENANCE_NAME = "name_text"       # inferred from an entity's name or summary text
PROVENANCE_CLASSIFIER = "classifier_rule"
PROVENANCE_CONTEXT = "ha_context"

TRUST_OBSERVED = "observed"     # read directly from Home Assistant state
TRUST_DERIVED = "derived"       # computed from observed facts by a rule
TRUST_HEURISTIC = "heuristic"   # a text or naming heuristic; weakest
TRUST_REPORTED = "reported"     # asserted by a provider; never proof

# ── Reason codes ───────────────────────────────────────────────────────────
R_HAZARD_ACTIVE = "hazard_active"
R_HAZARD_INACTIVE = "hazard_inactive"
R_RECENTLY_ANNOUNCED = "recently_announced"
R_ALARM_TRIGGERED = "alarm_triggered"
R_SECURITY_OCCUPIED = "security_normal_occupied"
R_ARRIVAL = "arrival"
R_DEPARTURE_AUDIENCE = "departure_with_audience"
R_LAST_DEPARTURE = "last_person_departure"
R_ENTRY_OPEN_OCCUPIED = "entry_open_occupied"
R_ENTRY_OPEN = "entry_open"
R_ENTRY_LOW = "entry_open_low_urgency"
R_ENTRY_CLOSED = "entry_closed"
R_LOCK_OCCUPIED = "unlocked_occupied"
R_LOCK_AWAY = "unlocked_away"
R_LOCK_SECURED = "lock_secured"
R_MOTION = "motion"
R_MOTION_ROUTINE = "motion_routine"
R_GARAGE_OPEN = "garage_open"
R_GARAGE_CLOSED = "garage_closed"
R_BATTERY_LOW = "battery_low"
R_APPLIANCE_DONE = "appliance_done"
R_CLIMATE_EXTREME = "climate_extreme"
R_CLIMATE_NORMAL = "climate_normal"
R_POWER_SPIKE = "power_spike"
R_LOW_URGENCY = "low_urgency"
R_NO_RULE = "no_local_rule"
R_CACHE_SILENT = "learned_silent"
R_CACHE_SPEAK = "learned_speak"
R_PROVIDER_SPEAK = "provider_speak"
R_PROVIDER_SILENT = "provider_silent"
R_LOCAL_MIND = "local_mind"
R_FALLBACK_URGENT = "fallback_urgent"
R_FALLBACK_QUIET = "fallback_quiet"

# ── Provider failure kinds (never carry the reply's content) ────────────────
FAIL_UNREADABLE = "unreadable"      # not a JSON object at all
FAIL_INCOMPLETE = "incomplete"      # a JSON object missing a required field
FAIL_INVALID = "invalid_field"      # a required field of the wrong type
FAIL_EMPTY_MESSAGE = "empty_message"
FAIL_TRANSPORT = "transport"        # the call itself raised
FAILURE_KINDS = (FAIL_UNREADABLE, FAIL_INCOMPLETE, FAIL_INVALID,
                 FAIL_EMPTY_MESSAGE, FAIL_TRANSPORT)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


@dataclass(frozen=True, slots=True)
class Evidence:
    """One fact a decision rests on, with where it came from and how far it
    can be trusted. ``age_s`` is a bounded age when the source has one;
    ``confidence`` is only set when the source supplies it."""

    source: str
    category: str
    entity_id: str = ""
    device_class: str = ""
    transition: tuple[str, str] = ("", "")
    age_s: Optional[float] = None
    confidence: Optional[float] = None
    provenance: str = ""
    trust: str = TRUST_OBSERVED

    def __post_init__(self):
        if self.confidence is not None and not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be within [0, 1] when supplied")
        if self.age_s is not None and float(self.age_s) < 0:
            raise ValueError("age must not be negative")


@dataclass(frozen=True, slots=True)
class EventSnapshot:
    """Everything a proactive decision may look at, collected once.

    Built by the coordinator from what the observer already gathered; the
    evaluators read it and nothing else. ``recent_announcements`` is a
    tuple, so no evaluator can change the caller's list."""

    entity_id: str = ""
    domain: str = ""
    device_class: str = ""
    from_state: str = ""
    to_state: str = ""
    friendly_name: str = ""
    event_summary: str = ""
    category: str = ""
    urgency: str = ""
    anyone_home: bool = False
    recent_announcements: tuple[str, ...] = ()

    @classmethod
    def build(cls, *, entity_id="", device_class="", from_state="", to_state="",
              friendly_name="", event_summary="", category="", urgency="",
              anyone_home=False, recent_announcements=(), domain=None) -> "EventSnapshot":
        eid = _text(entity_id)
        dom = _text(domain) if domain is not None else (
            eid.split(".", 1)[0] if "." in eid else "")
        return cls(
            entity_id=eid, domain=dom, device_class=_text(device_class),
            from_state=_text(from_state), to_state=_text(to_state),
            friendly_name=_text(friendly_name), event_summary=_text(event_summary),
            category=_text(category), urgency=_text(urgency),
            anyone_home=bool(anyone_home),
            recent_announcements=tuple(_text(a) for a in (recent_announcements or ())))

    @property
    def transition(self) -> tuple[str, str]:
        return (self.from_state, self.to_state)

    @property
    def summary_lower(self) -> str:
        return self.event_summary.lower()

    def state_evidence(self, category: str, *, provenance: str = PROVENANCE_STATE,
                       trust: str = TRUST_OBSERVED) -> Evidence:
        return Evidence(source=SOURCE_STATE, category=category, entity_id=self.entity_id,
                        device_class=self.device_class, transition=self.transition,
                        provenance=provenance, trust=trust)


@dataclass(frozen=True, slots=True)
class Phrase:
    """How to say a decision, resolved to words only by presentation.

    ``kind`` names a composer; ``params`` are its arguments as sorted
    (key, value) pairs so the phrase stays hashable and immutable."""

    kind: str
    params: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def of(cls, kind: str, **params) -> "Phrase":
        return cls(kind, tuple(sorted(params.items())))

    def get(self, key: str, default: Any = None) -> Any:
        for k, v in self.params:
            if k == key:
                return v
        return default


@dataclass(frozen=True, slots=True)
class Decision:
    """A typed proactive decision.

    ``reason`` and ``urgency`` are None when the legacy decision dict never
    carried them, so ``as_dict`` reproduces the exact shapes observer,
    diagnostics and tests have always seen. ``cacheable`` is only ever True
    for a validated provider decision (see cache_policy)."""

    action: str
    reason_code: str
    urgency: Optional[str] = None
    reason: Optional[str] = None
    evidence: tuple[Evidence, ...] = ()
    evaluator: str = ""
    origin: str = ORIGIN_RULE
    provider_used: bool = False
    cacheable: bool = False
    validated: bool = True
    phrase: Optional[Phrase] = None
    message: str = ""
    explanation: str = ""
    provider_failure: str = ""
    extra: tuple[tuple[str, Any], ...] = field(default=(), compare=False)

    def __post_init__(self):
        if self.action not in ACTIONS:
            raise ValueError(f"unknown action {self.action!r}")
        if self.urgency is not None and not isinstance(self.urgency, str):
            raise TypeError("urgency must be a string or None")
        if self.cacheable and not (self.validated and self.origin == ORIGIN_PROVIDER):
            raise ValueError("only a validated provider decision may be cacheable")
        if self.provider_failure and self.provider_failure not in FAILURE_KINDS:
            raise ValueError(f"unknown provider failure {self.provider_failure!r}")

    @property
    def speak(self) -> bool:
        return self.action in (ACTION_SPEAK, ACTION_ESCALATE)

    @property
    def is_critical(self) -> bool:
        return self.speak and self.urgency == URGENCY_CRITICAL

    def with_message(self, message: str) -> "Decision":
        return replace(self, message=message)

    def with_(self, **changes) -> "Decision":
        return replace(self, **changes)

    def as_dict(self) -> dict:
        """The legacy decision dict: speak, then message (when speaking),
        urgency and reason when this decision carries them."""
        out: dict = {"speak": self.speak}
        if self.speak:
            out["message"] = self.message
        if self.urgency is not None:
            out["urgency"] = self.urgency
        if self.reason is not None:
            out["reason"] = self.reason
        for k, v in self.extra:
            out[k] = v
        return out


def silent(reason_code: str, reason: Optional[str], *, evaluator: str = "",
           urgency: Optional[str] = None, evidence: tuple = (),
           origin: str = ORIGIN_RULE) -> Decision:
    return Decision(ACTION_SILENT, reason_code, urgency=urgency, reason=reason,
                    evidence=tuple(evidence), evaluator=evaluator, origin=origin)


def speak(reason_code: str, urgency: str, phrase: Phrase, *, reason: Optional[str] = None,
          evaluator: str = "", evidence: tuple = (), origin: str = ORIGIN_RULE) -> Decision:
    return Decision(ACTION_SPEAK, reason_code, urgency=urgency, reason=reason,
                    evidence=tuple(evidence), evaluator=evaluator, origin=origin,
                    phrase=phrase)


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """A provider reply that is not a usable decision.

    Carries only the failure kind, a safe detail naming what was wrong
    (never the reply's words) and the reply's length for diagnostics."""

    kind: str
    detail: str = ""
    reply_length: int = 0

    def __post_init__(self):
        if self.kind not in FAILURE_KINDS:
            raise ValueError(f"unknown provider failure {self.kind!r}")


def freeze_mapping(values: Optional[Mapping[str, Any]]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted((values or {}).items()))
