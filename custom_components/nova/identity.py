"""Who is Nova talking to right now?

The knowledge store's ``subject`` field needs a person. This module combines
the signals the home already has into a best guess with a confidence. Each
tier is optional and none needs a GPU:

* Presence: who is home, from person.* entities. A sole occupant is a
  strong signal.
* Faces: recent Frigate or Double Take recognitions from recognition.py,
  computed on the Frigate host, plus who is seen in the event's own room
  and which tracker is nearest to it.
* Voice (off by default), the strongest vote since it says most directly
  who is speaking. One of two sources, never both:

  - Nova Voice ID: the app's result for this request, given as
    ``resolve(voice=...)`` when ``voice_id_identity`` is on. Only a "known"
    result votes (0.9). A "short" or "not_sure" turn from the same device
    within 60 seconds of a known one carries that person at 0.6.
  - The older current speaker sensor (voice_recognition.py, through
    register_voice_provider), when ``identity_voice_fingerprint`` is on and
    no Voice ID result is given.

Voice only hints at who is talking; it is never a key, and no safety or
confirmation path reads identity (tests/unit/test_voice_safety.py).

Settings, all read through nova_config:
``identity_enabled`` (True), ``identity_min_confidence`` (0.45, below it the
answer is unknown), ``voice_id_identity`` (False) and
``identity_voice_fingerprint`` (False).
"""
from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

from homeassistant.core import HomeAssistant

if TYPE_CHECKING:   # for the annotation only; Voice ID is never imported here
    from .voice_id import VoiceResult

_LOGGER = logging.getLogger(__name__)

UNKNOWN = "unknown"
DEFAULT_PERSONAL_SUBJECT = "primary"  # knowledge subject when nobody is resolved

# The most each signal can add for one person.
_W_SOLE_OCCUPANT = 0.6
_W_HOME_PRIOR = 0.15
_W_FACE = 0.8
_W_VOICE = 0.9
# Room signals: when one person alone is seen in the event's room, the event
# is very likely theirs even with several people home. Sole occupancy of a
# room is common where sole occupancy of the house is not.
_W_ROOM_SOLE = 0.75       # the only person seen in that room
_W_ROOM_PRESENT = 0.30    # one of several seen in that room
_W_PROXIMITY = 0.35       # a tracker places them in that room
_ROOM_FRESH_SECS = 300.0  # room sightings older than this are ignored
_FACE_RECENCY_WINDOW = 300.0  # a face fades to nothing over this many seconds
# A short or unsure Voice ID turn soon after a known one, same satellite.
_W_VOICE_CARRY = 0.6
_VOICE_CARRY_SECS = 60.0

# A recognition with no usable confidence counts as this (0..1 scale).
_DEFAULT_FACE_CONFIDENCE = 0.7


@dataclass
class Identification:
    """One answer from resolve() or quick_identify()."""

    person: str = UNKNOWN            # display name, or UNKNOWN
    confidence: float = 0.0          # 0..1
    method: str = "none"             # the signals that voted, joined by "+"
    candidates: dict = field(default_factory=dict)   # name -> score

    @property
    def known(self) -> bool:
        """Whether a person was named."""
        return self.person != UNKNOWN


def _cfg(key: str, default):
    try:
        from . import nova_config
        return nova_config.get(key, default)
    except Exception:
        return default


def _enabled() -> bool:
    return bool(_cfg("identity_enabled", True))


def normalize(name: str) -> str:
    """A stable subject id for a display name ("Ann Lee" becomes "ann_lee")."""
    return "_".join(str(name or "").strip().lower().split())


def subject_for(ident: Identification) -> str:
    """The knowledge subject: the person, or the default when unknown."""
    if not ident.known:
        return DEFAULT_PERSONAL_SUBJECT
    return normalize(ident.person)


