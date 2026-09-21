"""
Nova — Camera semantic event learning (Phase 4, v7.109.0).

Turns a meaningful camera detection — from Eufy's native sensors, Frigate,
Nest, or Nova's own vision-analysis pass — into one structured, bounded
event Nova's EXISTING pattern learner (cognitive_core.StateLogger /
pattern_analyzer.py) can use as a sequence trigger. `record_event()` is the
single shared boundary every source calls: one label allowlist, one
confidence filter, one location resolver, one deduplicator. This never
creates a parallel learning system — it only ever writes through
cognitive_core.log_camera_event() into the same state_changes table (and
the same StateLogger instance) every other entity's pattern data already
goes through.

Recorded fields only: canonical label (as the synthetic entity's state),
camera entity, resolved area/location, timestamp (implicit — StateLogger
stamps it), source, confidence (only when the source supplied one),
resident (person events only, only when Nova's existing recognition cache
has a fresh, high-confidence match for THAT camera — never inferred from
"someone is home"), package state (package events only). Never an image,
face embedding, raw provider payload, raw vision-model response, prompt,
or credential — callers physically cannot pass one through this API, since
it has no parameter for any of them.

Camera events are recorded as camera_event.<stable-location> — a synthetic
id that was never registered as a real Home Assistant entity. Nothing in
Nova's action paths (pattern_analyzer.service_for, agent.py's execute_plan
domain allowlist) maps the "camera_event" domain to any service call, so a
learned camera_event trigger can only ever appear on the TRIGGER side of a
suggested automation, never as something Nova proposes to control — see
this module's own _REJECTED_ACTION_DOMAINS docstring note and the two
call-site guards duplicated there as defense in depth.
"""
from __future__ import annotations

import logging
import math
import re
import time
from typing import Optional

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# ── Canonical vocabulary — fixed allowlists, never inferred/extended at runtime ──
CANONICAL_LABELS = ("person", "vehicle", "animal", "package", "activity")
_CANONICAL_LABELS = set(CANONICAL_LABELS)

# Package sub-states stored as the synthetic entity's actual state value for
# package events, so the pattern analyzer can tell "delivered" from "taken"
# apart as distinct triggers rather than one flattened "package" state.
PACKAGE_STATES = ("delivered", "stranded", "taken", "mail")
_PACKAGE_STATES = set(PACKAGE_STATES)

VALID_SOURCES = ("eufy", "frigate", "nest", "vision")
_VALID_SOURCES = set(VALID_SOURCES)

_MAX_STRING_LEN = 200          # every stored string is bounded to this
_CONFIDENCE_SCALE = "0-100 (percent) — matches recognition.py and Frigate's own normalised score, not the raw 0..1 a provider may report."

_DEFAULT_CONFIDENCE_FLOOR = 50.0
_MIN_CONFIDENCE_FLOOR = 0.0     # 0 disables floor filtering entirely
_MAX_CONFIDENCE_FLOOR = 100.0

_DEFAULT_DEDUP_WINDOW = 300.0   # seconds (5 minutes)
_MIN_DEDUP_WINDOW = 0.0
_MAX_DEDUP_WINDOW = 3600.0      # 1 hour — a bound, not a suggestion to set it that high

# ── Bounded, in-memory dedup state ────────────────────────────────────────────
# Keyed by (label, location, package_state) — deliberately NOT keyed by
# camera or source, so the same physical event seen by two different
# integrations watching the same spot (the Eufy + Frigate case the task
# calls out by name) collapses to one row, while a different label at the
# same location, or a different package sub-state, stays distinct.
_DEDUP_MAX_ENTRIES = 500
_dedup_last_seen: dict[tuple, float] = {}


def _now() -> float:
    return time.monotonic()


def _prune_dedup(window: float) -> None:
    """Drop entries older than `window` so the cache can't grow from stale
    keys, then hard-cap size as a second bound regardless of age."""
    if not _dedup_last_seen:
        return
    cutoff = _now() - max(window, _DEFAULT_DEDUP_WINDOW)
    stale = [k for k, ts in _dedup_last_seen.items() if ts < cutoff]
    for k in stale:
        _dedup_last_seen.pop(k, None)
    if len(_dedup_last_seen) > _DEDUP_MAX_ENTRIES:
        # Drop the oldest entries until back under the cap.
        for k, _ in sorted(_dedup_last_seen.items(), key=lambda kv: kv[1])[
            : len(_dedup_last_seen) - _DEDUP_MAX_ENTRIES
        ]:
            _dedup_last_seen.pop(k, None)


def _is_duplicate(key: tuple, window: float) -> bool:
    """True (and does NOT update the cache) if `key` was already recorded
    within `window` seconds. False (and DOES stamp `key` as seen now) when
    it's new — callers only call this once they've decided to record, so a
    call here always means "claim this slot if free"."""
    _prune_dedup(window)
    last = _dedup_last_seen.get(key)
    now = _now()
    if last is not None and window > 0 and (now - last) < window:
        return True
    _dedup_last_seen[key] = now
    if len(_dedup_last_seen) > _DEDUP_MAX_ENTRIES:
        for k, _ in sorted(_dedup_last_seen.items(), key=lambda kv: kv[1])[
            : len(_dedup_last_seen) - _DEDUP_MAX_ENTRIES
        ]:
            _dedup_last_seen.pop(k, None)
    return False


