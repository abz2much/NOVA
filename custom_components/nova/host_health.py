"""Nova — Home Assistant host-health awareness (Phase 10).

Gives Nova structured visibility into the health of the machine Home
Assistant itself runs on (a mini PC running HAOS), reusing readings the
built-in **System Monitor** integration (domain ``systemmonitor``) already
publishes as ordinary sensor entities. Nova never reads ``/proc``, ``/sys``,
or any other filesystem path, never shells out, never calls an undocumented
Supervisor API, and has no host-control, restart, shutdown, or remediation
action anywhere in this module — it only observes System Monitor's own
entities through Home Assistant's state machine and reports what it finds.

Architecture, deliberately reusing existing Nova systems rather than adding
new ones:
  - Discovery: entity/device registry lookups (integration ownership via
    ``RegistryEntry.platform == "systemmonitor"``), the same registry-lookup
    style ``camera_semantic.resolve_location`` already uses.
  - Evaluation: a small deterministic state machine (no LLM). Persistence is
    tracked in an in-process dict — never written to disk, so a restart or
    reload starts with a clean slate rather than reconstructing false
    persistence from a partial history.
  - Diagnostics: folded into the existing ``system_diagnostics`` result
    (``diagnostics/service_health.py``) — HOMER already has that tool; this
    adds no new one.
  - Alerts: routed through the same pieces every other Nova proactive
    monitor already uses — ``output_gate`` (rate limit / dedup / mute),
    ``audio_routing.observer_speak_target`` (presence-aware, occupied-room
    routing), ``tts_helper.async_announce``, and
    ``notify_targets.async_send_configured_notifications`` — the exact
    pattern ``appliance_monitor.py`` uses for its own non-critical,
    proactive announcements. No standalone alert dispatcher.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

SYSMON_DOMAIN = "systemmonitor"
SENSOR_DOMAIN = "sensor"

# Accumulated PSI "total" entities (state_class total_increasing, unit
# microseconds) must never feed an alert decision — excluded structurally
# below by requiring state_class == "measurement", not by name matching.
_PSI_TOTAL_STATE_CLASS = "total_increasing"
_MEASUREMENT_STATE_CLASS = "measurement"

_UNAVAILABLE_STATES = {"unknown", "unavailable", "none", ""}
_PERCENT_UNITS = {"%"}
_TEMP_UNITS = {"°C", "°F"}

_MAX_LABEL_LEN = 80


@dataclass(frozen=True)
class MetricSpec:
    key: str                    # stable internal name (never localized)
    label: str                  # short label for panel/prompt/diagnostics
    sysmon_key: str             # System Monitor's own key / translation_key
    device_class: Optional[str] # expected device_class, when meaningful
    unit_kind: str              # "percent" | "temperature" | "none"
    recommended: bool           # recommended vs optional (README/panel guidance)
    per_instance: bool          # True only for disk_use_percent (multi-mount)
    alertable: bool             # False for purely informational metrics


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("cpu_percent", "Processor use", "processor_use", None, "percent", True, False, True),
    MetricSpec("memory_percent", "Memory usage", "memory_use_percent", None, "percent", True, False, True),
    MetricSpec("memory_pressure_some", "Memory Pressure Some 60s Average", "memory_pressure_some_avg60", None, "percent", True, False, True),
    MetricSpec("memory_pressure_full", "Memory Pressure Full 60s Average", "memory_pressure_full_avg60", None, "percent", True, False, True),
    MetricSpec("io_pressure_some", "IO Pressure Some 60s Average", "io_pressure_some_avg60", None, "percent", True, False, True),
    MetricSpec("io_pressure_full", "IO Pressure Full 60s Average", "io_pressure_full_avg60", None, "percent", True, False, True),
    MetricSpec("disk_percent", "Disk usage", "disk_use_percent", None, "percent", True, True, True),
    MetricSpec("cpu_temperature", "Processor temperature", "processor_temperature", "temperature", "temperature", False, False, True),
    MetricSpec("swap_percent", "Swap usage", "swap_use_percent", None, "percent", False, False, True),
    MetricSpec("cpu_pressure_some", "CPU Pressure Some 60s Average", "cpu_pressure_some_avg60", None, "percent", False, False, True),
    MetricSpec("load_5m", "Load 5 min", "load_5m", None, "none", False, False, False),
    MetricSpec("uptime", "Uptime", "last_boot", "uptime", "none", False, False, False),
)
_METRICS_BY_KEY: dict[str, MetricSpec] = {m.key: m for m in METRICS}
ALERTABLE_METRIC_KEYS = tuple(m.key for m in METRICS if m.alertable)

# Conservative defaults. Chosen to avoid nagging on a normal, briefly-busy
# mini PC while still catching a genuine, sustained problem. None of these
# claim to be validated against any specific hardware — see the temperature
# note especially (README/CHANGELOG document this explicitly).
DEFAULT_THRESHOLDS: dict[str, float] = {
    "cpu_percent": 90.0,
    "memory_percent": 90.0,
    "memory_pressure_some": 10.0,
    "memory_pressure_full": 5.0,
    "io_pressure_some": 10.0,
    "io_pressure_full": 5.0,
    "disk_percent": 90.0,
    "cpu_temperature": 80.0,   # Celsius. Conservative, NOT a hardware-specific safe limit.
    "swap_percent": 50.0,
    "cpu_pressure_some": 10.0,
}
_THRESHOLD_RANGE: dict[str, tuple[float, float]] = {
    "cpu_percent": (1.0, 100.0),
    "memory_percent": (1.0, 100.0),
    "memory_pressure_some": (0.0, 100.0),
    "memory_pressure_full": (0.0, 100.0),
    "io_pressure_some": (0.0, 100.0),
    "io_pressure_full": (0.0, 100.0),
    "disk_percent": (1.0, 100.0),
    "cpu_temperature": (30.0, 110.0),
    "swap_percent": (0.0, 100.0),
    "cpu_pressure_some": (0.0, 100.0),
}

DEFAULT_PERSISTENCE_MINUTES = 10.0   # sustained breach/recovery window
_PERSISTENCE_RANGE_MINUTES = (2.0, 120.0)
DEFAULT_COOLDOWN_MINUTES = 60.0      # minimum gap between repeat alerts
_COOLDOWN_RANGE_MINUTES = (5.0, 720.0)
DEFAULT_MAX_SAMPLE_AGE_SECONDS = 600.0  # a reading older than this is "stale"

TICK_INTERVAL_SECONDS = 120.0  # sampling cadence for the periodic sweep

_CONFIG_ENABLED_KEY = "host_health_enabled"
_CONFIG_ALERTS_KEY = "host_health_alerts_enabled"
_CONFIG_MAPPINGS_KEY = "host_health_mappings"           # JSON dict: metric_key -> entity_id
_CONFIG_THRESHOLDS_KEY = "host_health_thresholds"        # JSON dict: metric_key -> float
_CONFIG_PERSISTENCE_KEY = "host_health_persistence_minutes"
_CONFIG_COOLDOWN_KEY = "host_health_cooldown_minutes"
_CONFIG_RECOVERY_KEY = "host_health_recovery_announce"   # bool, default True


# ── small pure helpers ──────────────────────────────────────────────────────

def _now() -> float:
    return time.monotonic()


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _sanitize_label(value: Any, max_len: int = _MAX_LABEL_LEN) -> str:
    """Bound and stringify anything that might end up in a panel response,
    log line, or (never directly, but defensively) a prompt. Entity/friendly
    names are untrusted text as far as this module is concerned."""
    try:
        s = str(value)
    except Exception:
        return ""
    return s[:max_len]


def fahrenheit_to_celsius(value: float) -> float:
    return (value - 32.0) * 5.0 / 9.0


# ── discovery ────────────────────────────────────────────────────────────────

@dataclass
class EntityCandidate:
    entity_id: str
    friendly_name: str
    disabled: bool
    unit: Optional[str]


def _classify_entry(entry) -> Optional[str]:
    """RegistryEntry -> the metric sysmon_key it represents, or None. Uses
    translation_key first (Home Assistant's own stable, non-localized entity
    classifier), falling back to a unique_id PREFIX match against the
    System Monitor integration's own internal sensor key — never the
    friendly name or entity_id, both of which a user can freely rename."""
    tkey = getattr(entry, "translation_key", None)
    if tkey:
        return str(tkey)
    uid = getattr(entry, "unique_id", "") or ""
    for m in METRICS:
        if uid.startswith(m.sysmon_key):
            return m.sysmon_key
    return None


def _entry_unit(hass, entry) -> Optional[str]:
    state = hass.states.get(entry.entity_id)
    if state is not None:
        unit = state.attributes.get("unit_of_measurement")
        if unit is not None:
            return str(unit)
    # Disabled entities have no live state — fall back to whatever the
    # registry itself recorded, when present (varies by HA version).
    for attr in ("unit_of_measurement", "original_unit_of_measurement"):
        unit = getattr(entry, attr, None)
        if unit:
            return str(unit)
    return None


def _entry_state_class(hass, entry) -> Optional[str]:
    state = hass.states.get(entry.entity_id)
    if state is not None:
        sc = state.attributes.get("state_class")
        if sc is not None:
            return str(sc)
    for attr in ("state_class", "original_state_class"):
        sc = getattr(entry, attr, None)
        if sc:
            return str(sc)
    return None


def discover_candidates(hass) -> dict[str, list[EntityCandidate]]:
    """metric_key -> every plausible System Monitor entity for it, newest-
    unique_id-first is NOT guaranteed — order is registry iteration order,
    callers needing determinism sort themselves. Never raises; a registry
    access failure yields an empty result for every metric (fail closed,
    not fail with a stack trace)."""
    result: dict[str, list[EntityCandidate]] = {m.key: [] for m in METRICS}
    try:
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(hass)
        entries = list(registry.entities.values())
    except Exception as exc:
        _LOGGER.debug("host_health: entity registry unavailable: %s", exc)
        return result

    by_sysmon_key: dict[str, list] = {}
    for entry in entries:
        try:
            if getattr(entry, "platform", None) != SYSMON_DOMAIN:
                continue
            if not str(getattr(entry, "entity_id", "")).startswith(f"{SENSOR_DOMAIN}."):
                continue
            sysmon_key = _classify_entry(entry)
            if not sysmon_key:
                continue
            # Defense in depth against the accumulated PSI "_total" entities:
            # excluded by state_class (they're total_increasing, everything
            # we want is measurement), never by string-matching the name.
            state_class = _entry_state_class(hass, entry)
            if state_class == _PSI_TOTAL_STATE_CLASS:
                continue
            by_sysmon_key.setdefault(sysmon_key, []).append(entry)
        except Exception:
            continue

    for metric in METRICS:
        for entry in by_sysmon_key.get(metric.sysmon_key, []):
            try:
                unit = _entry_unit(hass, entry)
                if metric.unit_kind == "percent" and unit is not None and unit not in _PERCENT_UNITS:
                    continue
                if metric.unit_kind == "temperature" and unit is not None and unit not in _TEMP_UNITS:
                    continue
                state = hass.states.get(entry.entity_id)
                friendly = (
                    state.attributes.get("friendly_name")
                    if state is not None else None
                ) or entry.entity_id
                result[metric.key].append(EntityCandidate(
                    entity_id=entry.entity_id,
                    friendly_name=_sanitize_label(friendly),
                    disabled=getattr(entry, "disabled_by", None) is not None,
                    unit=unit,
                ))
            except Exception:
                continue
    return result


# ── mapping resolution ──────────────────────────────────────────────────────

@dataclass
class MappingResult:
    metric: MetricSpec
    entity_id: Optional[str]
    source: Optional[str]       # "manual" | "auto" | None
    status: str                 # "mapped" | "ambiguous" | "missing" | "disabled"
    candidates: list[EntityCandidate] = field(default_factory=list)


def _manual_entity_valid(hass, entity_id: str, metric: MetricSpec) -> bool:
    """A manually-mapped entity must be a real, numeric, measurement-class
    sensor with a plausible unit for this metric — Nova doesn't require it
    to specifically belong to the System Monitor integration (an admin may
    reasonably point Disk usage at their own storage sensor), but it must
    still be a genuinely appropriate reading, never an arbitrary entity."""
    if not entity_id.startswith(f"{SENSOR_DOMAIN}."):
        return False
    state = hass.states.get(entity_id)
    if state is None:
        return False  # can't validate a mapping to an entity with no state
    unit = state.attributes.get("unit_of_measurement")
    if metric.unit_kind == "percent" and unit not in _PERCENT_UNITS:
        return False
    if metric.unit_kind == "temperature" and unit not in _TEMP_UNITS:
        return False
    return True


def resolve_mappings(hass, config: dict, candidates: Optional[dict] = None) -> dict[str, MappingResult]:
    """Resolve every metric to (at most) one entity_id, honoring a manual
    override first, then auto-mapping only when exactly one candidate
    exists. Never enables a disabled entity — a disabled sole candidate is
    reported as status='disabled' with setup guidance, not silently
    force-enabled."""
    candidates = candidates if candidates is not None else discover_candidates(hass)
    manual_map = _decode_dict_config(config.get(_CONFIG_MAPPINGS_KEY))

    out: dict[str, MappingResult] = {}
    for metric in METRICS:
        cands = candidates.get(metric.key, [])
        manual_entity = manual_map.get(metric.key)
        if manual_entity and _manual_entity_valid(hass, str(manual_entity), metric):
            out[metric.key] = MappingResult(metric, str(manual_entity), "manual", "mapped", cands)
            continue

        available = [c for c in cands if not c.disabled]
        if len(available) == 1:
            out[metric.key] = MappingResult(metric, available[0].entity_id, "auto", "mapped", cands)
        elif len(available) > 1:
            out[metric.key] = MappingResult(metric, None, None, "ambiguous", cands)
        elif cands:  # only disabled candidates exist
            out[metric.key] = MappingResult(metric, None, None, "disabled", cands)
        else:
            out[metric.key] = MappingResult(metric, None, None, "missing", cands)
    return out


# ── reading + normalization ─────────────────────────────────────────────────

@dataclass
class MetricSample:
    key: str
    available: bool
    value: Optional[float]
    unit: str
    source_entity: Optional[str]
    sample_age_seconds: Optional[float]
    reason: str
    last_updated_iso: Optional[str] = None


def read_metric(hass, mapping: MappingResult, max_age_seconds: float) -> MetricSample:
    metric = mapping.metric
    if mapping.entity_id is None:
        return MetricSample(metric.key, False, None, "", None, None, mapping.status)

    state = hass.states.get(mapping.entity_id)
    if state is None:
        return MetricSample(metric.key, False, None, "", mapping.entity_id, None, "unavailable")
    if str(state.state).strip().lower() in _UNAVAILABLE_STATES:
        return MetricSample(metric.key, False, None, "", mapping.entity_id, None, "unavailable")

    value = _as_float(state.state)
    if value is None:
        return MetricSample(metric.key, False, None, "", mapping.entity_id, None, "non_numeric")

    age = None
    last_iso = None
    try:
        from homeassistant.util import dt as dt_util
        last = getattr(state, "last_updated", None)
        if last is not None:
            age = max(0.0, (dt_util.utcnow() - last).total_seconds())
            last_iso = last.isoformat()
    except Exception:
        age = None
    if age is not None and age > max_age_seconds:
        return MetricSample(metric.key, False, None, "", mapping.entity_id, age, "stale", last_iso)

    unit = state.attributes.get("unit_of_measurement") or ""
    if metric.unit_kind == "temperature":
        if unit == "°F":
            value = fahrenheit_to_celsius(value)
            unit = "°C"
        elif unit not in ("", "°C"):
            return MetricSample(metric.key, False, None, unit, mapping.entity_id, age, "unsupported_unit", last_iso)

    return MetricSample(metric.key, True, value, unit, mapping.entity_id, age, "ok", last_iso)


# ── configuration helpers ───────────────────────────────────────────────────

def _decode_dict_config(value: Any) -> dict:
    """A dict-shaped config value (host_health_mappings / _thresholds) may
    arrive as a native dict (a caller building config in Python directly —
    tests, the scheduler tick) or as a JSON-encoded string: the websocket
    nova/update_config schema only accepts bool/str/int/float/None for
    `value`, so the panel always JSON.stringify()s a dict before sending —
    the exact same convention room_speakers/satellite_pairings/
    person_honorifics already use (see websocket.py's _get_runtime_json,
    which decodes the same way on the read side). Never raises."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            import json
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def clamp_threshold(metric_key: str, raw: Any) -> float:
    lo, hi = _THRESHOLD_RANGE.get(metric_key, (0.0, 100.0))
    v = _as_float(raw)
    default = DEFAULT_THRESHOLDS.get(metric_key, lo)
    if v is None:
        return default
    return max(lo, min(hi, v))