def _unit_confidence(raw, default: float = _DEFAULT_FACE_CONFIDENCE) -> float:
    """A recognition confidence on the 0..1 scale the weights use.

    recognition.py stores a percent (Double Take's confidence, Frigate's
    score times 100). Taken as it was, a face at 98.7 outweighed a voice
    match worth at most 0.9 nearly a hundred times over, so it is divided by
    100 and clamped. None, a bool, text, NaN or infinity give ``default``.
    Never raises.
    """
    if raw is None or isinstance(raw, bool):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    if math.isnan(value) or math.isinf(value):
        return default
    return max(0.0, min(1.0, value / 100.0))


# The older voice sensor plugs in here.

_VOICE_PROVIDER: Optional[Callable[[HomeAssistant, Optional[str]], dict]] = None


def register_voice_provider(fn: Optional[Callable[[HomeAssistant, Optional[str]], dict]]) -> None:
    """Set the voice backend, ``fn(hass, device_id) -> {person: score}`` with
    scores 0..1, or clear it with None. Until one is set the sensor tier
    adds nothing."""
    global _VOICE_PROVIDER
    _VOICE_PROVIDER = fn


def has_voice_provider() -> bool:
    return _VOICE_PROVIDER is not None


def _voice_votes(hass: HomeAssistant, device_id: Optional[str]) -> dict:
    provider = _VOICE_PROVIDER
    if provider is None:
        return {}
    try:
        return provider(hass, device_id) or {}
    except Exception as exc:
        _LOGGER.debug("identity: voice provider failed: %s", exc)
        return {}


# Nova Voice ID. The result is a voice_id.VoiceResult read by its fields
# alone, so nothing from Voice ID is imported.

# device_id -> (person entity id, time). Written only for a known voice
# with the switch on, read only for the same device, cleared on unload.
_VOICE_CARRY: dict = {}


def voice_identity_enabled() -> bool:
    """The Voice ID card's "Use voice to tell who is speaking" switch."""
    return _cfg("voice_id_identity", False) is True


def clear_voice_carry() -> None:
    _VOICE_CARRY.clear()


def _person_display(hass: HomeAssistant, entity_id: str) -> Optional[str]:
    """The name presence gives a person.* entity."""
    try:
        from . import presence
        return presence.person_name(hass, entity_id)
    except Exception as exc:
        _LOGGER.debug("identity: person name lookup failed: %s", exc)
        return None


def _voice_result_votes(hass: HomeAssistant, voice, device_id: Optional[str],
                        now: float) -> list:
    """This request's Voice ID result as [(name, weight, method)]."""
    status = getattr(voice, "status", None)
    person_id = getattr(voice, "person", None)
    if status == "known" and person_id:
        name = _person_display(hass, person_id)
        if not name:
            return []
        if device_id:
            _VOICE_CARRY[device_id] = (person_id, now)
        return [(name, _W_VOICE, "voice")]
    if status not in ("short", "not_sure") or not device_id:
        return []
    carried = _VOICE_CARRY.get(device_id)
    if not carried or not 0.0 <= now - carried[1] <= _VOICE_CARRY_SECS:
        return []
    name = _person_display(hass, carried[0])
    return [(name, _W_VOICE_CARRY, "voice_carry")] if name else []


# Presence, faces, rooms and trackers

def _home_people(hass: HomeAssistant) -> list[str]:
    try:
        from . import presence
        people = presence.get_presence_summary(hass).get("people", [])
    except Exception as exc:
        _LOGGER.debug("identity: presence read failed: %s", exc)
        return []
    return [p["name"] for p in people if p.get("state") == "home" and p.get("name")]


def _is_unknown_face(recognition, name) -> bool:
    """recognition's own "unknown face" test; a stand in module without it
    falls back to the plain label."""
    check = getattr(recognition, "is_unknown_name", None)
    if check is None:
        return str(name).strip().lower() == UNKNOWN
    return bool(check(name))