def reset_dedup_state() -> None:
    """Clear all dedup memory. Test-only entry point; production code never
    needs to call this — the window/prune logic ages entries out on its own."""
    _dedup_last_seen.clear()


# ── Pure normalisation helpers ────────────────────────────────────────────────

def bounded(value, max_len: int = _MAX_STRING_LEN) -> str:
    """Coerce to str and hard-cap length — every stored string goes through
    this, since a label/detail/source string here originates from an
    external camera integration and must be treated as untrusted input."""
    return str(value or "")[:max_len]


def normalize_label(raw) -> Optional[str]:
    """A raw source label -> one of CANONICAL_LABELS, or None to reject.
    Fixed allowlist only — an unrecognised label is dropped, never passed
    through or used to build an entity id."""
    label = bounded(raw, 32).strip().lower()
    return label if label in _CANONICAL_LABELS else None


def normalize_package_state(raw) -> Optional[str]:
    label = bounded(raw, 32).strip().lower()
    return label if label in _PACKAGE_STATES else None


def normalize_confidence(raw, floor: float) -> tuple[Optional[float], bool]:
    """(confidence, passes_floor).

    - raw is None -> (None, True): the source supplied no score, so it is
      never rejected for a missing one and never has one invented for it.
    - raw is present but not a finite number in [0, 100] -> (None, False):
      reject the malformed value safely rather than store or trust it.
    - floor <= 0 -> filtering is disabled; any valid score passes.
    """
    if raw is None:
        return None, True
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, False
    if not math.isfinite(value) or value < 0.0 or value > 100.0:
        return None, False
    floor = max(_MIN_CONFIDENCE_FLOOR, min(_MAX_CONFIDENCE_FLOOR, float(floor or 0.0)))
    if floor <= 0.0:
        return value, True
    return value, value >= floor