def clamp_persistence_minutes(raw: Any) -> float:
    lo, hi = _PERSISTENCE_RANGE_MINUTES
    v = _as_float(raw)
    if v is None:
        return DEFAULT_PERSISTENCE_MINUTES
    return max(lo, min(hi, v))


def clamp_cooldown_minutes(raw: Any) -> float:
    lo, hi = _COOLDOWN_RANGE_MINUTES
    v = _as_float(raw)
    if v is None:
        return DEFAULT_COOLDOWN_MINUTES
    return max(lo, min(hi, v))


def _enabled(config: dict) -> bool:
    return bool(config.get(_CONFIG_ENABLED_KEY, False))


def _alerts_enabled(config: dict) -> bool:
    return _enabled(config) and bool(config.get(_CONFIG_ALERTS_KEY, False))


def _threshold_for(config: dict, metric_key: str) -> float:
    raw = _decode_dict_config(config.get(_CONFIG_THRESHOLDS_KEY)).get(metric_key)
    return clamp_threshold(metric_key, raw) if raw is not None else DEFAULT_THRESHOLDS.get(metric_key, 100.0)


def _persistence_seconds(config: dict) -> float:
    return clamp_persistence_minutes(config.get(_CONFIG_PERSISTENCE_KEY)) * 60.0


