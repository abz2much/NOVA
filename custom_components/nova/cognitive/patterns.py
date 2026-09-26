"""Deterministic, explainable scoring for learned routines and sequences.

Pure: the analyzer reads the evidence from its own SQLite history (in the
executor, during its periodic analysis) and hands plain numbers here. Nothing
in this module reads a database, calls a provider or touches Home Assistant,
and nothing here runs on the state-change path.

Evidence used, and why:
  * observations        matching state changes in the 30-day window;
  * distinct days       days the behaviour actually happened. Many
                        repeats on one day are one day, never a routine;
  * eligible days       days anything was being recorded at all;
  * coverage            distinct days / eligible days (support for a
                        routine: "on 18 of 20 days");
  * sample size         a small number of days cannot reach full confidence;
  * time concentration  the share of the behaviour's days (within an hour
                        either side) that fall in this hour. Broad,
                        inconsistent timing is not a clean time trigger;
  * recency             a routine last seen weeks ago is not current;
  * provenance          rows an automation caused are excluded upstream;
                        when most of a behaviour is automation-caused, the
                        rest is not evidence of a manual routine;
  * conditional         for "after A, B": how often A is followed by B. A
    confidence          busy trigger that is rarely followed is association,
                        not a reason to automate. Temporal order is never
                        treated as proof of cause.

Feedback that already exists stays where it is: a dismissed suggestion stays
dismissed through its stable identity (suggestions.suggestion_identity), a
changed behaviour has a new identity, an installed or covered suggestion is
never re-proposed, and the opt-in adaptive threshold learns from how
suggestions were received. None of it lowers a threshold here.
"""
from __future__ import annotations

from dataclasses import dataclass

MIN_DISTINCT_DAYS = 3          # a routine spans at least this many days
MIN_COVERAGE = 0.3             # unchanged from the original detector
FULL_CONCENTRATION = 0.8       # this share of nearby days in one hour is clean
RECENT_DAYS = 7.0              # seen within a week: fully current
STALE_DAYS = 21.0              # not seen for three weeks: not a current routine
MAX_AUTOMATED_SHARE = 0.5      # mostly automation-caused: not a manual routine
MIN_CONDITIONAL = 0.3          # after A, B at least this often
FULL_CONDITIONAL = 0.6

R_ACCEPTED = "accepted"
R_TOO_FEW_OBSERVATIONS = "too_few_observations"
R_TOO_FEW_DAYS = "too_few_distinct_days"
R_LOW_COVERAGE = "low_coverage"
R_STALE = "stale"
R_AUTOMATION_CAUSED = "automation_caused"
R_WEAK_ASSOCIATION = "weak_association"


@dataclass(frozen=True, slots=True)
class RoutineEvidence:
    """One entity reaching one state in one hour of the day."""
    observations: int
    positive_days: int
    eligible_days: int
    window_days: int = 0           # distinct days within an hour either side
    days_since_last: float = 0.0
    automated_observations: int = 0


@dataclass(frozen=True, slots=True)
class SequenceEvidence:
    """Entity B reaching a state shortly after entity A reached one."""
    support: int                   # B events preceded by A within the window
    trigger_count: int             # A events in the history
    distinct_days: int


@dataclass(frozen=True, slots=True)
class PatternScore:
    confidence: float
    accepted: bool
    reason: str
    factors: tuple = ()            # ((name, value), ...) in a fixed order

    def evidence(self) -> dict:
        """Plain numbers for the stored pattern details (explainability)."""
        return {"score": self.confidence, "result": self.reason, **dict(self.factors)}


def _reject(reason: str, factors) -> PatternScore:
    return PatternScore(0.0, False, reason, tuple(factors))


def recency_factor(days_since_last: float) -> float:
    d = max(0.0, float(days_since_last))
    if d <= RECENT_DAYS:
        return 1.0
    if d >= STALE_DAYS:
        return 0.0
    return 1.0 - (d - RECENT_DAYS) / (STALE_DAYS - RECENT_DAYS)


def score_time_routine(ev: RoutineEvidence, *, min_occurrences: int) -> PatternScore:
    eligible = max(1, int(ev.eligible_days))
    positive = max(0, int(ev.positive_days))
    coverage = positive / eligible
    sample = min(1.0, positive / max(1, int(min_occurrences)))
    window = max(positive, int(ev.window_days or 0))
    concentration = positive / window if window else 0.0
    conc_factor = min(1.0, concentration / FULL_CONCENTRATION)
    recency = recency_factor(ev.days_since_last)
    total = ev.observations + ev.automated_observations
    automated_share = ev.automated_observations / total if total else 0.0
    factors = (
        ("observations", int(ev.observations)),
        ("distinct_days", positive),
        ("eligible_days", eligible),
        ("coverage", round(coverage, 3)),
        ("sample", round(sample, 3)),
        ("concentration", round(concentration, 3)),
        ("recency", round(recency, 3)),
        ("days_since_last", round(float(ev.days_since_last), 1)),
        ("automated_share", round(automated_share, 3)),
    )
    if ev.observations < min_occurrences:
        return _reject(R_TOO_FEW_OBSERVATIONS, factors)
    if positive < MIN_DISTINCT_DAYS:
        return _reject(R_TOO_FEW_DAYS, factors)
    if automated_share >= MAX_AUTOMATED_SHARE:
        return _reject(R_AUTOMATION_CAUSED, factors)
    if coverage < MIN_COVERAGE:
        return _reject(R_LOW_COVERAGE, factors)
    if recency <= 0.0:
        return _reject(R_STALE, factors)
    confidence = round(coverage * sample * conc_factor * recency, 3)
    return PatternScore(confidence, True, R_ACCEPTED, factors)


def score_sequence(ev: SequenceEvidence, *, min_occurrences: int) -> PatternScore:
    support = max(0, int(ev.support))
    triggers = max(support, int(ev.trigger_count))
    conditional = support / triggers if triggers else 0.0
    volume = min(1.0, support / (max(1, int(min_occurrences)) * 3))
    factors = (
        ("support", support),
        ("trigger_count", triggers),
        ("conditional", round(conditional, 3)),
        ("distinct_days", int(ev.distinct_days)),
        ("volume", round(volume, 3)),
    )
    if support < min_occurrences:
        return _reject(R_TOO_FEW_OBSERVATIONS, factors)
    if ev.distinct_days < MIN_DISTINCT_DAYS:
        return _reject(R_TOO_FEW_DAYS, factors)
    if conditional < MIN_CONDITIONAL:
        return _reject(R_WEAK_ASSOCIATION, factors)
    confidence = round(volume * min(1.0, conditional / FULL_CONDITIONAL), 3)
    return PatternScore(confidence, True, R_ACCEPTED, factors)
