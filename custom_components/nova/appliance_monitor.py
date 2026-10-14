"""Appliance cycle completion for Nova.

Notices when a washer, dryer, dishwasher, oven, microwave or a generic load
has finished, and hands one "cycle complete" message to the single
proactive delivery path (delivery.async_deliver). Three sources feed it:

* power sensors, each running an idle/running state machine on its watt
  readings, with a periodic settle re-check (SETTLE_CHECK_SECONDS) that ends
  cycles whose sensor has stopped reporting;
* native completion entities (an appliance's own "run completed" or "job
  state" entity);
* a whole-home meter, whose sudden rises and drops are matched against the
  user's declared appliance wattages.

Only a trusted provenance is ever announced: native, user-declared or
explicitly configured, or a whole-home load matched to a declared
appliance. Everything else is tracked and logged but stays silent.

Nothing is persisted here: all state lives in _MON and is lost on restart.
Nova modules are imported lazily, inside the functions that need them.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from homeassistant.core import Event, HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)

SETTLE_CHECK_SECONDS = 30


# ── appliance types ─────────────────────────────────────────────────────────

class ApplianceType(Enum):
    """Each kind of appliance and the thresholds its power cycle uses."""

    #            label         run W  idle W  sustain s  settle s
    WASHER = ("washer", 50, 10, 120, 60)
    DRYER = ("dryer", 100, 15, 120, 90)
    DISHWASHER = ("dishwasher", 30, 5, 120, 60)
    OVEN = ("oven", 500, 20, 180, 120)
    MICROWAVE = ("microwave", 200, 10, 30, 15)
    GENERIC = ("appliance", 50, 10, 120, 60)

    @property
    def label(self) -> str:
        return self.value[0]

    @property
    def run_threshold(self) -> float:
        return self.value[1]

    @property
    def idle_threshold(self) -> float:
        return self.value[2]

    @property
    def sustained_seconds(self) -> float:
        return self.value[3]

    @property
    def settle_seconds(self) -> float:
        return self.value[4]


# Name keywords, in tie-break order (the longest match wins; equal lengths
# keep this order).
_KEYWORDS: tuple[tuple[str, ApplianceType], ...] = (
    ("washing_machine", ApplianceType.WASHER),
    ("clothes_dryer", ApplianceType.DRYER),
    ("wash_machine", ApplianceType.WASHER),
    ("tumble_dryer", ApplianceType.DRYER),
    ("dish_washer", ApplianceType.DISHWASHER),
    ("dishwasher", ApplianceType.DISHWASHER),
    ("microwave", ApplianceType.MICROWAVE),
    ("laundry", ApplianceType.WASHER),
    ("washer", ApplianceType.WASHER),
    ("dryer", ApplianceType.DRYER),
    ("stove", ApplianceType.OVEN),
    ("range", ApplianceType.OVEN),
    ("oven", ApplianceType.OVEN),
)

# Peak-power fingerprints, inclusive, checked in this order.
_FINGERPRINTS: tuple[tuple[float, float, ApplianceType, str], ...] = (
    (1800, 6000, ApplianceType.DRYER, "high sustained draw, typical of a dryer"),
    (1200, 2000, ApplianceType.DISHWASHER, "heating draw, typical of a dishwasher"),
    (600, 1500, ApplianceType.MICROWAVE, "short high draw, typical of a microwave"),
    (200, 800, ApplianceType.WASHER, "motor draw, typical of a washer"),
    (2000, 5500, ApplianceType.OVEN, "element draw, typical of an oven"),
)

_POWER_UNITS = frozenset({"w", "kw", "watt", "watts"})
_METER_UNITS = frozenset({"w", "kw"})
_SKIP_STATES = ("unavailable", "unknown", "")

# Unidentified power sensors whose name says they are not one appliance.
_NOT_AN_APPLIANCE = (
    "solar", "grid", "battery", "total", "sum", "net", "generation",
    "consumption", "export", "import", "hvac", "furnace", "heat_pump",
    "air_handler",
)
_UNIDENTIFIED_MAX_W = 5000.0
_APPLIANCE_AREAS = ("laundry", "utility", "mud_room", "mudroom")

# Native completion entities: (pattern, done states, forced type).
_DONE_ON = frozenset({"on"})
_NATIVE_PATTERNS: tuple[tuple[str, frozenset, Optional[ApplianceType]], ...] = (
    ("run_completed", _DONE_ON, None),
    ("wash_completed", _DONE_ON, None),
    ("dry_completed", _DONE_ON, None),
    ("run_complete", _DONE_ON, None),
    ("cycle_complete", _DONE_ON, None),
    ("run_state", frozenset({"end", "finished", "finish", "complete", "completed"}), None),
    ("job_state", frozenset({"finished", "finish", "end", "complete", "completed"}), None),
    ("washer_job_state", frozenset({"finished", "end"}), None),
    ("dryer_job_state", frozenset({"finished", "end"}), None),
    ("dishwasher_job", frozenset({"finished", "end"}), ApplianceType.DISHWASHER),
)

_METER_NAMES = (
    "electric consumption", "home consumption", "home energy",
    "total consumption", "main power", "whole house", "grid consumption",
    "mains power",
)

# Whole-home delta tracking.
_JUMP_W = 200.0
_DROP_W = 200.0
_DELTA_SUSTAIN_S = 30.0
_WINDOW = 20
_DELTA_TIMEOUT_S = 14400.0
_SAME_LOAD_W = 300.0
# A declared washer, dryer or dishwasher pauses mid-cycle (a heater switching
# off, the drum resting), so its load must stay off this long to be finished.
_CYCLE_PAUSE_S = 600.0
_PAUSING_TYPES = frozenset({"WASHER", "DRYER", "DISHWASHER"})

# Loads Nova must never call "done" (always on, or a safety concern).
_NEVER_ANNOUNCE = (
    "fridge", "refrigerator", "freezer", "ice maker", "icemaker",
    "air condition", "air-condition", "aircon", "hvac", "heat pump",
    "heatpump", "mini split", "minisplit", "furnace", "air handler",
    "stove", "oven", "range", "cooktop", "cook top", "wall oven",
)
_NEVER_ANNOUNCE_TOKENS = frozenset({"ac", "a/c"})

# Discovery methods whose completions may be spoken.
_TRUSTED_METHODS = ("native_status", "declared_entity", "explicit_config",
                    "whole_home_match")


# ── state ───────────────────────────────────────────────────────────────────

@dataclass
class _SensorState:
    """One tracked power sensor and its cycle state machine."""

    entity_id: str
    friendly_name: str
    appliance: ApplianceType
    phase: str = "idle"            # "idle" or "running" ("settling" is unused)
    run_start: float = 0.0
    settle_start: float = 0.0
    last_power: float = 0.0
    peak_power: float = 0.0
    announced: bool = False
    discovery_method: str = ""
    identified: bool = True
    sibling_entities: list = field(default_factory=list)


@dataclass
class _NativeAppliance:
    """An appliance's own completion entity."""

    entity_id: str
    device_name: str
    appliance: ApplianceType
    trigger_states: frozenset
    last_state: str = ""
    announced: bool = False
    seeded: bool = False