def _cooldown_seconds(config: dict) -> float:
    return clamp_cooldown_minutes(config.get(_CONFIG_COOLDOWN_KEY)) * 60.0


def _recovery_announce_enabled(config: dict) -> bool:
    return bool(config.get(_CONFIG_RECOVERY_KEY, True))


# ── persistence state machine (in-process only — never written to disk) ────

_STATE: dict[str, dict] = {}


def reset_state() -> None:
    """Test/reload hook: clears all persistence tracking. Never called on a
    normal tick — only a fresh process start naturally clears _STATE, which
    is exactly the point (a restart must never inherit false persistence)."""
    _STATE.clear()


def _metric_state(metric_key: str) -> dict:
    return _STATE.setdefault(metric_key, {
        "breach_since": None, "ok_since": None, "alerted": False,
        "last_alert_ts": None, "last_sample": None, "mapping_status": None,
        "last_entity_id": None,
    })


@dataclass
class MetricResult:
    key: str
    label: str
    available: bool
    value: Optional[float]
    unit: str
    source_entity: Optional[str]
    sample_age_seconds: Optional[float]
    threshold: Optional[float]
    over_threshold: bool
    persistent_problem: bool
    reason: str


def _advance_one(config: dict, mapping: MappingResult, sample: MetricSample, now: float) -> MetricResult:
    metric = mapping.metric
    st = _metric_state(metric.key)
    if mapping.entity_id != st["last_entity_id"]:
        # The mapped entity changed (manual remap, or a previously-missing
        # sensor just appeared) — any accumulated breach/recovery streak
        # belonged to a DIFFERENT source and must not carry over. Alert
        # state (whether an alert was already sent) is preserved rather
        # than reset, so a remap can't itself trigger a duplicate alert.
        st["breach_since"] = None
        st["ok_since"] = None
        st["last_entity_id"] = mapping.entity_id
    st["last_sample"] = sample
    st["mapping_status"] = mapping.status

    if not metric.alertable:
        return MetricResult(metric.key, metric.label, sample.available, sample.value,
                            sample.unit, sample.source_entity, sample.sample_age_seconds,
                            None, False, False, sample.reason)

    threshold = _threshold_for(config, metric.key)
    if not sample.available:
        # A gap must never count toward persistence in either direction.
        st["breach_since"] = None
        st["ok_since"] = None
        return MetricResult(metric.key, metric.label, False, None, sample.unit,
                            sample.source_entity, sample.sample_age_seconds,
                            threshold, False, st["alerted"], sample.reason)

    over = sample.value >= threshold
    persistence_secs = _persistence_seconds(config)
    if over:
        if st["breach_since"] is None:
            st["breach_since"] = now
        st["ok_since"] = None
        persistent = (now - st["breach_since"]) >= persistence_secs
    else:
        st["breach_since"] = None
        persistent = False
        if st["alerted"]:
            if st["ok_since"] is None:
                st["ok_since"] = now
            if (now - st["ok_since"]) >= persistence_secs:
                st["alerted"] = False
                st["ok_since"] = None

    return MetricResult(metric.key, metric.label, True, sample.value, sample.unit,
                        sample.source_entity, sample.sample_age_seconds,
                        threshold, over, persistent, "ok")


