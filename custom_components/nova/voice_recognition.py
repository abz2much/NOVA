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
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_ENROLL_COOLDOWN = 300.0
_RECENCY_WINDOW = 20.0

# normalised person -> epoch seconds of the last enrolment event
_last_enroll_fire: dict = {}

_NO_SPEAKER = {
    "", "unknown", "unavailable", "none", "no_speaker", "not_speaking", "idle",
    "off", "uncertain", "unbekannt", "tv-noise", "tv_noise",
}
_SCORE_ATTRS = ("confidence", "score", "probability", "similarity")
_NAME_SUFFIXES = (" voice", " speaking", " is speaking")
_ID_SUFFIXES = ("_voice", "_speaking", "_is_speaking")


def _cfg(key: str, default: Any) -> Any:
    """The stored setting as is (None included); the default only on error."""
    try:
        from . import nova_config
        return nova_config.get(key, default)
    except Exception:
        return default


def normalize(name: str) -> str:
    from . import identity
    return identity.normalize(name)


def _score(st: Any, default: float) -> float:
    attrs = getattr(st, "attributes", None) or {}
    for key in _SCORE_ATTRS:
        raw = attrs.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 1.0:
            value /= 100.0
        return max(0.0, min(1.0, value))
    return default


def _fresh(st: Any, window: float = _RECENCY_WINDOW) -> bool:
    """True when the state changed within `window` seconds (or we can't tell)."""
    try:
        stamp = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
        if not stamp:
            return True
        return (datetime.now(timezone.utc) - stamp).total_seconds() <= window
    except Exception:
        return True


def _strip_suffixes(text: str, suffixes: tuple[str, ...]) -> str:
    for suffix in suffixes:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text


def _person_for(st: Any) -> str:
    attrs = getattr(st, "attributes", None) or {}
    name = attrs.get("friendly_name")
    if name:
        return _strip_suffixes(str(name).lower(), _NAME_SUFFIXES).strip()
    object_id = st.entity_id.split(".", 1)[-1]
    return _strip_suffixes(object_id, _ID_SUFFIXES)


def _is_pattern(src: str) -> bool:
    return any(ch in src for ch in "*?[") or src.endswith("_voice")


def identify(hass: HomeAssistant, device_id: Optional[str] = None) -> dict[str, float]:
    """{person: score in 0..1} for whoever the voice service hears now."""
    src = (_cfg("voice_recognition_source", "") or "").strip()
    if not src:
        return {}
    try:
        conf = float(_cfg("voice_recognition_confidence", 0.85))
    except (TypeError, ValueError):
        conf = 0.85  # left blank or not a number

    if _is_pattern(src):
        found: dict[str, float] = {}
        for st in hass.states.async_all("binary_sensor"):
            if not fnmatch.fnmatch(st.entity_id, src):
                continue
            if str(st.state).lower() != "on" or not _fresh(st, _RECENCY_WINDOW):
                continue
            person = _person_for(st)
            if not person:
                continue
            score = _score(st, conf)
            if score > found.get(person, -1.0):
                found[person] = score
        return found

    st = hass.states.get(src)
    if st is None:
        return {}
    name = str(st.state).strip()
    if name.lower() in _NO_SPEAKER or not _fresh(st, _RECENCY_WINDOW):
        return {}
    return {name: _score(st, conf)}


def enrollment_candidate(hass: HomeAssistant, device_id: Optional[str] = None) -> Optional[str]:
    """The person Nova knows is talking but the voice service does not."""
    from . import identity
    ident = identity.resolve(hass, device_id=device_id)
    if not ident.known:
        return None
    if "voice" in ident.method:
        return None
    heard = {normalize(name) for name in identify(hass, device_id)}
    if normalize(ident.person) in heard:
        return None
    return ident.person


def maybe_fire_enrollment(hass: HomeAssistant, device_id: Optional[str] = None) -> Optional[str]:
    """Fire the enrolment event for a candidate, at most once per person per cool-down."""
    if not bool(_cfg("identity_voice_fingerprint", False)):
        return None
    if not (_cfg("voice_recognition_source", "") or "").strip():
        return None
    if not _cfg("voice_recognition_auto_enroll", True):
        return None
    person = enrollment_candidate(hass, device_id)
    if not person:
        return None
    key = normalize(person)
    now = time.time()
    if now - _last_enroll_fire.get(key, 0) < _ENROLL_COOLDOWN:
        return None
    _last_enroll_fire[key] = now
    try:
        hass.bus.async_fire("nova_voice_enroll_candidate",
                            {"person": person, "device_id": device_id})
        _LOGGER.info("Voice enrolment candidate: %s", person)
    except Exception as exc:
        _LOGGER.debug("Voice enrolment event failed: %s", exc)
    return person


def register(hass: HomeAssistant) -> None:
    """Offer identify() to identity.py as its voice provider."""
    try:
        from . import identity

        def _provider(h: HomeAssistant, dev: Optional[str]) -> dict[str, float]:
            return identify(h, dev)

        identity.register_voice_provider(_provider)
        _LOGGER.debug("Voice recognition provider registered")
    except Exception as exc:
        _LOGGER.warning("Voice recognition provider not registered: %s", exc)


def unregister() -> None:
    """Withdraw the provider at unload; failures don't matter here."""
    with contextlib.suppress(Exception):
        from . import identity
        identity.register_voice_provider(None)
