"""The infrastructure audit: grade the health sensors the user listed and say
what is wrong in one sentence, in Nova's voice.

Only the sensors named in the "infrastructure_audit_sensors" setting are
read (8.28.0); with none listed the audit does nothing. Every check stands
alone, so one failing never stops the rest, and a sensor that cannot be
read is reported as such rather than passed or crashed on.

This module is loaded on its own by its tests, so it imports nothing from
the package.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

_UNKNOWN_STATES = {"unknown", "unavailable", "none", ""}

# Severity: the higher, the earlier it is said.
_SEV_CRITICAL = 3
_SEV_WARNING = 2
_SEV_INFO = 1

# Binary sensor classes where "on" is the fault and "off" is normal (a
# pending update, a low battery, tamper, smoke...). For every other class an
# "off" sensor is the fault: offline, not running, not connected.
_ON_MEANS_FAULT = {
    "problem", "update", "battery", "tamper", "safety", "smoke", "gas",
    "carbon_monoxide", "moisture", "heat", "cold",
}


@dataclass(frozen=True)
class _BinaryCheck:
    entity_id: str
    label: str
    bad_state: str   # the reading that is the fault (on or off)


@dataclass(frozen=True)
class _ThresholdCheck:
    entity_id: str
    label: str
    warn_above: float      # elevated past this
    critical_above: float  # critical past this
    unit: str = "percent"


@dataclass
class Finding:
    severity: int
    phrase: str      # a clause that stands alone, e.g. "root storage is at 97 percent"
    label: str = ""  # short name kept for recall, e.g. "root storage"


def _as_float(value: object) -> float | None:
    """A finite number, or None for anything else (booleans included)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _unreadable(state) -> bool:
    return str(state.state).lower() in _UNKNOWN_STATES


def _join(clauses: list[str]) -> str:
    if len(clauses) == 1:
        return clauses[0]
    return ", ".join(clauses[:-1]) + f", and {clauses[-1]}"


class InfrastructureTriage:
    """Read the listed sensors and build one spoken verdict.

    How each is graded:
      * measured in percent: elevated above 90, critical above 96;
      * a binary sensor: a fault when "off", or when "on" for a class where
        on is the trouble (problem, update, battery, tamper, smoke...);
      * anything else: only mentioned when it cannot be read.
    """

    PERCENT_WARN_ABOVE = 90.0
    PERCENT_CRITICAL_ABOVE = 96.0

    def __init__(self, hass, *, honorific: str = "sir", sensors=None) -> None:
        self.hass = hass
        self.honorific = honorific
        self.sensors = [str(e).strip() for e in (sensors or []) if str(e).strip()]

    def _checks(self) -> tuple[list, list]:
        """(threshold checks, binary checks) for each listed sensor that
        exists, once each."""
        thresholds: list[_ThresholdCheck] = []
        binaries: list[_BinaryCheck] = []
        for eid in dict.fromkeys(self.sensors):
            state = self.hass.states.get(eid)
            if state is None:
                continue   # not in this home
            attrs = getattr(state, "attributes", None) or {}
            label = str(attrs.get("friendly_name") or eid)
            if eid.startswith("binary_sensor."):
                fault = "on" if attrs.get("device_class") in _ON_MEANS_FAULT else "off"
                binaries.append(_BinaryCheck(eid, label, fault))
            elif attrs.get("unit_of_measurement") == "%":
                thresholds.append(_ThresholdCheck(eid, label, self.PERCENT_WARN_ABOVE,
                                                  self.PERCENT_CRITICAL_ABOVE))
            elif _unreadable(state):
                # Not graded otherwise, but a listed sensor gone dark is news.
                never = float("inf")
                thresholds.append(_ThresholdCheck(eid, label, never, never))
        return thresholds, binaries

    def _eval_threshold(self, check: _ThresholdCheck) -> Finding | None:
        try:
            state = self.hass.states.get(check.entity_id)
            if state is None:
                return None
            name = check.label
            if _unreadable(state):
                return Finding(_SEV_WARNING,
                               f"I can't read {name} — that sensor is unavailable", name)
            value = _as_float(state.state)
            if value is None:
                return Finding(_SEV_WARNING, f"{name} is reporting an unreadable value", name)
            if value > check.critical_above:
                return Finding(_SEV_CRITICAL,
                               f"{name} is critically high at {value:.0f} {check.unit}", name)
            if value > check.warn_above:
                return Finding(_SEV_WARNING,
                               f"{name} is elevated at {value:.0f} {check.unit}", name)
            return None
        except Exception:  # noqa: BLE001 - one probe never stops the audit
            _LOGGER.exception("Triage threshold check failed for %s", check.entity_id)
            return None

    def _eval_binary(self, check: _BinaryCheck) -> Finding | None:
        try:
            state = self.hass.states.get(check.entity_id)
            if state is None:
                return None
            name = check.label
            if _unreadable(state):
                return Finding(_SEV_WARNING,
                               f"{name} has dropped offline and can't be reached", name)
            if str(state.state).lower() != check.bad_state.lower():
                return None
            what = "a problem" if check.bad_state == "on" else "offline"
            return Finding(_SEV_CRITICAL, f"{name} is reporting {what}", name)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Triage binary check failed for %s", check.entity_id)
            return None

    def evaluate(self) -> dict:
        """Run every check. Returns ``alert_required`` (anything at warning or
        worse), ``message`` (the sentence, "" when all is well),
        ``critical`` and ``tags`` (each finding's label once, for recall)."""
        thresholds, binaries = self._checks()
        findings = [f for f in (*map(self._eval_threshold, thresholds),
                                *map(self._eval_binary, binaries)) if f is not None]
        if not findings:
            return {"alert_required": False, "message": "", "critical": False, "tags": []}
        critical = any(f.severity >= _SEV_CRITICAL for f in findings)
        findings.sort(key=lambda f: f.severity, reverse=True)   # worst first
        tags = list(dict.fromkeys(f.label for f in findings if f.label))
        return {"alert_required": True, "message": self._compose(findings, critical),
                "critical": critical, "tags": tags}

    def _compose(self, findings: list[Finding], critical: bool) -> str:
        body = _join([f.phrase for f in findings])
        body = body[:1].upper() + body[1:]
        # The honorific is "" when nobody in particular is home (honorific.py).
        # persona.lead_in cannot be used: this module has no package context.
        honorific = (self.honorific or "").strip()
        if honorific:
            title = " ".join(word.capitalize() for word in honorific.split())
            note = ("infrastructure attention is required." if critical
                    else "a minor infrastructure note.")
            lead = f"{title}, {note}"
        else:
            lead = ("Infrastructure attention is required." if critical
                    else "A minor infrastructure note.")
        return f"{lead} {body}."