@dataclass
class _DeltaTracker:
    """The whole-home meter and the loads currently seen on it."""

    entity_id: str
    baseline_w: float = 0.0
    samples: list = field(default_factory=list)
    active_deltas: dict = field(default_factory=dict)
    last_reading: float = 0.0
    _seq: int = field(default=0, init=False, repr=False)

    def next_id(self) -> str:
        self._seq += 1
        return f"delta_{self._seq}"


class _MonitorState:
    """Everything the monitor knows; one instance, _MON."""

    def __init__(self) -> None:
        self.hass: Optional[HomeAssistant] = None
        self.config: dict = {}
        self.entry: Any = None
        self.sensors: dict[str, _SensorState] = {}
        self.natives: dict[str, _NativeAppliance] = {}
        self.delta: Optional[_DeltaTracker] = None
        self.profile: list = []
        self.disagg: list = []
        self.claimed: set = set()
        self.unsub: Any = None
        self.unsub_settle: Any = None
        self.running: bool = False


_MON = _MonitorState()


# ── pure helpers ────────────────────────────────────────────────────────────

def _classify_appliance(entity_id: str, friendly_name: str) -> Optional[ApplianceType]:
    """The type named by the most specific keyword in the id or name."""
    text = f"{entity_id} {friendly_name}".lower()
    best: Optional[tuple[str, ApplianceType]] = None
    for word, atype in _KEYWORDS:
        if word in text and (best is None or len(word) > len(best[0])):
            best = (word, atype)
    return best[1] if best else None


def _fingerprint_from_power(peak_w: float) -> Optional[tuple[ApplianceType, str]]:
    """Guess a type from a cycle's peak draw."""
    for low, high, atype, why in _FINGERPRINTS:
        if low <= peak_w <= high:
            return atype, f"{why} ({peak_w:.0f}W)"
    if peak_w > 6000:
        return ApplianceType.OVEN, f"very high draw, typical of an oven ({peak_w:.0f}W)"
    return None


def _type_to_appliance(type_str: Optional[str]) -> ApplianceType:
    """A declared type string as an ApplianceType (GENERIC when unknown)."""
    s = (type_str or "").strip().lower()
    if s:
        for word, atype in sorted(_KEYWORDS, key=lambda kw: -len(kw[0])):
            if word == s or word in s:
                return atype
    for atype in ApplianceType:
        if atype.label == s:
            return atype
    return ApplianceType.GENERIC


