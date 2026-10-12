"""Learning voices from everyday use (8.38.0, Nova Voice ID app 0.3.0).

When Nova is very sure who spoke, it adds that voice turn's sample to the app
as "learned", so each person's voice keeps up with how they sound day to day.
Off by default (`voice_id_learning`), and only with "Use voice to tell who is
speaking" (`voice_id_identity`) on too.

The turn records a candidate (offer) once identity has used the voice. The
conversation wrapper starts the commit (schedule) only after the reply is
built, in a background task, so the reply is never delayed and the turn is
never affected. A sample is committed only when every check in check() holds:

  * both switches are on, and the app is 0.3.0 or newer (an older app would
    keep a learned sample as a recorded one);
  * the result is known, with score >= 0.70 and speech_s >= 2.0 (const.py);
  * another signal agrees: without any voice, the same person has at least
    0.6 from being the only one home, a face, or being the only one in the
    satellite's room, and nobody else has 0.3 or more from any signal
    (identity.non_voice_breakdown);
  * it is a voice satellite's turn, not Chat, a recording, a voice_carry, an
    answer to a pending confirmation or to a proactive offer;
  * this sample was never sent before, and no learned commit is in flight for
    this person.

The app has its own rules on top and may answer "refused". Whatever it
answers, the turn is untouched; a refused sample is never sent again, as
learned or as recorded. Nova keeps no voice data: only sample ids and person
ids, in memory.
"""
from __future__ import annotations

from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import re
from typing import Any, Optional

from .const import (
    VOICE_LEARN_AGREE_WEIGHT,
    VOICE_LEARN_MIN_APP,
    VOICE_LEARN_MIN_SCORE,
    VOICE_LEARN_MIN_SPEECH_S,
    VOICE_LEARN_OTHER_MAX,
)

_LOGGER = logging.getLogger(__name__)

AGREEING_SIGNALS = ("sole_occupant", "face", "room_sole")
_SENT_MAX = 500


@dataclass
class Candidate:
    device_id: str
    voice: Any                      # voice_id.VoiceResult
    ident: Any                      # identity.Identification
    blocked: Optional[str] = None   # why this turn can't learn, once known


# The current turn's candidate. Set and read in the conversation turn's own
# task, so two satellites' turns never see each other's.
_CANDIDATE: ContextVar[Optional[Candidate]] = ContextVar("nova_voice_learn", default=None)

# Every sample id Nova has sent to the app (learned or recorded), so none is
# ever sent twice, and the people with a learned commit in flight.
_SENT: "OrderedDict[str, None]" = OrderedDict()
_IN_FLIGHT: set = set()


@dataclass
class Outcome:
    learned: bool = False
    reason: str = ""
    extra: dict = field(default_factory=dict)


def _cfg(key: str, default):
    try:
        from . import nova_config
        return nova_config.get(key, default)
    except Exception:
        return default


def enabled() -> bool:
    """Both switches on: learning needs voice to decide who is speaking."""
    from . import identity
    return identity.voice_identity_enabled() and _cfg("voice_id_learning", False) is True


def clear() -> None:
    """Forget everything (unload)."""
    _SENT.clear()
    _IN_FLIGHT.clear()


def was_sent(sample_id: Optional[str]) -> bool:
    return bool(sample_id) and sample_id in _SENT


def mark_sent(sample_id: Optional[str]) -> None:
    if not sample_id:
        return
    _SENT[sample_id] = None
    _SENT.move_to_end(sample_id)
    while len(_SENT) > _SENT_MAX:
        _SENT.popitem(last=False)


# ── the turn's side ───────────────────────────────────────────────────────

def reset() -> None:
    _CANDIDATE.set(None)


def offer(device_id: Optional[str], voice, ident) -> None:
    """Called once identity has used this turn's voice (switch on)."""
    if device_id and voice is not None and ident is not None:
        _CANDIDATE.set(Candidate(device_id=device_id, voice=voice, ident=ident))


def block(reason: str) -> None:
    """This turn can't learn (a confirmation or offer answer)."""
    cand = _CANDIDATE.get()
    if cand is not None and cand.blocked is None:
        cand.blocked = reason


def take() -> Optional[Candidate]:
    cand = _CANDIDATE.get()
    _CANDIDATE.set(None)
    return cand


def schedule(hass, entry) -> None:
    """After the reply is built: start the commit in the background."""
    cand = take()
    if cand is None:
        return
    try:
        hass.async_create_background_task(
            async_learn(hass, entry, cand), "nova voice id learn")
    except Exception as exc:  # noqa: BLE001 - never affects the turn
        _LOGGER.debug("Nova Voice ID: learning not started: %s", exc)