def _compose_problem_message(results: list[MetricResult]) -> str:
    clauses = []
    for r in results:
        if r.unit == "°C":
            clauses.append(f"{r.label.lower()} has stayed at {r.value:.0f}°C")
        elif r.unit == "%":
            clauses.append(f"{r.label.lower()} has stayed at {r.value:.0f} percent")
        else:
            clauses.append(f"{r.label.lower()} has stayed elevated at {r.value:.2f}")
    if len(clauses) == 1:
        body = clauses[0]
    elif len(clauses) == 2:
        body = f"{clauses[0]}, and {clauses[1]}"
    else:
        body = ", ".join(clauses[:-1]) + f", and {clauses[-1]}"
    return f"The host has a persistent resource problem: {body}."


def _compose_recovery_message(results: list[MetricResult]) -> str:
    names = ", ".join(r.label.lower() for r in results)
    return f"The host's {names} {'has' if len(results) == 1 else 'have'} recovered and stayed stable."


async def _dispatch_alert(hass, config: dict, message: str, category: str) -> None:
    """Route through the exact pieces every other Nova proactive monitor
    uses (appliance_monitor.py's own pattern) — no standalone dispatcher."""
    try:
        from . import output_gate
        allowed, reason = output_gate.can_announce(
            entity_id="host_health", category=category, urgency="medium", message=message,
        )
        if not allowed:
            _LOGGER.debug("host_health: announcement suppressed: %s", reason)
            output_gate.record_announcement(
                entity_id="host_health", category=category, urgency="medium",
                message=message, was_spoken=False,
            )
            return

        from .audio_routing import observer_speak_target
        from . import sleep_detection
        sleeping = False
        try:
            sleeping, _ = sleep_detection.is_sleeping(
                hass,
                bedroom_area_ids=config.get("bedroom_areas", []) or [],
                quiet_start=config.get("observer_quiet_start", "22:00"),
                quiet_end=config.get("observer_quiet_end", "07:00"),
            )
        except Exception:
            pass

        targets, mode = observer_speak_target(
            hass, urgency="medium",
            broadcast_group=config.get("broadcast_group") or None,
            announcement_speakers=config.get("announcement_speakers") or None,
            is_sleeping=sleeping,
        )

        spoken = False
        if mode not in ("suppressed",) and targets:
            from .tts_helper import resolve_tts_for_context, async_announce
            tts_entity = resolve_tts_for_context(
                hass, "host_health",
                config.get("tts_engine", "auto"),
                config.get("tts_premium_engine") or None,
                config.get("tts_premium_contexts") or [],
            )
            if tts_entity:
                spoken = await async_announce(hass, message, tts_entity, targets, context="host_health")

        if mode == "notify_only" or not spoken:
            try:
                from .notify_targets import async_send_configured_notifications
                await async_send_configured_notifications(
                    hass, config, {"message": message, "title": "Nova"},
                    action="notify", source="proactive", entity_id="host_health",
                )
            except Exception as exc:
                _LOGGER.debug("host_health: notification failed: %s", exc)

        output_gate.record_announcement(
            entity_id="host_health", category=category, urgency="medium",
            message=message, was_spoken=bool(spoken or mode == "notify_only"),
        )
    except Exception as exc:
        # A delivery failure must never propagate into a retry loop — the
        # caller (tick) has already stamped last_alert_ts before this runs.
        _LOGGER.debug("host_health: alert dispatch failed: %s", exc)