def _face_votes(hass: HomeAssistant, now: float,
                names: Optional[dict] = None) -> dict:
    """Recent face recognitions as {normalize(name): weight}.

    Each weight is _W_FACE times the 0..1 confidence times how fresh the
    sighting is, so at most 0.8. Keys are normalised like the room and
    tracker votes, so "Sam" and "sam" are one person. An "unknown face"
    label is not a person and never votes. ``names``, when given, is filled
    with {normalised: name as the backend wrote it} for display.
    """
    try:
        from . import recognition
        seen = recognition.who_is_where(hass)  # {camera: name}
    except Exception as exc:
        _LOGGER.debug("identity: face read failed: %s", exc)
        return {}
    votes: dict = {}
    for camera, name in (seen or {}).items():
        try:
            if not name or _is_unknown_face(recognition, name):
                continue
            sighting = recognition.last_seen_at(hass, camera) or {}
            freshness = 1.0 - float(sighting.get("age_seconds", 0.0)) / _FACE_RECENCY_WINDOW
            if freshness <= 0:
                continue
            weight = _W_FACE * _unit_confidence(sighting.get("confidence")) * freshness
            key = normalize(name)
            votes[key] = max(weight, votes.get(key, 0.0))
            if names is not None:
                names.setdefault(key, name)
        except Exception as exc:
            _LOGGER.debug("identity: face read failed for %s: %s", camera, exc)
    return votes


def _entity_area(hass: HomeAssistant, entity_id: str):
    from . import audio_routing
    try:
        return audio_routing.entity_area(hass, entity_id)
    except Exception:
        return None


def _room_votes(hass: HomeAssistant, area_id: Optional[str], now: float) -> dict:
    """Who is seen in this room right now, as {name: weight}.

    Camera recognitions are mapped to areas. When one person alone was
    recently seen in the event's room, a light changing there is almost
    certainly theirs even with three people home; with several seen, each
    gets a smaller share.
    """
    if not area_id:
        return {}
    try:
        from . import recognition
        seen = recognition.who_is_where(hass) or {}   # {camera: name}
        strongest: dict = {}   # normalised -> (name as written, strength 0..1)
        for camera, name in seen.items():
            if not name or name == UNKNOWN or _entity_area(hass, camera) != area_id:
                continue
            sighting = recognition.last_seen_at(hass, camera) or {}
            age = float(sighting.get("age_seconds", 0.0))
            if age > _ROOM_FRESH_SECS:
                continue
            strength = (_unit_confidence(sighting.get("confidence"))
                        * max(0.0, 1.0 - age / _ROOM_FRESH_SECS))
            key = normalize(name)
            if key not in strongest or strength > strongest[key][1]:
                strongest[key] = (name, strength)
        share = _W_ROOM_SOLE if len(strongest) == 1 else _W_ROOM_PRESENT
        return {name: share * max(0.25, strength) for name, strength in strongest.values()}
    except Exception as exc:
        _LOGGER.debug("identity: room votes failed: %s", exc)
        return {}


def _owner_from_tracker(hass: HomeAssistant, tracker_id: str) -> Optional[str]:
    """The person entity that lists this device_tracker as theirs."""
    try:
        for person in hass.states.async_all("person"):
            if tracker_id in (person.attributes.get("device_trackers") or []):
                return person.attributes.get("friendly_name") or person.entity_id.split(".")[-1]
    except Exception:
        pass
    return None


