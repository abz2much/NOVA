"""Infrastructure triage for Nova.

``InfrastructureTriage`` reads the health sensors the user listed straight off
the Home Assistant state machine, grades each against thresholds, and synthesises a
single natural-language verdict in Nova's voice. It is intentionally
defensive: every check is isolated, and unreadable/unknown states are reported
as a *degraded-visibility* warning rather than silently passing or crashing.

The checks are built from the listed sensors on every run.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# States that mean "Home Assistant cannot currently read this entity".
_UNKNOWN_STATES = {"unknown", "unavailable", "none", ""}

# Severity ranking for ordering the spoken summary (higher = spoken first).
_SEV_CRITICAL = 3
_SEV_WARNING = 2
_SEV_INFO = 1


@dataclass
class Finding:
    severity: int
    phrase: str  # a self-contained spoken clause, e.g. "root storage is at 97 percent"
    label: str = ""  # short tag for memory recall, e.g. "root storage"


@dataclass
class _ThresholdCheck:
    entity_id: str
    label: str
    warn_above: float
    critical_above: float
    unit: str = "percent"


@dataclass
class _BinaryCheck:
    entity_id: str
    label: str
    bad_state: str  # the state that constitutes a fault, e.g. "off"


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


class InfrastructureTriage:
    """Evaluate the infrastructure sensors the user listed and produce a
    spoken verdict.

    8.28.0: the sensors come from the "infrastructure_audit_sensors" setting.
    It starts empty, and with nothing listed the audit does nothing. (Before,
    a fixed set of one particular home's server, switch and freeze sensor
    ids was checked.) Each listed sensor is graded by what it is:
      * a sensor measured in percent: elevated above 90, critical above 96
      * a binary sensor: a fault when "off", or when "on" for the problem
        device class
      * anything else: only reported when it cannot be read
    """

    PERCENT_WARN_ABOVE = 90.0
    PERCENT_CRITICAL_ABOVE = 96.0

    def __init__(self, hass, *, honorific: str = "sir", sensors=None) -> None:
        self.hass = hass
        self.honorific = honorific
        self.sensors = [str(e).strip() for e in (sensors or []) if str(e).strip()]

    def _checks(self) -> tuple[list, list]:
        """(threshold checks, binary checks) for the listed sensors that exist."""
        thresholds: list[_ThresholdCheck] = []
        binaries: list[_BinaryCheck] = []
        seen: set = set()
        for eid in self.sensors:
            if eid in seen:
                continue
            seen.add(eid)
            state = self.hass.states.get(eid)
            if state is None:
                continue  # not part of this home's setup
            attrs = getattr(state, "attributes", None) or {}
            label = str(attrs.get("friendly_name") or eid)
            if eid.startswith("binary_sensor."):
                bad = "on" if attrs.get("device_class") == "problem" else "off"
                binaries.append(_BinaryCheck(eid, label, bad))
            elif attrs.get("unit_of_measurement") == "%":
                thresholds.append(_ThresholdCheck(
                    eid, label, warn_above=self.PERCENT_WARN_ABOVE,
                    critical_above=self.PERCENT_CRITICAL_ABOVE))
            elif str(state.state).lower() in _UNKNOWN_STATES:
                # Not graded, but a listed sensor that cannot be read is
                # still worth a warning.
                thresholds.append(_ThresholdCheck(
                    eid, label, warn_above=float("inf"), critical_above=float("inf")))
        return thresholds, binaries

    # ── Individual checks (each fully guarded) ────────────────────────────
    def _eval_threshold(self, check: _ThresholdCheck) -> Finding | None:
        try:
            state = self.hass.states.get(check.entity_id)
            if state is None:
                return None  # not part of this home's setup
            if str(state.state).lower() in _UNKNOWN_STATES:
                return Finding(
                    _SEV_WARNING,
                    f"I can't read {check.label} — that sensor is unavailable",
                    check.label,
                )
            pct = _as_float(state.state)
            if pct is None:
                return Finding(
                    _SEV_WARNING,
                    f"{check.label} is reporting an unreadable value",
                    check.label,
                )
            if pct > check.critical_above:
                return Finding(
                    _SEV_CRITICAL,
                    f"{check.label} is critically high at {pct:.0f} {check.unit}",
                    check.label,
                )
            if pct > check.warn_above:
                return Finding(
                    _SEV_WARNING,
                    f"{check.label} is elevated at {pct:.0f} {check.unit}",
                    check.label,
                )
            return None
        except Exception:  # noqa: BLE001 - never let one probe abort the audit
            _LOGGER.exception("Triage threshold check failed for %s", check.entity_id)
            return None

    def _eval_binary(self, check: _BinaryCheck) -> Finding | None:
        try:
            state = self.hass.states.get(check.entity_id)
            if state is None:
                return None  # not part of this home's setup
            value = str(state.state).lower()
            if value in _UNKNOWN_STATES:
                return Finding(
                    _SEV_WARNING,
                    f"{check.label} has dropped offline and can't be reached",
                    check.label,
                )
            if value == check.bad_state.lower():
                what = "a problem" if check.bad_state == "on" else "offline"
                return Finding(
                    _SEV_CRITICAL,
                    f"{check.label} is reporting {what}",
                    check.label,
                )
            return None
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Triage binary check failed for %s", check.entity_id)
            return None

    # ── Aggregation ───────────────────────────────────────────────────────
    def evaluate(self) -> dict:
        """Run every probe and synthesise the verdict.

        Returns a dict with:
            alert_required (bool) – any finding at warning severity or above
            message        (str)  – a single natural-language summary ('' if clear)
            critical       (bool) – any finding at critical severity
        """
        findings: list[Finding] = []
        thresholds, binaries = self._checks()
        for tcheck in thresholds:
            if (f := self._eval_threshold(tcheck)) is not None:
                findings.append(f)
        for bcheck in binaries:
            if (f := self._eval_binary(bcheck)) is not None:
                findings.append(f)

        if not findings:
            return {"alert_required": False, "message": "", "critical": False, "tags": []}

        critical = any(f.severity >= _SEV_CRITICAL for f in findings)
        # Speak the most severe items first.
        findings.sort(key=lambda f: f.severity, reverse=True)
        message = self._compose(findings, critical)
        # De-duplicated finding labels, for memory recall / commit.
        tags: list[str] = []
        for f in findings:
            if f.label and f.label not in tags:
                tags.append(f.label)
        return {"alert_required": True, "message": message, "critical": critical, "tags": tags}

    def _compose(self, findings: list[Finding], critical: bool) -> str:
        clauses = [f.phrase for f in findings]

        if len(clauses) == 1:
            body = clauses[0]
        elif len(clauses) == 2:
            body = f"{clauses[0]}, and {clauses[1]}"
        else:
            body = ", ".join(clauses[:-1]) + f", and {clauses[-1]}"

        # honorific may be "" once nobody specific is home to address (see
        # honorific.py). No relative import of persona.lead_in here — this
        # module is loaded standalone (no package context) by its own tests,
        # see tests/unit/test_infrastructure_triage.py.
        h = (self.honorific or "").strip()
        if h:
            note = "infrastructure attention is required." if critical else "a minor infrastructure note."
            lead = f"{h.title()}, {note}"
        else:
            lead = "Infrastructure attention is required." if critical else "A minor infrastructure note."
        # Capitalise the first clause for a clean sentence.
        body = body[0].upper() + body[1:] if body else body
        return f"{lead} {body}."