async def tick(hass, config: dict) -> dict:
    """The one authoritative sampling pass: reads every mapped metric,
    advances persistence, and — only here, never from a diagnostics read —
    may dispatch an alert. Returns a small summary dict, never raises."""
    if not _enabled(config):
        return {"skipped": "disabled"}

    now = _now()
    candidates = discover_candidates(hass)
    mappings = resolve_mappings(hass, config, candidates)
    max_age = float(config.get("host_health_max_sample_age", DEFAULT_MAX_SAMPLE_AGE_SECONDS) or DEFAULT_MAX_SAMPLE_AGE_SECONDS)

    newly_persistent: list[MetricResult] = []
    recovered: list[MetricResult] = []
    for metric in METRICS:
        mapping = mappings[metric.key]
        sample = read_metric(hass, mapping, max_age)
        st_before_alerted = _metric_state(metric.key)["alerted"]
        result = _advance_one(config, mapping, sample, now)
        st = _metric_state(metric.key)
        if metric.alertable and result.persistent_problem and not st["alerted"]:
            st["alerted"] = True
            cooldown_ok = st["last_alert_ts"] is None or (now - st["last_alert_ts"]) >= _cooldown_seconds(config)
            if cooldown_ok:
                newly_persistent.append(result)
        elif metric.alertable and result.persistent_problem and st["alerted"]:
            cooldown_ok = st["last_alert_ts"] is not None and (now - st["last_alert_ts"]) >= _cooldown_seconds(config)
            if cooldown_ok:
                newly_persistent.append(result)
        elif metric.alertable and st_before_alerted and not st["alerted"]:
            recovered.append(result)

    if newly_persistent and _alerts_enabled(config):
        # Stamp last_alert_ts BEFORE attempting delivery, and wrap the
        # dispatch itself defensively — a delivery failure (or a bug in the
        # dispatch chain) must never turn into an every-tick retry loop.
        for r in newly_persistent:
            _metric_state(r.key)["last_alert_ts"] = now
        message = _compose_problem_message(newly_persistent)
        try:
            await _dispatch_alert(hass, config, message, "host_health_problem")
        except Exception as exc:
            _LOGGER.debug("host_health: problem alert dispatch raised: %s", exc)
    if recovered and _alerts_enabled(config) and _recovery_announce_enabled(config):
        message = _compose_recovery_message(recovered)
        try:
            await _dispatch_alert(hass, config, message, "host_health_recovery")
        except Exception as exc:
            _LOGGER.debug("host_health: recovery alert dispatch raised: %s", exc)

    return {
        "checked": True,
        "problems_alerted": [r.key for r in newly_persistent],
        "recoveries_alerted": [r.key for r in recovered],
    }