def _proximity_votes(hass: HomeAssistant, area_id: Optional[str]) -> dict:
    """Who a device tracker places in the event's area.

    A tracker counts when its state names the room (Bermuda or ESPresense
    style BLE trackers) or, more weakly, when the tracker entity itself sits
    in that area.
    """
    if not area_id:
        return {}
    votes: dict = {}
    wanted = str(area_id).lower()
    try:
        for tracker in hass.states.async_all("device_tracker"):
            attrs = tracker.attributes
            who = (attrs.get("person") or _owner_from_tracker(hass, tracker.entity_id)
                   or (attrs.get("friendly_name") or "").strip())
            if not who:
                continue
            room = str(tracker.state or "").strip().lower().replace(" ", "_")
            if room and room == wanted:
                weight = _W_PROXIMITY
            elif (_entity_area(hass, tracker.entity_id) or None) == area_id:
                weight = _W_PROXIMITY * 0.6
            else:
                continue
            key = normalize(who)
            votes[key] = max(votes.get(key, 0.0), weight)
    except Exception as exc:
        _LOGGER.debug("identity: proximity votes failed: %s", exc)
    return votes


def person_for_user(hass: HomeAssistant, user_id: Optional[str]) -> Optional[str]:
    """Name of the person.* entity whose user_id is this Home Assistant user,
    or None."""
    if not user_id:
        return None
    try:
        for person in hass.states.async_all("person"):
            if person.attributes.get("user_id") != user_id:
                continue
            fallback = person.entity_id.split(".")[-1].replace("_", " ").title()
            return person.attributes.get("friendly_name") or fallback
    except Exception as exc:
        _LOGGER.debug("identity: person lookup for user failed: %s", exc)
    return None


# Combining the signals

class _Tally:
    """Votes keyed by normalised name, with the name as a signal wrote it
    (so a result reads "Ann", not "ann") and the methods that voted."""

    def __init__(self):
        self.votes: dict = defaultdict(float)
        self.display: dict = {}
        self.methods: set = set()

    def add(self, name: str, weight: float, method: str, *, keep_name: bool = True) -> None:
        key = normalize(name)
        self.votes[key] += weight
        if keep_name:
            self.display.setdefault(key, name)
        self.methods.add(method)


def resolve(hass: HomeAssistant, *, device_id: Optional[str] = None,
            area_id: Optional[str] = None, now: Optional[float] = None,
            user_id: Optional[str] = None,
            voice: Optional["VoiceResult"] = None) -> Identification:
    """The best guess at who this is, with a confidence; person is
    "unknown" when nothing is confident enough. Reads state only, so it is
    safe on every turn.

    ``user_id`` is the logged in Home Assistant user of a typed Chat turn.
    It is certain, so it decides alone: that user's person.* entity, or
    unknown. Presence and faces, which could name someone else, are not
    mixed in.

    ``voice`` is this request's Voice ID result. With voice_id_identity on,
    the voice tier uses it and never the registered provider; otherwise it
    changes nothing.
    """
    if user_id is not None:
        name = person_for_user(hass, user_id)
        if not name:
            return Identification(UNKNOWN, 0.0, "user_unmatched")
        return Identification(name, 1.0, "user", {name: 1.0})
    if not _enabled():
        return Identification(UNKNOWN, 0.0, "disabled")
    now = time.time() if now is None else now
    tally = _Tally()

    home = _home_people(hass)
    if len(home) == 1:
        tally.add(home[0], _W_SOLE_OCCUPANT, "sole_occupant")
    else:
        for name in home:
            tally.add(name, _W_HOME_PRIOR, "home_prior")

    for key, weight in _face_votes(hass, now, tally.display).items():
        tally.add(key, weight, "face", keep_name=False)
    # Room and tracker votes only ever carried the normalised name.
    for name, weight in _room_votes(hass, area_id, now).items():
        tally.add(name, weight, "room", keep_name=False)
    for key, weight in _proximity_votes(hass, area_id).items():
        tally.add(key, weight, "proximity", keep_name=False)

    if voice is not None and voice_identity_enabled():
        for name, weight, method in _voice_result_votes(hass, voice, device_id, now):
            tally.add(name, weight, method)
    elif bool(_cfg("identity_voice_fingerprint", False)):
        for name, score in _voice_votes(hass, device_id).items():
            tally.add(name, _W_VOICE * float(score), "voice")

    if not tally.votes:
        return Identification(UNKNOWN, 0.0, "no_signal")
    ranked = sorted(tally.votes.items(), key=lambda item: item[1], reverse=True)
    leader, top = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    # Strong evidence alone is not enough: a near tie with the runner up
    # keeps the confidence low, as does a single weak vote.
    margin = (top - runner_up) / top if top > 0 else 0.0
    confidence = min(1.0, top) * (0.5 + 0.5 * margin)
    candidates = {tally.display.get(k, k): round(v, 3) for k, v in ranked}
    if confidence < float(_cfg("identity_min_confidence", 0.45)):
        return Identification(UNKNOWN, round(confidence, 3), "low_confidence", candidates)
    return Identification(tally.display.get(leader, leader), round(confidence, 3),
                          "+".join(sorted(tally.methods)), candidates)


