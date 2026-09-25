"""Compatibility module: pattern learning now lives in
``automation/patterns.py``, suggestion generation and storage in
``automation/suggestions.py`` and suggestion installation in
``automation/installation.py``. Every name here is the package object itself, and assigning one here
assigns it in the package (see ``automation/_compat.py``).

The thresholds are process configuration that ``set_thresholds`` rebinds, so
they (and the analyzer singleton) are read through to the package module on
every access instead of being copied here.
"""
from __future__ import annotations

from .automation import (
    _compat,
    patterns as _patterns,
    suggestions as _suggestions,
    installation as _installation,
    models as _models,
)
from .automation.installation import install_approved_suggestion
from .automation.models import DetectedPattern
from .automation.patterns import (
    _ADAPT_MIN_JUDGED,
    _ADAPT_WINDOW_S,
    _AUTOMATED_SOURCE_SQL,
    ANALYSIS_INTERVAL,
    KNOWLEDGE_FACT_CONFIDENCE,
    MIN_DAYS,
    PERSON_DOMINANCE_RATIO,
    PatternAnalyzer,
    _condition_phrase,
    _effective_threshold,
    _is_dark_at,
    _learned_threshold_delta,
    _nice_threshold,
    _numeric_condition,
    _numeric_trigger_from,
    _numeric_value_at,
    _source_filter,
    _sun_condition,
    _time_window_condition,
    get_analyzer,
    set_thresholds,
)
from .automation.suggestions import (
    _trigger_extra_conditions,
    _trigger_for,
    _trigger_phrase,
    explain_suggestion,
    normalize_suggestion_automation,
    service_for,
)

__all__ = [
    "ANALYSIS_INTERVAL",
    "DetectedPattern",
    "KNOWLEDGE_FACT_CONFIDENCE",
    "MIN_DAYS",
    "PERSON_DOMINANCE_RATIO",
    "PatternAnalyzer",
    "_ADAPT_MIN_JUDGED",
    "_ADAPT_WINDOW_S",
    "_AUTOMATED_SOURCE_SQL",
    "_condition_phrase",
    "_effective_threshold",
    "_is_dark_at",
    "_learned_threshold_delta",
    "_nice_threshold",
    "_numeric_condition",
    "_numeric_trigger_from",
    "_numeric_value_at",
    "_source_filter",
    "_sun_condition",
    "_time_window_condition",
    "_trigger_extra_conditions",
    "_trigger_for",
    "_trigger_phrase",
    "explain_suggestion",
    "get_analyzer",
    "install_approved_suggestion",
    "normalize_suggestion_automation",
    "service_for",
    "set_thresholds",
]

_compat.install(
    __name__, (_patterns, _suggestions, _installation, _models,),
    live={
        "MIN_OCCURRENCES": _patterns,
        "CONFIDENCE_THRESHOLD": _patterns,
        "DB_PATH": _patterns,
        "_ADAPT_CACHE": _patterns,
        "_ANALYZER": _patterns,
    },
)