# ── read-only snapshot for system_diagnostics / HOMER / the panel ──────────

def snapshot(hass, config: dict) -> dict:
    """Pure read: formats whatever the last tick() observed, plus a fresh
    mapping-status pass (mapping/candidate discovery is cheap and has no
    persistence side effects, so it's safe to compute fresh on every read).
    Never mutates the persistence state machine and never runs I/O beyond
    ordinary state-machine registry/state reads."""
    if not _enabled(config):
        return {
            "enabled": False, "alerts_enabled": False, "overall": "off",
            "available": [], "problems": [], "persistent_problems": [],
            "missing_or_stale": [], "newest_sample_ts": None,
        }

    mappings = resolve_mappings(hass, config)
    available: list[dict] = []
    problems: list[dict] = []
    persistent_problems: list[dict] = []
    missing_or_stale: list[dict] = []
    newest_updated: Optional[str] = None

    for metric in METRICS:
        mapping = mappings[metric.key]
        st = _STATE.get(metric.key)
        sample: Optional[MetricSample] = st["last_sample"] if st else None
        entry = {
            "key": metric.key,
            "label": metric.label,
            "mapping_status": mapping.status,
            "source_entity": mapping.entity_id,
        }
        if sample is None or not sample.available:
            entry["reason"] = sample.reason if sample else mapping.status
            missing_or_stale.append(entry)
            continue
        entry["value"] = sample.value
        entry["unit"] = sample.unit
        entry["sample_age_seconds"] = sample.sample_age_seconds
        available.append(entry)
        if sample.last_updated_iso and (newest_updated is None or sample.last_updated_iso > newest_updated):
            newest_updated = sample.last_updated_iso
        if metric.alertable:
            threshold = _threshold_for(config, metric.key)
            entry["threshold"] = threshold
            over = sample.value >= threshold
            entry["over_threshold"] = over
            if over:
                problems.append(dict(entry))
            if st and st.get("alerted"):
                persistent_problems.append(dict(entry))

    if persistent_problems:
        overall = "problem"
    elif problems:
        overall = "watch"
    elif missing_or_stale:
        overall = "partial"
    else:
        overall = "ok" if available else "no_data"

    return {
        "enabled": True,
        "alerts_enabled": _alerts_enabled(config),
        "overall": overall,
        "available": available,
        "problems": problems,
        "persistent_problems": persistent_problems,
        "missing_or_stale": missing_or_stale,
        "newest_sample_ts": newest_updated,
    }


def prompt_fields(hass, config: dict) -> list[dict]:
    """Fixed-shape, LLM-facing view of the current snapshot — only stable
    internal labels and numbers, never an entity_id, friendly name, or raw
    attribute. Used when host-health context is folded into a prompt (e.g.
    HOMER investigating slowness); nothing here is untrusted free text."""
    snap = snapshot(hass, config)
    if not snap["enabled"]:
        return []
    fields = []
    for entry in snap["available"]:
        fields.append({
            "metric": entry["key"],
            "value": entry.get("value"),
            "unit": entry.get("unit"),
            "over_threshold": entry.get("over_threshold", False),
            "persistent": entry["key"] in {p["key"] for p in snap["persistent_problems"]},
        })
    return fields
