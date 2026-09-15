"""Offline replay / policy evaluation over recorded Decision Records (v7.48.0).

Re-applies a candidate decision policy to the decisions Nova already made and,
for the ones with a known outcome, reports how the calls would change and whether
accuracy improves — so a threshold change can be evaluated against real history
before it ships.

The policy modelled here is the general one every tier shares: *act only when
confidence ≥ threshold*. Each recorded decision carries a confidence and, once
judged, an outcome — one of decision_record.py's real verdicts: ``"good"``,
``"unnecessary"``, or ``"wrong"`` (see decision_record.OUTCOME_GOOD etc.).
``"good"`` is the positive class, matching decision_record.calibration()'s own
convention exactly; ``"unnecessary"`` and ``"wrong"`` are both negative — Nova
either acted when it shouldn't have (wrong) or the call didn't earn its keep
(unnecessary), and either way a threshold that would have suppressed it was
the better call. Sweeping the threshold sorts each judged decision into one of
four buckets:

    acted (conf ≥ T) & good        → kept a good call        (correct)
    acted (conf ≥ T) & not good    → still made a mistake     (incorrect)
    held  (conf < T) & not good    → avoided a mistake        (correct)
    held  (conf < T) & good        → suppressed a good call   (incorrect)

Maximising (kept-right + avoided-mistake) picks the threshold that best separates
good decisions from bad ones by confidence. Everything here is pure over a list
of record dicts — nothing mutates records, calls a model, or acts on the home.

Was ``RIGHT = "right"`` / ``WRONG = "wrong"`` before this fix — a vocabulary
that no part of the integration ever actually wrote to a Decision Record (real
writers only ever use good/unnecessary/wrong), so every "good" outcome was
silently invisible to this module and only "wrong" records were ever counted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import decision_record as _dr

# The real verdict vocabulary decision_record.py actually writes (see
# decision_record.OUTCOME_GOOD / OUTCOME_UNNECESSARY / OUTCOME_WRONG) — not a
# separate vocabulary of our own that can drift out of sync with it again.
POSITIVE_OUTCOME = _dr.OUTCOME_GOOD
NEGATIVE_OUTCOMES = (_dr.OUTCOME_UNNECESSARY, _dr.OUTCOME_WRONG)

# Below this many judged samples a recommendation is withheld — a threshold tuned
# on a handful of outcomes would chase noise. Cold-start gate, same spirit as the
# pattern/routine learning that stays quiet until it has enough evidence.
DEFAULT_MIN_SAMPLES = 25


@dataclass
class ReplayResult:
    """Outcome of replaying one confidence threshold over judged records."""
    threshold: float
    kept_right: int = 0        # acted (conf ≥ T) and it was good
    kept_wrong: int = 0        # acted and it was NOT good (unnecessary or wrong)
    suppressed_right: int = 0  # held (conf < T) but it was good — a good call lost
    suppressed_wrong: int = 0  # held and it was NOT good — a mistake avoided

    @property
    def total(self) -> int:
        return self.kept_right + self.kept_wrong + self.suppressed_right + self.suppressed_wrong

    @property
    def correct(self) -> int:
        """Decisions the policy got right: kept a good call or avoided a bad one."""
        return self.kept_right + self.suppressed_wrong

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def mistakes_avoided(self) -> int:
        return self.suppressed_wrong

    @property
    def good_calls_lost(self) -> int:
        return self.suppressed_right

    @property
    def acted(self) -> int:
        return self.kept_right + self.kept_wrong

    def to_dict(self) -> dict:
        return {
            "threshold": round(self.threshold, 3),
            "samples": self.total,
            "accuracy": round(self.accuracy, 3),
            "kept_right": self.kept_right,
            "kept_wrong": self.kept_wrong,
            "suppressed_right": self.suppressed_right,
            "suppressed_wrong": self.suppressed_wrong,
            "mistakes_avoided": self.mistakes_avoided,
            "good_calls_lost": self.good_calls_lost,
        }


def _judged(records) -> list:
    """(confidence, is_good) for records with a numeric confidence AND a real
    judged outcome (good/unnecessary/wrong). Everything else is skipped —
    unjudged decisions and ones logged without a confidence carry no signal
    for threshold evaluation."""
    out = []
    for r in records or []:
        conf = r.get("confidence")
        outcome = r.get("outcome")
        if conf is None or outcome not in (POSITIVE_OUTCOME, *NEGATIVE_OUTCOMES):
            continue
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            continue
        out.append((conf, outcome == POSITIVE_OUTCOME))
    return out


def evaluate_threshold(records, threshold: float) -> ReplayResult:
    """Replay a single confidence threshold over the judged records."""
    res = ReplayResult(threshold=float(threshold))
    for conf, is_good in _judged(records):
        acted = conf >= threshold
        if acted and is_good:
            res.kept_right += 1
        elif acted and not is_good:
            res.kept_wrong += 1
        elif not acted and is_good:
            res.suppressed_right += 1
        else:
            res.suppressed_wrong += 1
    return res


def _default_grid() -> list:
    return [round(i / 20.0, 3) for i in range(21)]  # 0.00 … 1.00, step 0.05


def sweep(records, thresholds=None) -> list:
    """Replay a range of thresholds; returns one ReplayResult per threshold."""
    grid = thresholds if thresholds is not None else _default_grid()
    return [evaluate_threshold(records, t) for t in grid]


def recommend_threshold(records, min_samples: int = DEFAULT_MIN_SAMPLES,
                        thresholds=None) -> Optional[dict]:
    """The threshold with the best accuracy over history, or None if there
    aren't yet enough judged samples to trust a recommendation.

    Ties break toward the *lower* threshold (act more readily) so the
    recommendation doesn't silently make Nova more conservative than the
    evidence requires. Returns a summary dict including the current-vs-best
    comparison the caller can act on."""
    judged = _judged(records)
    if len(judged) < max(1, min_samples):
        return None
    results = sweep(records, thresholds)
    # best accuracy, lowest threshold on a tie
    best = max(results, key=lambda r: (r.accuracy, -r.threshold))
    return {
        "samples": len(judged),
        "recommended": best.to_dict(),
        "sweep": [r.to_dict() for r in results],
    }


def replay_kind(kind: str, min_samples: int = DEFAULT_MIN_SAMPLES,
                limit: int = 2000, db_path: Optional[str] = None) -> dict:
    """Pull recent records of `kind` from the Decision Record and recommend a
    threshold. DB-facing convenience over the pure functions above; safe to call
    with no data (returns a 'not enough data' summary rather than raising)."""
    try:
        from . import decision_record
        records = decision_record.recent(limit=limit, kind=kind, db_path=db_path)
    except Exception:
        records = []
    rec = recommend_threshold(records, min_samples=min_samples)
    if rec is None:
        judged = len(_judged(records))
        return {
            "kind": kind,
            "ready": False,
            "samples": judged,
            "needed": min_samples,
            "reason": f"only {judged} judged decision(s); need {min_samples} to recommend a threshold",
        }
    return {"kind": kind, "ready": True, **rec}


REPLAY_LABEL = ("Replay using current settings. This is not an exact "
                "reconstruction of the original decision.")

# How close (in confidence units) counts as "near" the current threshold —
# reuses _default_grid()'s own step size rather than inventing a separate one.
_NEAR_THRESHOLD_MARGIN = 0.05


def replay_one(record: dict) -> dict:
    """Replay one stored Decision Record against Nova's CURRENT policy —
    never a reconstruction of what actually happened at the time, since no
    historical threshold was ever stored alongside the decision. Supported
    today only for ``suggestion`` decisions, the one kind with a real,
    currently-effective, adjustable confidence threshold
    (pattern_analyzer._effective_threshold()); every other kind returns
    ``supported: False`` rather than inventing a threshold that doesn't exist
    for it.

    Structurally read-only: takes a plain record dict, not `hass` — it has no
    way to write to a database, call a Home Assistant service, send a
    notification, speak, call an LLM/cloud service, or change a device or
    configuration, because nothing here is ever handed the means to.
    """
    kind = record.get("kind")
    confidence = record.get("confidence")
    out = {
        "id": record.get("id"),
        "kind": kind,
        "recorded_decision": record.get("decision"),
        "recorded_outcome": record.get("outcome"),
        "recorded_confidence": confidence,
        "label": REPLAY_LABEL,
    }
    if kind != "suggestion":
        out["supported"] = False
        out["reason"] = f"no adjustable policy threshold exists for kind '{kind}'"
        return out
    if confidence is None:
        out["supported"] = False
        out["reason"] = "no confidence was recorded for this decision"
        return out
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        out["supported"] = False
        out["reason"] = "recorded confidence is not numeric"
        return out

    from . import pattern_analyzer
    threshold = float(pattern_analyzer._effective_threshold())
    out.update({
        "supported": True,
        "current_threshold": round(threshold, 3),
        "would_pass_current_threshold": confidence >= threshold,
        "within_0_05_of_threshold": abs(confidence - threshold) <= _NEAR_THRESHOLD_MARGIN,
    })
    return out
