"""
Nova — who is speaking, read from an outside speaker-recognition service.

The service publishes to Home Assistant in one of two ways:

  - one current-speaker sensor whose state is the speaker's name, or
  - a binary sensor per person that turns on when that person is heard.

A per-person sensor can stay on after the person stops talking, so it only
counts while it changed recently. Either way the result is offered to
identity.py as its voice provider.

It also spots an enrolment chance: Nova already knows who is talking from
other signals but the voice service does not recognise them yet. It then
fires `nova_voice_enroll_candidate` so an automation can enrol the sample.
No model runs here.
"""
from __future__ import annotations

import contextlib
import fnmatch
import functools
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_ENROLL_COOLDOWN = 300.0
_RECENCY_WINDOW = 20.0

# Wall clock second each person (in normalised form) was last offered for
# enrolment.
_last_enroll_fire: dict[str, float] = {}

# Current-speaker states that mean nobody is talking.
_NO_SPEAKER = frozenset({
    "", "unknown", "unavailable", "none", "no_speaker", "not_speaking", "idle",
    "off", "uncertain", "unbekannt", "tv-noise", "tv_noise",
})
# Attributes a score may sit in, checked in this order.
_SCORE_ATTRS = (
    "confidence", "score", "probability", "similarity",
)
_NAME_SUFFIXES = (" voice", " speaking", " is speaking")
_ID_SUFFIXES = ("_voice", "_speaking", "_is_speaking")


def _cfg(key: str, default: Any) -> Any:
    """The stored setting as is (None included); the default only on error."""
    try:
        from . import nova_config as settings
        value = settings.get(key, default)
    except Exception:  # settings store not ready yet
        value = default
    return value


def _source() -> str:
    """The configured sensor or pattern, blank when voice is not set up."""
    return (_cfg("voice_recognition_source", "") or "").strip()


def normalize(name: str) -> str:
    """A name in identity.py's form, so both sides compare alike."""
    from .identity import normalize as identity_form
    return identity_form(name)


def _as_float(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _score(st: Any, default: float) -> float:
    """The first readable score attribute, as a share of 1; else `default`."""
    attrs = getattr(st, "attributes", None) or {}
    readings = (_as_float(attrs.get(name)) for name in _SCORE_ATTRS)
    first = next((r for r in readings if r is not None), None)
    if first is None:
        return default
    if first > 1.0:  # given as a percentage
        first /= 100.0
    return max(0.0, min(1.0, first))


def _fresh(st: Any, window: float = _RECENCY_WINDOW) -> bool:
    """True when the state changed within `window` seconds (or we can't tell)."""
    try:
        changed = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
        if changed:
            age = datetime.now(timezone.utc) - changed
            return age.total_seconds() <= window
    except Exception:  # e.g. a naive time that cannot be compared
        pass
    return True


def _person_for(st: Any) -> str:
    """Whom a per-person sensor stands for: its name, else its object id."""
    label = (getattr(st, "attributes", None) or {}).get("friendly_name")
    if label:
        trimmed = functools.reduce(str.removesuffix, _NAME_SUFFIXES, str(label).lower())
        return trimmed.strip()
    object_id = st.entity_id.split(".", 1)[-1]
    return functools.reduce(str.removesuffix, _ID_SUFFIXES, object_id)


def _is_pattern(src: str) -> bool:
    return any(ch in src for ch in "*?[") or src.endswith("_voice")


def _per_person(hass: HomeAssistant, pattern: str, fallback: float) -> dict[str, float]:
    """Binary sensors matching `pattern` that turned on just now, best score each."""
    best: dict[str, float] = {}
    for st in hass.states.async_all("binary_sensor"):
        heard = (fnmatch.fnmatch(st.entity_id, pattern)
                 and str(st.state).lower() == "on"
                 and _fresh(st, _RECENCY_WINDOW))
        who = _person_for(st) if heard else ""
        if who:
            best[who] = max(_score(st, fallback), best.get(who, -1.0))
    return best


def _current_speaker(hass: HomeAssistant, entity_id: str, fallback: float) -> dict[str, float]:
    """The one speaker a current-speaker sensor names, if fresh."""
    st = hass.states.get(entity_id)
    speaker = "" if st is None else str(st.state).strip()
    if speaker.lower() in _NO_SPEAKER or not _fresh(st, _RECENCY_WINDOW):
        return {}
    return {speaker: _score(st, fallback)}


def identify(hass: HomeAssistant, device_id: Optional[str] = None) -> dict[str, float]:
    """{person: score in 0..1} for whoever the voice service hears now."""
    source = _source()
    if not source:
        return {}
    fallback = _as_float(_cfg("voice_recognition_confidence", 0.85))
    if fallback is None:
        fallback = 0.85  # left blank or not a number
    if _is_pattern(source):
        return _per_person(hass, source, fallback)
    return _current_speaker(hass, source, fallback)


def enrollment_candidate(hass: HomeAssistant, device_id: Optional[str] = None) -> Optional[str]:
    """The person Nova knows is talking but the voice service does not."""
    from . import identity
    resolved = identity.resolve(hass, device_id=device_id)
    if not resolved.known or "voice" in resolved.method:
        return None  # nobody known, or voice already helped decide
    heard = {normalize(name) for name in identify(hass, device_id)}
    return None if normalize(resolved.person) in heard else resolved.person


def _enrolment_on() -> bool:
    return (bool(_cfg("identity_voice_fingerprint", False))
            and bool(_source())
            and bool(_cfg("voice_recognition_auto_enroll", True)))


def maybe_fire_enrollment(hass: HomeAssistant, device_id: Optional[str] = None) -> Optional[str]:
    """Fire the enrolment event for a candidate, at most once per person per cool-down."""
    person = enrollment_candidate(hass, device_id) if _enrolment_on() else None
    if not person:
        return None
    key, now = normalize(person), time.time()
    if now - _last_enroll_fire.get(key, 0.0) < _ENROLL_COOLDOWN:
        return None  # offered recently
    _last_enroll_fire[key] = now  # counts even when the event below fails
    payload = {"person": person, "device_id": device_id}
    try:
        hass.bus.async_fire("nova_voice_enroll_candidate", payload)
    except Exception as exc:  # the bus may be closing
        _LOGGER.debug("Voice enrolment event failed: %s", exc)
    else:
        _LOGGER.info("Voice enrolment candidate: %s", person)
    return person


def register(hass: HomeAssistant) -> None:
    """Offer identify() to identity.py as its voice provider. Each setup
    registers a new function, so a stale one is never mistaken for it."""
    def provide(h: HomeAssistant, device_id: Optional[str]) -> dict[str, float]:
        return identify(h, device_id)

    try:
        from .identity import register_voice_provider
        register_voice_provider(provide)
    except Exception as exc:  # identity not importable or not ready
        _LOGGER.warning("Voice recognition provider not registered: %s", exc)
    else:
        _LOGGER.debug("Voice recognition provider registered")


def unregister() -> None:
    """Withdraw the provider at unload; failures don't matter here."""
    with contextlib.suppress(Exception):
        from .identity import register_voice_provider
        register_voice_provider(None)