def clamp_confidence_floor(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CONFIDENCE_FLOOR
    if not math.isfinite(value):
        return _DEFAULT_CONFIDENCE_FLOOR
    return max(_MIN_CONFIDENCE_FLOOR, min(_MAX_CONFIDENCE_FLOOR, value))


def clamp_dedup_window(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_DEDUP_WINDOW
    if not math.isfinite(value):
        return _DEFAULT_DEDUP_WINDOW
    return max(_MIN_DEDUP_WINDOW, min(_MAX_DEDUP_WINDOW, value))


def slugify(value) -> str:
    """A stable, filesystem/entity-id-safe slug. Never derived from a
    mutable friendly name — callers pass a camera entity_id or an HA area
    id/name, both of which the fallback and area path already resolve
    independently of anything a user could rename mid-session in a way
    that breaks the identity (a rename changes the slug too, but never
    produces an unsafe or unbounded one)."""
    s = re.sub(r"[^a-z0-9]+", "_", bounded(value, 128).strip().lower()).strip("_")
    return (s or "unknown")[:64]


def resolve_location(hass: HomeAssistant, camera_entity: str) -> str:
    """The area slug this camera belongs to, via entity -> device -> area
    registry lookups — never a friendly name (renamable, not stable). Falls
    back to a slug of the camera's own entity id when no area is assigned,
    so every camera always resolves to SOME stable location."""
    try:
        from homeassistant.helpers import area_registry as ar
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        entry = er.async_get(hass).async_get(camera_entity)
        area_id = None
        if entry is not None:
            area_id = entry.area_id
            if not area_id and entry.device_id:
                device = dr.async_get(hass).async_get(entry.device_id)
                area_id = device.area_id if device else None
        if area_id:
            area = ar.async_get(hass).async_get_area(area_id)
            if area is not None and getattr(area, "name", None):
                return slugify(area.name)
            return slugify(area_id)
    except Exception as exc:
        _LOGGER.debug("camera_semantic: area resolution failed for %s: %s", camera_entity, exc)
    object_id = camera_entity.split(".", 1)[-1] if "." in camera_entity else camera_entity
    return slugify(object_id)


def synthetic_entity_id(location: str) -> str:
    return f"camera_event.{slugify(location)}"


def attribute_resident(hass: HomeAssistant, camera_entity: str) -> Optional[tuple[str, float]]:
    """(name, confidence) for a PERSON detection at this specific camera,
    only when Nova's existing recognition cache (recognition.py, populated
    by Frigate/DoubleTake face recognition) has a fresh, sufficiently
    confident match for THIS camera — reusing recognition.py's own
    CACHE_MAX_AGE/CONFIDENCE_THRESHOLD, not a new or looser bar. This is
    deliberately camera-scoped, not a "who's home" guess: a sole occupant
    being home is not evidence they were in front of this specific camera.
    `confidence` here is the RECOGNITION's own confidence (0-100, same
    scale recognition.py already uses) — distinct from the detection
    source's own confidence, which record_event tracks separately. Callers
    must never call this for vehicle/animal/package labels — see
    record_event, the only caller."""
    try:
        from . import recognition
        rec = recognition.last_seen_at(hass, camera_entity)
        if not rec:
            return None
        name = str(rec.get("name") or "").strip()
        if not name or name.lower() in ("unknown", "none"):
            return None
        conf = float(rec.get("confidence") or 0.0)
        if conf < recognition.CONFIDENCE_THRESHOLD:
            return None
        return bounded(name, 100), conf
    except Exception as exc:
        _LOGGER.debug("camera_semantic: resident attribution failed for %s: %s", camera_entity, exc)
        return None


def _learning_config(hass: HomeAssistant) -> tuple[bool, float, float]:
    """(camera_event_learning enabled, confidence_floor, dedup_window)."""
    try:
        from . import nova_config
        enabled = bool(nova_config.get("camera_event_learning", True))
        floor = clamp_confidence_floor(nova_config.get("camera_event_confidence_floor", _DEFAULT_CONFIDENCE_FLOOR))
        window = clamp_dedup_window(nova_config.get("camera_event_dedup_window", _DEFAULT_DEDUP_WINDOW))
        return enabled, floor, window
    except Exception:
        return True, _DEFAULT_CONFIDENCE_FLOOR, _DEFAULT_DEDUP_WINDOW


async def record_event(
    hass: HomeAssistant,
    *,
    label: str,
    camera_entity: str,
    source: str,
    confidence=None,
    package_state: Optional[str] = None,
    detail: str = "",
    attribute: bool = True,
) -> bool:
    """The single shared semantic-recording boundary every camera source
    calls (Eufy, Frigate, Nest, Nova's own vision analysis). Returns True
    if a row was written, False if the event was filtered, deduplicated,
    rejected, or learning is disabled — never raises, so a learning-write
    failure can never break camera perception, announcements, or
    notifications upstream of this call.

    Only ever writes: canonical label (as the synthetic entity's state, or
    a package sub-state), the resolved location, source, confidence when
    supplied, a resident name when safely attributed (person only), and a
    bounded package_state. No image, embedding, raw payload, prompt, or
    credential can reach this function — there is no parameter for one.

    `attribute=False` skips resident attribution outright regardless of
    label — for a source that has ALREADY determined this person is
    unrecognised (Eufy's own "stranger" role, for example). Nova's separate
    recognition cache could theoretically hold a stale match for the same
    camera; a source's own "this is not a known person" verdict must not
    be second-guessed by attribution running anyway.
    """
    try:
        norm_label = normalize_label(label)
        if norm_label is None:
            return False
        if source not in _VALID_SOURCES:
            return False
        if not camera_entity or "." not in str(camera_entity):
            return False

        enabled, floor, window = _learning_config(hass)
        if not enabled:
            return False

        norm_confidence, passes = normalize_confidence(confidence, floor)
        if not passes:
            return False

        norm_package_state = None
        if norm_label == "package":
            norm_package_state = normalize_package_state(package_state) if package_state else None

        # Never attribute a resident to anything but a person event — this
        # is the ONLY branch that ever calls attribute_resident, so vehicle/
        # animal/package/activity events structurally can't carry one.
        resident_name = "unknown"
        resident_confidence = 0.0
        if norm_label == "person" and attribute:
            attributed = attribute_resident(hass, camera_entity)
            if attributed:
                resident_name, resident_confidence = attributed

        location = resolve_location(hass, camera_entity)
        entity_id = synthetic_entity_id(location)

        dedup_key = (norm_label, location, norm_package_state)
        if _is_duplicate(dedup_key, window):
            return False

        new_state = f"package_{norm_package_state}" if norm_package_state else norm_label

        from . import cognitive_core
        ok = await hass.async_add_executor_job(
            cognitive_core.log_camera_event,
            entity_id, new_state, location,
            resident_name, resident_confidence, norm_confidence,
        )
        if ok:
            # A live state too — not just the pattern-learning log row — so
            # a suggested automation using this entity as a trigger can
            # actually fire once installed. Deliberately minimal attributes:
            # no resident, no confidence, no package sub-detail beyond what
            # `new_state` already carries — those stay in state_changes
            # only, never in a live entity attribute any signed-in HA user
            # can read from Developer Tools / the Logbook.
            try:
                hass.states.async_set(
                    entity_id, new_state,
                    {"source": source, "area": location},
                )
            except Exception as exc:
                _LOGGER.debug("camera_semantic: live state set failed for %s: %s", entity_id, exc)
            _LOGGER.debug(
                "camera_semantic: recorded %s @ %s (source=%s conf=%s pkg=%s detail=%s)",
                norm_label, entity_id, source, norm_confidence, norm_package_state,
                bounded(detail, 80),
            )
        return ok
    except Exception as exc:
        # A learning-write failure must never break camera perception,
        # Eufy handling, announcements, or notifications.
        _LOGGER.debug("camera_semantic: record_event failed (non-fatal): %s", exc)
        return False