# ── the checks ────────────────────────────────────────────────────────────

def _version(text: str) -> tuple:
    parts = re.findall(r"\d+", str(text or ""))[:3]
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts)) if parts else ()


def _app_version(entry) -> tuple:
    try:
        from .runtime import lifecycle_runtime
        runtime = lifecycle_runtime(entry) if entry is not None else None
        entity = getattr(runtime, "voice_id", None) if runtime is not None else None
        status = entity.status() if entity is not None else {}
        return _version(status.get("version", "")) if status.get("state") == "connected" else ()
    except Exception:
        return ()


def _satellite_area(hass, device_id: str) -> Optional[str]:
    try:
        from . import audio_routing, continued_conversation
        sat = continued_conversation.satellite_for_device(hass, device_id)
        return audio_routing.entity_area(hass, sat) if sat else None
    except Exception:
        return None


def agrees(hass, person_id: str, device_id: str) -> tuple[bool, str]:
    """Whether a non-voice signal names the same person, and nobody else."""
    from . import identity, presence
    name = presence.person_name(hass, person_id)
    if not name:
        return False, "person not in Home Assistant"
    key = identity.normalize(name)
    breakdown = identity.non_voice_breakdown(hass, area_id=_satellite_area(hass, device_id))
    own = breakdown.get(key, {})
    strong = sum(own.get(s, 0.0) for s in AGREEING_SIGNALS)
    if strong < VOICE_LEARN_AGREE_WEIGHT:
        return False, "no other signal agrees"
    for other, signals in breakdown.items():
        if other != key and sum(signals.values()) >= VOICE_LEARN_OTHER_MAX:
            return False, "someone else is likely"
    return True, ""


def check(hass, entry, cand: Candidate) -> tuple[bool, str]:
    """(ok, reason). Never raises."""
    try:
        if not enabled():
            return False, "off"
        voice, ident = cand.voice, cand.ident
        if cand.blocked:
            return False, cand.blocked
        if getattr(voice, "status", None) != "known" or not getattr(voice, "person", None):
            return False, "not known"
        if not getattr(voice, "sample_id", None):
            return False, "no sample"
        if voice.score is None or voice.score < VOICE_LEARN_MIN_SCORE:
            return False, "low score"
        if voice.speech_s is None or voice.speech_s < VOICE_LEARN_MIN_SPEECH_S:
            return False, "short speech"
        if "voice_carry" in str(getattr(ident, "method", "")):
            return False, "carried"
        if was_sent(voice.sample_id):
            return False, "already sent"
        if voice.person in _IN_FLIGHT:
            return False, "in flight"
        from . import voice_confirm
        if not voice_confirm.is_voice_satellite_device(hass, cand.device_id):
            return False, "not a satellite"
        if _app_version(entry) < VOICE_LEARN_MIN_APP:
            return False, "app too old"
        return agrees(hass, voice.person, cand.device_id)
    except Exception as exc:  # noqa: BLE001
        return False, f"check failed ({type(exc).__name__})"


async def async_learn(hass, entry, cand: Candidate) -> Outcome:
    """Commit the candidate's sample as learned, if every check holds.
    Never raises; logs a VOICE line only when the app saved or refused it."""
    ok, reason = check(hass, entry, cand)
    if not ok:
        _LOGGER.debug("Nova Voice ID: not learning (%s)", reason)
        return Outcome(False, reason)
    from . import voice_id, voice_id_store
    voice = cand.voice
    person = voice.person
    _IN_FLIGHT.add(person)
    mark_sent(voice.sample_id)
    try:
        await voice_id.enrol_commit(
            voice_id_store.get_uri(hass, entry), await voice_id_store.async_get_token(hass),
            voice.sample_id, person, source="learned")
    except voice_id.VoiceIdManagementError as exc:
        _LOGGER.debug("Nova Voice ID: learned sample not saved (%s)", exc.code)
        if exc.code == "refused":
            _log(f"person={person} device={cand.device_id} learn refused ({exc.code})")
        return Outcome(False, exc.code)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova Voice ID: learning failed: %s", type(exc).__name__)
        return Outcome(False, "error")
    finally:
        _IN_FLIGHT.discard(person)
    _log(f"person={person} device={cand.device_id} + learned")
    return Outcome(True, "")


def _log(line: str) -> None:
    try:
        from .ws_log import nova_log
        nova_log("VOICE", line)
    except Exception:
        pass