def non_voice_breakdown(hass: HomeAssistant, *, area_id: Optional[str] = None,
                        now: Optional[float] = None) -> dict:
    """Each person's evidence other than voice, per signal.

    Returns {normalize(name): {signal: weight}} with signals sole_occupant,
    home_prior, face, room_sole, room_present and proximity: the helpers and
    weights resolve() uses, without any voice tier, kept apart so a caller
    can see which signal agrees. Empty when identity is off; never raises.
    Only voice_learn.py uses it, to check that something besides the voice
    agrees before a sample is learned.
    """
    found: dict = defaultdict(dict)

    def add(key: str, signal: str, weight: float) -> None:
        found[key][signal] = found[key].get(signal, 0.0) + weight

    try:
        if not _enabled():
            return {}
        now = time.time() if now is None else now
        home = _home_people(hass)
        if len(home) == 1:
            add(normalize(home[0]), "sole_occupant", _W_SOLE_OCCUPANT)
        else:
            for name in home:
                add(normalize(name), "home_prior", _W_HOME_PRIOR)
        for key, weight in _face_votes(hass, now).items():
            add(key, "face", weight)
        room = _room_votes(hass, area_id, now)
        room_signal = "room_sole" if len(room) == 1 else "room_present"
        for name, weight in room.items():
            add(normalize(name), room_signal, weight)
        for key, weight in _proximity_votes(hass, area_id).items():
            add(key, "proximity", weight)
    except Exception as exc:
        _LOGGER.debug("identity: non-voice breakdown failed: %s", exc)
    return {key: dict(signals) for key, signals in found.items()}


def resolve_subject(hass: HomeAssistant, **kwargs) -> str:
    """resolve(), returned as the knowledge subject id."""
    return subject_for(resolve(hass, **kwargs))


def quick_identify(hass: HomeAssistant, area_id: Optional[str] = None) -> Identification:
    """A room aware guess with its confidence and candidates.

    The full Identification lets callers keep partial certainty ("probably
    Ann, 0.62") instead of dropping anything short of sure. It runs on every
    state change, so the voice tier is skipped; presence, faces, room and
    trackers are used.
    """
    if not _enabled():
        return Identification(UNKNOWN, 0.0, "disabled")
    try:
        in_room = None
        if area_id:
            # Everything but voice, which resolve() keeps behind its settings.
            in_room = resolve(hass, area_id=area_id)
            if in_room.known:
                return in_room
            # An undecided room still falls back to the house, so a sole
            # occupant is attributed; returning unknown here used to stop
            # single person homes from ever forming personal routines.
        home = _home_people(hass)
        if len(home) == 1:
            return Identification(home[0], _W_SOLE_OCCUPANT, "sole_occupant",
                                  {home[0]: _W_SOLE_OCCUPANT})
        if in_room is not None and in_room.candidates:
            return in_room
        if home:
            return Identification(UNKNOWN, 0.0, "ambiguous_home",
                                  {name: _W_HOME_PRIOR for name in home})
    except Exception as exc:
        _LOGGER.debug("identity: quick_identify failed: %s", exc)
    return Identification(UNKNOWN, 0.0, "no_signal")