def _as_watts(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalize_profile(config: dict) -> list[dict]:
    """The declared appliance profile as clean dicts."""
    raw = config.get("appliance_profile")
    if isinstance(raw, str):
        if not raw.strip():
            return []
        import json
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        watts = _as_watts(item.get("watts"))
        out.append({
            "name": name,
            "type": str(item.get("type") or "appliance").strip().lower(),
            "entity": str(item.get("entity") or "").strip(),
            "watts": watts,
            "learned_w": watts,
            "observed_n": 0,
        })
    return out


def _claimed_running_near(delta_w: float) -> bool:
    """True when a running, user-claimed power sensor already explains a load."""
    for eid in _MON.claimed:
        sensor = _MON.sensors.get(eid)
        if sensor is None or sensor.phase != "running":
            continue
        ref = max(sensor.peak_power, sensor.last_power)
        if ref > 0 and abs(delta_w - ref) / max(ref, 1) <= 0.5:
            return True
    return False


def _match_declared(delta_w: float) -> tuple[Optional[dict], Optional[float], bool]:
    """The declared appliance closest to a whole-home load, if one is close."""
    if _claimed_running_near(delta_w):
        return None, None, False
    candidates: list[tuple[float, dict]] = []
    for ap in _MON.disagg:
        w = ap.get("learned_w") or ap.get("watts")
        if not w or w <= 0:
            continue
        err = abs(delta_w - w) / w
        if err <= max(0.35, 250.0 / w):
            candidates.append((err, ap))
    if not candidates:
        return None, None, False
    candidates.sort(key=lambda c: c[0])
    best_err, best = candidates[0]
    ambiguous = len(candidates) > 1 and (candidates[1][0] - best_err) < 0.10
    return best, best_err, ambiguous


def _learn_watts(ap: dict, observed_w: float, ambiguous: bool) -> None:
    """Nudge a declared appliance's wattage toward a confident observation."""
    if ambiguous or observed_w <= 0:
        return
    base = ap.get("learned_w") or ap.get("watts") or observed_w
    if abs(observed_w - base) / base > 0.25:
        return
    ap["learned_w"] = round(0.8 * base + 0.2 * observed_w, 1)
    ap["observed_n"] = ap.get("observed_n", 0) + 1


def _is_never_announce(appliance: ApplianceType, label: str, friendly: str) -> bool:
    """Appliances whose 'done' Nova must never say."""
    if appliance == ApplianceType.OVEN:
        return True
    text = f"{label} {friendly}".lower()
    if any(word in text for word in _NEVER_ANNOUNCE):
        return True
    import re
    return bool(_NEVER_ANNOUNCE_TOKENS & set(re.findall(r"[a-z0-9/]+", text)))


def _parse_watts(state: Any) -> Optional[float]:
    """A power state in watts, or None when it is not a reading."""
    raw = state.state
    if raw in _SKIP_STATES:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    unit = str(state.attributes.get("unit_of_measurement") or "").lower()
    return value * 1000.0 if unit == "kw" else value


def _friendly(state: Any) -> str:
    return state.attributes.get("friendly_name") or ""


def _looks_like_power(state: Any, units: frozenset = _POWER_UNITS) -> bool:
    attrs = state.attributes
    unit = str(attrs.get("unit_of_measurement") or "").lower()
    return attrs.get("device_class") == "power" or unit in units


def _native_pattern(text: str) -> Optional[tuple[str, frozenset, Optional[ApplianceType]]]:
    for pattern in _NATIVE_PATTERNS:
        if pattern[0] in text:
            return pattern
    return None


# ── discovery ───────────────────────────────────────────────────────────────

def _get_device_siblings(hass: HomeAssistant, eid: str) -> tuple[Optional[str], list]:
    """The entity's device name and the other entities on that device."""
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(hass)
        entry = ent_reg.async_get(eid)
        if entry is None or not entry.device_id:
            return None, []
        device = dr.async_get(hass).async_get(entry.device_id)
        # Precedence as written: a missing device raises here (caught below).
        name = device.name_by_user or device.name or "" if device else ""
        siblings = [e.entity_id for e in ent_reg.entities.values()
                    if e.device_id == entry.device_id and e.entity_id != eid]
        return name, siblings
    except Exception:  # noqa: BLE001 - registries are best effort
        return None, []


def _get_entity_area(hass: HomeAssistant, eid: str) -> Optional[str]:
    """The entity's (or its device's) area name, lowercased."""
    try:
        from homeassistant.helpers import area_registry as ar
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        entry = er.async_get(hass).async_get(eid)
        if entry is None:
            return None
        area_id = entry.area_id
        if not area_id and entry.device_id:
            device = dr.async_get(hass).async_get(entry.device_id)
            area_id = device.area_id if device else None
        if not area_id:
            return None
        area = ar.async_get(hass).async_get_area(area_id)
        return area.name.lower() if area and area.name else None
    except Exception:  # noqa: BLE001
        return None


def _identify_power_sensor(hass: HomeAssistant, state: Any) -> Optional[_SensorState]:
    """Work out what one power sensor is for, or None to leave it alone."""
    eid = state.entity_id
    friendly = _friendly(state)
    device_name, siblings = _get_device_siblings(hass, eid)

    def _make(atype: ApplianceType, name: str, method: str,
              identified: bool = True) -> _SensorState:
        return _SensorState(entity_id=eid, friendly_name=name, appliance=atype,
                            discovery_method=method, identified=identified,
                            sibling_entities=list(siblings))

    atype = _classify_appliance(eid, friendly)
    if atype is not None:
        return _make(atype, friendly or eid, "keyword")
    if device_name:
        atype = _classify_appliance("", device_name)
        if atype is not None:
            return _make(atype, device_name, "device_sibling")
    for sib in siblings:
        sib_state = hass.states.get(sib)
        sib_friendly = _friendly(sib_state) if sib_state is not None else ""
        atype = _classify_appliance(sib, sib_friendly)
        if atype is not None:
            return _make(atype, sib_friendly or friendly or eid, "sibling_name")
    area = _get_entity_area(hass, eid)
    if area and any(word in area for word in _APPLIANCE_AREAS):
        return _make(ApplianceType.WASHER, friendly or eid, f"area:{area}")

    text = f"{eid} {friendly}".lower()
    if any(word in text for word in _NOT_AN_APPLIANCE):
        return None
    watts = _parse_watts(state)
    if watts is not None and watts > _UNIDENTIFIED_MAX_W:
        return None
    return _make(ApplianceType.GENERIC, friendly or eid, "unidentified",
                 identified=False)


def _discover_sensors(hass: HomeAssistant, exclusions: Optional[tuple] = None
                      ) -> dict[str, _SensorState]:
    """Every power sensor worth tracking. Runs in the executor: exclusions
    come from the snapshot passed in, never from live config."""
    from .entity_filter import is_excluded
    found: dict[str, _SensorState] = {}
    for state in hass.states.async_all("sensor"):
        if not _looks_like_power(state):
            continue
        if is_excluded(hass, state.entity_id, exclusions):
            continue
        sensor = _identify_power_sensor(hass, state)
        if sensor is not None:
            found[state.entity_id] = sensor
    return found


def _discover_native_appliances(hass: HomeAssistant) -> dict[str, _NativeAppliance]:
    """Appliances' own completion entities, one per device."""
    found: dict[str, _NativeAppliance] = {}
    devices_seen: set[str] = set()
    for domain in ("binary_sensor", "sensor"):
        for state in hass.states.async_all(domain):
            eid = state.entity_id
            friendly = _friendly(state)
            pattern = (_native_pattern(eid.lower())
                       or _native_pattern(friendly.lower()))
            if pattern is None:
                continue
            _p, done, forced = pattern
            device_name, _sibs = _get_device_siblings(hass, eid)
            key = (device_name or eid).lower()
            if key in devices_seen:
                continue
            devices_seen.add(key)
            atype = (_classify_appliance(eid, friendly)
                     or _classify_appliance("", device_name or "")
                     or ApplianceType.GENERIC)
            if forced is not None:
                atype = forced
            found[eid] = _seeded(_NativeAppliance(
                entity_id=eid,
                device_name=device_name or friendly.lower() or eid,
                appliance=atype,
                trigger_states=done,
                last_state=str(state.state).lower(),
            ))
    return found


def _seeded(native: _NativeAppliance) -> _NativeAppliance:
    """Take a real starting state as the first one seen, so a cycle already
    under way when the monitor starts is announced when it finishes. An
    unknown or unavailable start leaves the first event to seed it."""
    if native.last_state not in _SKIP_STATES:
        native.seeded = True
        native.announced = native.last_state in native.trigger_states
    return native


def _discover_whole_home_meter(hass: HomeAssistant) -> Optional[_DeltaTracker]:
    """The house's total power meter, if one can be recognised."""
    import re
    candidates: list[tuple[bool, float, str]] = []
    for state in hass.states.async_all("sensor"):
        if not _looks_like_power(state, _METER_UNITS):
            continue
        eid = state.entity_id.lower()
        friendly = _friendly(state).lower()
        if not any(name in friendly or name.replace(" ", "_") in eid
                   for name in _METER_NAMES):
            continue
        watts = _parse_watts(state) or 0.0
        numbered = bool(re.search(r"\(\d+\)$", friendly))
        candidates.append((numbered, -watts, state.entity_id))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    numbered, neg_w, eid = candidates[0]
    return _DeltaTracker(entity_id=eid, baseline_w=-neg_w, last_reading=-neg_w)


# ── the per-sensor state machine ────────────────────────────────────────────

def _finish_cycle(sensor: _SensorState) -> Optional[str]:
    sensor.phase = "idle"
    sensor.run_start = 0.0
    sensor.settle_start = 0.0
    if not sensor.identified:
        guess = _fingerprint_from_power(sensor.peak_power)
        if guess is not None:
            sensor.appliance = guess[0]
            sensor.identified = True
            sensor.discovery_method = f"fingerprint:{sensor.peak_power:.0f}W"
    if sensor.announced:
        return None
    sensor.announced = True
    return sensor.appliance.label


def _process_reading(sensor: _SensorState, power_w: float) -> Optional[str]:
    """Feed one reading; returns the appliance label when a cycle completes."""
    now = time.time()
    a = sensor.appliance
    sensor.last_power = power_w

    if sensor.phase == "idle":
        if power_w < a.run_threshold:
            sensor.run_start = 0.0
        elif not sensor.run_start:
            sensor.run_start = now
        elif now - sensor.run_start >= a.sustained_seconds:
            sensor.phase = "running"
            sensor.announced = False
            sensor.peak_power = power_w
            _LOGGER.debug("Appliance %s started (%s, %.0fW)",
                          sensor.entity_id, a.label, power_w)
        return None

    # running
    sensor.peak_power = max(sensor.peak_power, power_w)
    if power_w >= a.idle_threshold:
        sensor.settle_start = 0.0
        return None
    if not sensor.settle_start:
        sensor.settle_start = now
        return None
    if now - sensor.settle_start >= a.settle_seconds:
        return _finish_cycle(sensor)
    return None


def _schedule(sensor: _SensorState, label: str) -> None:
    """Hand a completion to the announcer as a task on the loop."""
    hass = _MON.hass
    if hass is None:
        return
    hass.async_create_task(_announce_done(sensor, label))


def _settle_tick(_now: Any = None) -> None:
    """Re-check cycles waiting to settle whose sensor has gone quiet."""
    if not _MON.running:
        return
    for sensor in list(_MON.sensors.values()):
        if sensor.phase != "running" or sensor.settle_start == 0:
            continue
        label = _process_reading(sensor, sensor.last_power)
        if label:
            _schedule(sensor, label)


# ── whole-home delta tracking ───────────────────────────────────────────────

def _new_delta(tracker: _DeltaTracker, reading: float, diff: float) -> None:
    matched: Optional[dict] = None
    ambiguous = False
    if _MON.profile:
        best, _err, ambiguous = _match_declared(diff)
        if best is not None:
            matched = best
            guess = best["name"]
            w = best.get("learned_w") or best.get("watts") or 0
            reason = f"matches declared {best['name']} (~{w:.0f}W)"
        else:
            guess, reason = "unknown", "no declared appliance matches"
    else:
        fp = _fingerprint_from_power(diff)
        if fp is not None:
            guess, reason = fp[0].label, fp[1]
        else:
            guess, reason = "unknown", "no fingerprint matches"
    did = tracker.next_id()
    tracker.active_deltas[did] = dict(
        delta_w=diff, peak_w=reading, start_time=time.time(),
        appliance_guess=guess, matched=matched, ambiguous=ambiguous,
        reason=reason, announced=False,
    )
    _LOGGER.debug("Whole-home load %s: +%.0fW (%s: %s)", did, diff, guess, reason)


def _end_delta(tracker: _DeltaTracker, did: str, d: dict) -> None:
    delta_w = d["delta_w"]
    matched = d.get("matched")
    if matched is not None:
        _learn_watts(matched, delta_w, d.get("ambiguous", False))
        synth = _SensorState(
            entity_id=tracker.entity_id, friendly_name=matched["name"],
            appliance=_type_to_appliance(matched.get("type")),
            discovery_method=f"whole_home_match:{delta_w:.0f}W",
            peak_power=d["peak_w"])
        _schedule(synth, matched["name"])
        return
    if _MON.profile:
        _LOGGER.info("Whole-home load %s ended (%.0fW) matching no declared "
                     "appliance; not announced", did, delta_w)
        return
    guess = d.get("appliance_guess", "unknown")
    if guess == "unknown":
        return
    fp = _fingerprint_from_power(delta_w)
    atype = fp[0] if fp is not None else ApplianceType.GENERIC
    synth = _SensorState(
        entity_id=tracker.entity_id, friendly_name=f"Detected {guess.title()}",
        appliance=atype, discovery_method=f"whole_home_delta:{delta_w:.0f}W",
        peak_power=d["peak_w"])
    _schedule(synth, atype.label)


def _pause_s(d: dict) -> float:
    """How long a load must stay off before it counts as finished."""
    matched = d.get("matched")
    if matched is None:
        return 0.0
    atype = _type_to_appliance(matched.get("type"))
    return _CYCLE_PAUSE_S if atype.name in _PAUSING_TYPES else 0.0


def _process_delta(power_w: float) -> None:
    """Feed one whole-home reading into load start/stop detection.

    Loads are sized by the step they add on top of the loads already
    running, so a kettle during a dryer cycle is seen as a kettle. A load
    whose step goes away is held as ended (``ended_at``) and finished once
    it has stayed off for its pause; it resumes if its draw comes back."""
    tracker = _MON.delta
    if tracker is None:
        return
    tracker.last_reading = power_w
    if not tracker.active_deltas:
        tracker.samples.append(power_w)
        tracker.samples = tracker.samples[-_WINDOW:]
        tracker.baseline_w = sum(tracker.samples) / len(tracker.samples)

    now = time.time()

    def _running() -> list[tuple[str, dict]]:
        return [(did, d) for did, d in tracker.active_deltas.items()
                if not d.get("ended_at")]

    def _level() -> float:
        return tracker.baseline_w + sum(d["delta_w"] for _did, d in _running())

    diff = power_w - tracker.baseline_w
    step = power_w - _level()
    if diff > _JUMP_W:
        same = None
        for _did, d in _running():
            if abs(d["delta_w"] - diff) < _SAME_LOAD_W:
                same = d
                break
        if same is not None:
            if power_w > same["peak_w"]:
                same["peak_w"] = power_w
        elif step > _JUMP_W:
            resumed = None
            for d in tracker.active_deltas.values():
                if d.get("ended_at") and abs(d["delta_w"] - step) < _SAME_LOAD_W:
                    resumed = d
                    break
            if resumed is not None:
                resumed.pop("ended_at", None)
            else:
                _new_delta(tracker, power_w, step)

    aged = [(did, d) for did, d in _running()
            if now - d["start_time"] > _DELTA_SUSTAIN_S and not d.get("announced")]
    if power_w < tracker.baseline_w + _DROP_W:
        for _did, d in aged:
            d["ended_at"] = now
    elif aged:
        missing = _level() - power_w
        if missing > _DROP_W:
            _did, d = min(aged, key=lambda item: abs(item[1]["delta_w"] - missing))
            if abs(d["delta_w"] - missing) < _SAME_LOAD_W:
                d["ended_at"] = now

    for did, d in list(tracker.active_deltas.items()):
        ended_at = d.get("ended_at")
        if not ended_at:
            continue
        if ended_at != now and power_w - _level() >= _DROP_W:
            d.pop("ended_at", None)          # its draw came back: same cycle
        elif now - ended_at >= _pause_s(d):
            d["announced"] = True
            tracker.active_deltas.pop(did, None)
            _end_delta(tracker, did, d)

    for did, d in list(tracker.active_deltas.items()):
        if now - d["start_time"] > _DELTA_TIMEOUT_S:
            tracker.active_deltas.pop(did, None)


# ── state changes ───────────────────────────────────────────────────────────

def _native_event(native: _NativeAppliance, new_state: Any, old_state: Any) -> None:
    new = str(new_state.state).lower()
    old = str(old_state.state).lower() if old_state is not None else ""
    native.last_state = new
    done = native.trigger_states
    if not native.seeded:
        native.seeded = True
        if new in done:
            native.announced = True
        return
    if new not in done:
        native.announced = False
        return
    if old in done or native.announced:
        return
    native.announced = True
    synth = _SensorState(entity_id=native.entity_id,
                         friendly_name=native.device_name,
                         appliance=native.appliance,
                         discovery_method="native_status", peak_power=0.0)
    _schedule(synth, native.appliance.label)


@callback
def _on_state_changed(event: Event) -> None:
    """The bus listener: route each state change to its tracker."""
    if not _MON.running:
        return
    data = event.data
    new_state = data.get("new_state")
    if new_state is None:
        return
    eid = data.get("entity_id")

    native = _MON.natives.get(eid)
    if native is not None:
        _native_event(native, new_state, data.get("old_state"))
        return

    sensor = _MON.sensors.get(eid)
    if sensor is not None:
        watts = _parse_watts(new_state)
        if watts is None:
            return
        label = _process_reading(sensor, watts)
        if label:
            _schedule(sensor, label)
        return

    tracker = _MON.delta
    if tracker is not None and eid == tracker.entity_id:
        watts = _parse_watts(new_state)
        if watts is not None:
            _process_delta(watts)


# ── the announcement chokepoint ─────────────────────────────────────────────

async def _announce_done(sensor: _SensorState, appliance_label: str) -> None:
    """Speak or push one cycle completion, if it may be announced at all."""
    hass = _MON.hass
    try:
        from .entity_filter import is_excluded
        if is_excluded(hass, sensor.entity_id):
            return
    except Exception:  # noqa: BLE001 - an exclusion lookup failure is not a veto
        pass
    if _is_never_announce(sensor.appliance, appliance_label, sensor.friendly_name or ""):
        _LOGGER.debug("Appliance %s is never announced", sensor.entity_id)
        return
    if not sensor.discovery_method.startswith(_TRUSTED_METHODS):
        _LOGGER.info("Appliance %s finished (%s, via %s); untrusted, not announced",
                     sensor.entity_id, appliance_label, sensor.discovery_method)
        return

    try:
        from . import honorific as _hon
        title = _hon.effective_honorific(hass)
    except Exception:  # noqa: BLE001
        title = _MON.config.get("honorific", "sir")

    nice = appliance_label.replace("_", " ").title()
    friendly = sensor.friendly_name or nice
    if sensor.appliance == ApplianceType.GENERIC:
        sentence = f"{friendly} has finished its cycle."
    elif appliance_label.lower() in friendly.lower():
        sentence = f"the {nice} cycle is complete."
    else:
        sentence = f"{friendly} has finished its cycle. The {nice} is done."

    from . import delivery, persona
    message = persona.lead_in(title, sentence)
    await delivery.async_deliver(hass, delivery.DeliveryRequest(
        source="appliance",
        message=message,
        entity_id=sensor.entity_id,
        category="appliance",
        push=delivery.PUSH_HIGH,
        title="Nova",
        tts_context="sentinel",
        config=_MON.config,
        motion_counts=True,
    ))


# ── live settings ───────────────────────────────────────────────────────────

def _live_runtime_config() -> dict:
    """The owner entry's live runtime_config ({} without an entry)."""
    entry = _MON.entry
    if entry is None:
        return {}
    from .runtime import lifecycle_runtime_config
    return lifecycle_runtime_config(entry)


# ── start / stop ────────────────────────────────────────────────────────────

async def _resolve_config(hass: HomeAssistant, config: dict) -> dict:
    """A copy of config whose appliance_profile follows live > saved > given."""
    resolved = dict(config)
    live = _live_runtime_config()
    try:
        from . import nova_config
        saved = await hass.async_add_executor_job(nova_config.get_all)
    except Exception:  # noqa: BLE001 - the saved file is optional here
        saved = None
    if live.get("appliance_profile") is not None:
        resolved["appliance_profile"] = live["appliance_profile"]
    elif isinstance(saved, dict) and saved.get("appliance_profile") is not None:
        resolved["appliance_profile"] = saved["appliance_profile"]
    return resolved


def _drop_superseded_sensors(hass: HomeAssistant) -> None:
    """A native completion entity beats a power sensor on the same device."""
    native_names = set()
    for native in _MON.natives.values():
        reg_name, _s = _get_device_siblings(hass, native.entity_id)
        name = (reg_name or native.device_name or "").strip().lower()
        if name:
            native_names.add(name)
    for eid in list(_MON.sensors):
        reg_name, _s = _get_device_siblings(hass, eid)
        name = (reg_name or "").strip().lower()
        if name and name in native_names:
            _MON.sensors.pop(eid, None)


def _add_explicit_sensors(hass: HomeAssistant) -> None:
    """The legacy appliance_sensors mapping {entity_id: label}."""
    mapping = _MON.config.get("appliance_sensors")
    if not isinstance(mapping, dict):
        return
    for eid, label in mapping.items():
        if eid in _MON.sensors:
            continue
        atype = _classify_appliance(eid, str(label)) or ApplianceType.GENERIC
        state = hass.states.get(eid)
        friendly = (state.attributes.get("friendly_name", eid)
                    if state is not None else str(label))
        _MON.sensors[eid] = _SensorState(
            entity_id=eid, friendly_name=friendly, appliance=atype,
            discovery_method="explicit_config",
            identified=atype != ApplianceType.GENERIC)


def _wire_profile(hass: HomeAssistant) -> None:
    """Attach each declared appliance to its entity, or to the meter."""
    _MON.profile = _normalize_profile(_MON.config)
    _MON.claimed = set()
    _MON.disagg = []
    for ap in _MON.profile:
        eid = ap["entity"]
        if not eid:
            if ap["watts"] > 0:
                _MON.disagg.append(ap)
            continue
        _MON.claimed.add(eid)
        atype = _type_to_appliance(ap["type"])
        state = hass.states.get(eid)
        if state is not None and _looks_like_power(state):
            found = _MON.sensors.get(eid)
            if found is None:
                _MON.sensors[eid] = _SensorState(
                    entity_id=eid,
                    friendly_name=_friendly(state) or eid or ap["name"],
                    appliance=atype, discovery_method="declared_entity")
            else:
                # The user's declaration outranks discovery's guess.
                found.discovery_method = "declared_entity"
                found.identified = True
                if atype != ApplianceType.GENERIC:
                    found.appliance = atype
        elif eid not in _MON.natives:
            pattern = _native_pattern(eid.lower())
            _MON.natives[eid] = _seeded(_NativeAppliance(
                entity_id=eid, device_name=ap["name"], appliance=atype,
                trigger_states=pattern[1] if pattern else _DONE_ON,
                last_state=str(state.state).lower() if state is not None else ""))


def _seed_running_sensors(hass: HomeAssistant) -> None:
    """A sensor already drawing a running load when the monitor starts is
    mid-cycle (Nova restarted, or appliances were saved), so its cycle is
    picked up as running and its finish is announced."""
    now = time.time()
    for sensor in _MON.sensors.values():
        state = hass.states.get(sensor.entity_id)
        watts = _parse_watts(state) if state is not None else None
        if watts is None or watts < sensor.appliance.run_threshold:
            continue
        sensor.phase = "running"
        sensor.run_start = now
        sensor.last_power = watts
        sensor.peak_power = watts


async def start(hass: HomeAssistant, config: dict, entry: Any = None) -> None:
    """Discover appliances and begin watching them."""
    if _MON.running:
        await stop()
    _MON.hass = hass
    _MON.entry = entry
    _MON.config = await _resolve_config(hass, config)

    from .entity_filter import exclusion_snapshot
    _MON.sensors = await hass.async_add_executor_job(
        _discover_sensors, hass, exclusion_snapshot(hass))
    _MON.natives = await hass.async_add_executor_job(
        _discover_native_appliances, hass)
    _MON.delta = await hass.async_add_executor_job(
        _discover_whole_home_meter, hass)

    if _MON.delta is not None:
        # The whole-home meter is never an appliance of its own.
        _MON.sensors.pop(_MON.delta.entity_id, None)
    _drop_superseded_sensors(hass)
    _add_explicit_sensors(hass)
    _wire_profile(hass)
    _seed_running_sensors(hass)

    if not _MON.sensors and not _MON.natives and _MON.delta is None:
        _LOGGER.info("Appliance monitor: no power sensors, completion entities "
                     "or whole-home meter found; not running")
        return

    _MON.running = True
    _MON.unsub = hass.bus.async_listen("state_changed", _on_state_changed)
    if _MON.sensors:
        from datetime import timedelta

        from homeassistant.helpers.event import async_track_time_interval
        _MON.unsub_settle = async_track_time_interval(
            hass, callback(_settle_tick), timedelta(seconds=SETTLE_CHECK_SECONDS))
    _LOGGER.info("Appliance monitor running: %d power sensor(s), %d completion "
                 "entit(ies), whole-home meter %s, %d declared appliance(s)",
                 len(_MON.sensors), len(_MON.natives),
                 _MON.delta.entity_id if _MON.delta else "none", len(_MON.profile))


async def stop() -> None:
    """Stop watching. Safe before start and safe to call twice."""
    if _MON.unsub is not None:
        try:
            _MON.unsub()
        except Exception:  # noqa: BLE001
            pass
    if _MON.unsub_settle is not None:
        try:
            _MON.unsub_settle()
        except Exception:  # noqa: BLE001
            pass
        _MON.unsub_settle = None
    _MON.sensors = {}
    _MON.natives = {}
    _MON.delta = None
    _MON.running = False
    _MON.entry = None


def is_running() -> bool:
    return _MON.running


def status() -> dict:
    """What the monitor is watching, for the panel and diagnostics."""
    now = time.time()
    profile = []
    for ap in _MON.profile:
        watts = ap.get("watts") or 0
        profile.append({
            "name": ap.get("name"),
            "type": ap.get("type"),
            "entity": ap.get("entity"),
            "watts": ap.get("watts"),
            "learned_w": round(ap.get("learned_w") or ap.get("watts") or 0),
            "observed_n": ap.get("observed_n"),
            "mode": ("entity" if ap.get("entity")
                     else "disaggregation" if watts > 0 else "declared"),
        })
    sensors = {}
    for eid, s in _MON.sensors.items():
        sensors[eid] = dict(
            friendly_name=s.friendly_name, appliance=s.appliance.label,
            phase=s.phase, identified=s.identified,
            discovery=s.discovery_method, power_w=s.last_power,
            peak_w=s.peak_power, siblings=s.sibling_entities[:5],
        )
    natives = {
        eid: {
            "appliance": n.appliance.label,
            "device_name": n.device_name,
            "trigger_state": sorted(n.trigger_states),
            "current_state": n.last_state,
            "announced": n.announced,
        }
        for eid, n in _MON.natives.items()
    }
    whole_home = None
    tracker = _MON.delta
    if tracker is not None:
        whole_home = {
            "entity_id": tracker.entity_id,
            "baseline_w": tracker.baseline_w,
            "last_reading_w": tracker.last_reading,
            "active_events": len(tracker.active_deltas),
            "deltas": {
                did: {
                    "delta_w": d.get("delta_w"),
                    "peak_w": d.get("peak_w"),
                    "guess": d.get("appliance_guess"),
                    "duration_s": int(now - d.get("start_time", now)),
                }
                for did, d in tracker.active_deltas.items()
            },
        }
    return {
        "running": _MON.running,
        "profile": profile,
        "sensors": sensors,
        "native_appliances": natives,
        "whole_home_delta": whole_home,
    }
