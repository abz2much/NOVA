"""Spoken confirmation before sensitive actions, and spoken follow up questions.

Both are opt in through the panel settings.

confirm() asks a yes or no question before Nova acts on a protected entity
(unlocking, opening a garage, disarming). It picks a channel in this order:

* no assist satellite anywhere: an actionable Confirm / Deny push to the
  household's phones, first tap wins (_confirm_via_notification);
* native (voice_confirm_mode "native", or "auto" when the satellite is known
  to have its own audio out): assist_satellite.ask_question, which speaks,
  waits for playback and listens by itself;
* gated (everything else, and native when it cannot run): the question is
  spoken on the speaker paired with the satellite, Nova waits for playback to
  end, then reopens the satellite's mic. The answer arrives later as an
  ordinary conversation turn, so this path never approves anything itself.

ask_followup() asks an open question the same two ways and lets the answer
come back as a normal turn.

Nothing here raises to a caller. Any failure reads as "not confirmed", so the
protected action does not run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Optional, Sequence

from homeassistant.core import callback

from .answers import NO as _SHARED_NO, YES as _SHARED_YES

_LOGGER = logging.getLogger(__name__)

# (domain, service) pairs that need a confirmation once the feature is on.
# Covers expose both open and open_cover; switch.turn_off only counts for
# switches whose id looks security related (see action_is_protected).
_SENSITIVE = {
    ("lock", "unlock"),
    ("cover", "open"),
    ("cover", "open_cover"),
    ("alarm_control_panel", "alarm_disarm"),
    ("switch", "turn_off"),
}

# A scene, script or automation can unlock or open something itself and can't
# be inspected first, so the whole domain counts, except the services that
# only stop or reload one. Domain level on purpose: every script also gets its
# own script.<name> service. Must match policy._INDIRECTION_DOMAINS.
_INDIRECTION_DOMAINS = {"scene", "script", "automation"}
_INDIRECTION_SAFE = {"", "reload", "turn_off", "stop"}

# The household yes/no words from answers.py plus this channel's own. The
# question names the action, so "unlock it" or "open it" answers it. Home
# Assistant does the matching, hence the spoken contractions.
_YES = sorted(_SHARED_YES | {"unlock it", "open it"})
_NO = sorted(_SHARED_NO | {"don't", "don't do it"})

_ANSWER_TIMEOUT = 30.0              # seconds for a spoken answer
_PLAYBACK_TIMEOUT = 20.0            # seconds for the prompt to finish playing
_NOTIFY_CONFIRM_TIMEOUT = 120.0     # seconds for a phone tap (someone must find it)

# The five outcomes, written straight into an action's approval_result:
#   approved / rejected  a real answer: a matched spoken reply or a phone tap
#   expired              a timed wait (spoken or phone) ended with no answer
#   deferred             gated: asked aloud, the answer can only come later
#                        as a normal turn, so nothing is decided now
#   error                the question could not be asked at all
# confirm() and confirm_via_phone_only() are the bool views: approved or not.
ConfirmResult = str


def _cfg(hass, key: str, default=None):
    try:
        from . import nova_config
        value = nova_config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def is_enabled(hass) -> bool:
    """Voice confirmation is off unless the user turns it on."""
    return bool(_cfg(hass, "voice_confirm_enabled", False))


def _mode(hass) -> str:
    mode = str(_cfg(hass, "voice_confirm_mode", "auto") or "auto").lower()
    return mode if mode in {"native", "gated", "auto"} else "auto"


def action_is_protected(hass, domain: str, service: str, entity_id: str = "") -> bool:
    """Whether this call needs a confirmation first.

    voice_confirm_entities can add an entity, or exempt one with a leading
    "!"; an exemption wins over everything else."""
    if not is_enabled(hass):
        return False
    overrides = _cfg(hass, "voice_confirm_entities", [])
    if entity_id and isinstance(overrides, list):
        if "!" + entity_id in overrides:
            return False
        if entity_id in overrides:
            return True
    if domain == "switch" and service == "turn_off":
        lowered = entity_id.lower()
        return any(word in lowered for word in ("alarm", "security", "camera", "lock"))
    if domain in _INDIRECTION_DOMAINS:
        return (service or "").lower() not in _INDIRECTION_SAFE
    return (domain, service) in _SENSITIVE


# ── Satellites and speakers ─────────────────────────────────────────────────

def _satellite_for_entity(hass, entity_id: str) -> Optional[str]:
    """The satellite to ask through: one in the target entity's area if
    there is one, else the first satellite that is up. None if neither."""
    try:
        from . import audio_routing
        area_id = None
        try:
            from homeassistant.helpers import entity_registry as er
            reg_entry = er.async_get(hass).async_get(entity_id)
            area_id = reg_entry.area_id if reg_entry else None
        except Exception:
            area_id = None
        if area_id:
            in_area = audio_routing.satellites_in_area(hass, area_id)
            if in_area:
                return in_area[0]
        for state in hass.states.async_all("assist_satellite"):
            if state.state not in ("unavailable", "unknown"):
                return state.entity_id
    except Exception:
        pass
    return None


def _satellite_pairings(hass) -> Optional[dict]:
    """The panel's satellite to speaker pairings from the live runtime
    config (a dict, or JSON text of one). None when unset or unreadable."""
    try:
        from .runtime import domain_runtime_config
        raw = domain_runtime_config(hass).get("satellite_pairings")
        if not raw:
            return None
        pairs = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    return pairs if isinstance(pairs, dict) and pairs else None


def _speaker_for_satellite(hass, satellite: str) -> tuple[Optional[str], list]:
    """(tts entity, [media players]) for the gated path.

    The speaker is the one a reply from this satellite would use
    (audio_routing.reply_target: its pairing, its room's speaker, or the
    general one) and is never the satellite itself."""
    try:
        from . import audio_routing, tts_helper
        target = audio_routing.reply_target(
            hass, satellite_entity_id=satellite,
            satellite_pairings=_satellite_pairings(hass))
        if target and target.startswith("assist_satellite."):
            target = None
        configured = _cfg(hass, "tts_engine", "") or ""
        try:
            engine = tts_helper.resolve_tts_entity(hass, configured)
        except Exception:
            engine = None
        if not engine:
            try:
                engine = tts_helper.find_best_tts_entity(hass)
            except Exception:
                engine = configured or None
        return engine, [target] if target else []
    except Exception:
        return None, []


def _satellite_has_audio_out(hass, satellite: str) -> bool:
    """Whether native prompts on this satellite would be heard. The pipeline
    can't be inspected, so this only trusts the satellite_audio_out setting
    (a bool, or a per satellite dict) and otherwise says no."""
    hint = _cfg(hass, "satellite_audio_out", None)
    if isinstance(hint, bool):
        return hint
    if isinstance(hint, dict):
        return bool(hint.get(satellite, False))
    return False


def _use_native(hass, satellite: str) -> bool:
    mode = _mode(hass)
    return mode == "native" or (mode == "auto" and _satellite_has_audio_out(hass, satellite))


# ── Yes or no ───────────────────────────────────────────────────────────────

async def confirm_typed(hass, question: str, *, entity_id: str = "",
                        timeout: float = _ANSWER_TIMEOUT) -> ConfirmResult:
    """Ask `question` and return one of the ConfirmResult values. Never
    raises: an unexpected failure is "error"."""
    try:
        satellite = _satellite_for_entity(hass, entity_id)
        if not satellite:
            _LOGGER.info("voice_confirm: no assist_satellite available; "
                         "falling back to phone confirmation")
            return await _confirm_via_notification(hass, question)
        if _use_native(hass, satellite):
            outcome = await _confirm_native(hass, satellite, question, timeout)
            if outcome is not None:
                return outcome
        return await _confirm_gated(hass, satellite, question, timeout)
    except Exception as exc:
        _LOGGER.warning("voice_confirm.confirm failed (treating as error): %s", exc)
        return "error"


async def confirm(hass, question: str, *, entity_id: str = "",
                  timeout: float = _ANSWER_TIMEOUT) -> bool:
    """True only when the answer was a clear yes; see confirm_typed()."""
    outcome = await confirm_typed(hass, question, entity_id=entity_id, timeout=timeout)
    return outcome == "approved"


def is_voice_satellite_device(hass, device_id: str, *, strict: bool = False) -> bool:
    """Whether device_id is the device of an assist satellite, which means
    the request was spoken rather than typed.

    Opening and unlocking asked for by voice need a phone tap instead of a
    spoken yes, since a spoken yes is as easy to fake as the request. A
    failed lookup reads as False, unless strict=True, where it raises so the
    policy gate can fail closed on an origin it could not check."""
    if not device_id:
        return False
    try:
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(hass)
        if reg is None:
            raise RuntimeError("entity registry unavailable")
        return any(
            (reg_entry := reg.async_get(state.entity_id)) is not None
            and reg_entry.device_id == device_id
            for state in hass.states.async_all("assist_satellite"))
    except Exception as exc:
        if strict:
            raise
        _LOGGER.debug("voice_confirm.is_voice_satellite_device check failed: %s", exc)
        return False


def phone_services_for_user(hass, user_id: str) -> list[str]:
    """notify.mobile_app_* services of the phones tracked by the person
    linked to this Home Assistant user. Empty when there is no such person
    or phone; never raises."""
    if not user_id:
        return []
    try:
        from homeassistant.helpers import entity_registry as er
        from homeassistant.util import slugify
        reg = er.async_get(hass)
        available = hass.services.async_services().get("notify", {})
        found: list[str] = []
        people = [p for p in hass.states.async_all("person")
                  if p.attributes.get("user_id") == user_id]
        for person in people:
            for tracker in person.attributes.get("device_trackers") or []:
                reg_entry = reg.async_get(tracker) if reg is not None else None
                if (reg_entry is None or reg_entry.platform != "mobile_app"
                        or not reg_entry.config_entry_id):
                    continue
                app_entry = hass.config_entries.async_get_entry(reg_entry.config_entry_id)
                device_name = (app_entry.data.get("device_name") if app_entry else "") or ""
                service = "mobile_app_" + slugify(device_name)
                if device_name and service in available and service not in found:
                    found.append(service)
        return found
    except Exception as exc:
        _LOGGER.debug("voice_confirm.phone_services_for_user failed: %s", exc)
        return []


async def confirm_via_phone_only_typed(hass, question: str, *,
                                       timeout: float = _NOTIFY_CONFIRM_TIMEOUT,
                                       services: Optional[Sequence[str]] = None) -> ConfirmResult:
    """Ask on phones only, never by voice. `services` limits the push to
    those notify.mobile_app_* services (a chat turn asks only the user's own
    phones); None asks every phone. Never raises."""
    try:
        if services is None:
            return await _confirm_via_notification(hass, question, timeout)
        return await _confirm_via_notification(hass, question, timeout, services=services)
    except Exception as exc:
        _LOGGER.warning("voice_confirm.confirm_via_phone_only failed (treating as error): %s", exc)
        return "error"


async def confirm_via_phone_only(hass, question: str, *,
                                 timeout: float = _NOTIFY_CONFIRM_TIMEOUT) -> bool:
    """Bool view of confirm_via_phone_only_typed(), used where a spoken yes
    would be no safer than the spoken request it confirms."""
    outcome = await confirm_via_phone_only_typed(hass, question, timeout=timeout)
    return outcome == "approved"


def _answer_id(response) -> Optional[str]:
    """The matched answer id from ask_question's response, which is either
    {"id": ...} or {satellite: {"id": ...}}."""
    if not isinstance(response, dict):
        return None
    if "id" in response:
        return response.get("id")
    return next((v["id"] for v in response.values()
                 if isinstance(v, dict) and "id" in v), None)


async def _confirm_native(hass, satellite: str, question: str,
                          timeout: float) -> Optional[ConfirmResult]:
    """assist_satellite.ask_question with the yes and no sentences.

    "approved" or "rejected" on a matched answer, "expired" when nothing is
    answered within `timeout`, and None when the call can't run or its reply
    can't be read, which tells the caller to try the gated path instead."""
    answers = [{"id": "confirm", "sentences": _YES}, {"id": "deny", "sentences": _NO}]
    request = {"entity_id": satellite, "question": question,
               "preannounce": False, "answers": answers}
    try:
        response = await asyncio.wait_for(
            hass.services.async_call("assist_satellite", "ask_question", request,
                                     blocking=True, return_response=True),
            timeout=timeout)
    except asyncio.TimeoutError:
        _LOGGER.info("voice_confirm: no spoken answer within %.0fs; expiring", timeout)
        return "expired"
    except Exception as exc:
        _LOGGER.debug("voice_confirm native path unavailable: %s", exc)
        return None
    answer = _answer_id(response)
    if answer is None:
        return None
    return "approved" if answer == "confirm" else "rejected"


async def _confirm_gated(hass, satellite: str, question: str,
                         timeout: float) -> ConfirmResult:
    """Speak the question on the satellite's speaker, wait for it to finish
    (so the mic doesn't hear the prompt), then reopen the satellite's mic.

    The spoken reply can't be captured here; it comes back as a normal
    conversation turn, where the agent sees the pending confirmation and
    finishes the action. So this returns "deferred", which callers treat as
    "don't act now", and never approves on its own. No speaker is "error"."""
    from . import tts_helper
    engine, speakers = _speaker_for_satellite(hass, satellite)
    if not (engine and speakers):
        _LOGGER.warning("voice_confirm gated: no speaker for %s; cannot voice prompt",
                        satellite)
        return "error"
    try:
        await tts_helper.async_announce(hass, question, engine, speakers, context="confirm")
    except Exception as exc:
        _LOGGER.debug("voice_confirm gated announce failed: %s", exc)
    await _wait_for_playback(hass, speakers)
    if not await _start_listening(hass, satellite):
        _LOGGER.debug("voice_confirm gated: couldn't reopen mic on %s", satellite)
    return "deferred"


async def _confirm_via_notification(hass, question: str,
                                    timeout: float = _NOTIFY_CONFIRM_TIMEOUT,
                                    services: Optional[Sequence[str]] = None) -> ConfirmResult:
    """Push a Confirm / Deny notification to the household's phones (every
    notify.mobile_app_* service, or only `services`) and take the first tap.

    The tap listener is in place before anything is sent, so a very quick
    tap is never missed. A tap gives "approved" or "rejected", no tap within
    `timeout` gives "expired", and no phone to ask gives "error"."""
    tag = uuid.uuid4().hex[:8]
    yes_action, no_action = f"NOVA_CONFIRM_{tag}", f"NOVA_DENY_{tag}"
    answer: asyncio.Future = asyncio.get_running_loop().create_future()

    @callback
    def _on_action(event) -> None:
        if answer.done():
            return
        action = event.data.get("action")
        if action == yes_action:
            answer.set_result("approved")
        elif action == no_action:
            answer.set_result("rejected")

    unsub = hass.bus.async_listen("mobile_app_notification_action", _on_action)
    try:
        payload = {
            "title": "Nova needs your OK",
            "message": question,
            "data": {
                "actions": [
                    {"action": yes_action, "title": "Confirm"},
                    {"action": no_action, "title": "Deny"},
                ],
                "push": {"interruption-level": "time-sensitive"},
            },
        }
        pushed = 0
        try:
            for name in list(hass.services.async_services().get("notify", {})):
                if not name.startswith("mobile_app_"):
                    continue
                if services is not None and name not in services:
                    continue
                try:
                    await hass.services.async_call("notify", name, payload, blocking=False)
                except Exception as exc:
                    _LOGGER.debug("voice_confirm notify.%s failed: %s", name, exc)
                else:
                    pushed += 1
        except Exception as exc:
            _LOGGER.warning("voice_confirm: enumerate notify services failed: %s", exc)
        if not pushed:
            _LOGGER.warning("voice_confirm: no notify.mobile_app_* service found; cannot confirm")
            return "error"
        try:
            return await asyncio.wait_for(answer, timeout=timeout)
        except asyncio.TimeoutError:
            _LOGGER.info("voice_confirm: no phone response within %.0fs; expiring", timeout)
            return "expired"
    finally:
        unsub()


async def _wait_for_playback(hass, speakers: Sequence[str],
                             timeout: float = _PLAYBACK_TIMEOUT) -> None:
    """Best effort: return once none of `speakers` is playing, or at the
    timeout. A short first pause lets them switch to playing."""
    if not speakers:
        return
    loop = asyncio.get_running_loop()
    give_up_at = loop.time() + timeout
    await asyncio.sleep(0.6)

    def _any_playing() -> bool:
        try:
            for speaker in speakers:
                state = hass.states.get(speaker)
                if state and state.state == "playing":
                    return True
        except Exception:
            pass
        return False

    while loop.time() < give_up_at and _any_playing():
        await asyncio.sleep(0.4)


async def _start_listening(hass, satellite: str) -> bool:
    """Reopen the satellite's mic: assist_satellite.start_conversation with
    no prompt, else the ESPHome action mapped to it in satellite_start_action
    (for example "esphome.kitchen_start_va")."""
    try:
        await hass.services.async_call(
            "assist_satellite", "start_conversation",
            {"entity_id": satellite, "preannounce": False}, blocking=False)
        return True
    except Exception:
        pass
    mapping = _cfg(hass, "satellite_start_action", {})
    if not (isinstance(mapping, dict) and satellite in mapping):
        return False
    try:
        domain, _, service = mapping[satellite].partition(".")
        await hass.services.async_call(domain, service, {}, blocking=False)
        return True
    except Exception as exc:
        _LOGGER.debug("voice_confirm ESPHome start failed: %s", exc)
        return False


# ── Open follow up ──────────────────────────────────────────────────────────

async def ask_followup(hass, question: str, *, turn_context: str = "",
                       entity_id: str = "") -> dict:
    """Ask an open question aloud and reopen the mic for the answer, which
    comes back as a normal turn. Native start_conversation passes
    `turn_context` as extra_system_prompt so a bare "yes" makes sense.

    Returns {ok, mode, satellite}, or {ok: False, error} when the question
    could not be asked (no satellite, or no speaker on the gated path, in
    which case the mic is left alone). Never raises."""
    try:
        satellite = _satellite_for_entity(hass, entity_id)
        if not satellite:
            return {"ok": False, "error": "no assist_satellite available"}
        if _use_native(hass, satellite):
            try:
                await hass.services.async_call(
                    "assist_satellite", "start_conversation",
                    {"entity_id": satellite, "start_message": question,
                     "extra_system_prompt": turn_context or "", "preannounce": False},
                    blocking=True)
                return {"ok": True, "mode": "native", "satellite": satellite}
            except Exception as exc:
                _LOGGER.debug("voice_confirm followup native failed: %s", exc)
        engine, speakers = _speaker_for_satellite(hass, satellite)
        if not (engine and speakers):
            _LOGGER.warning("voice_confirm followup: no speaker for %s; not asking", satellite)
            return {"ok": False, "mode": "gated", "satellite": satellite,
                    "error": "no speaker for the satellite"}
        from . import tts_helper
        await tts_helper.async_announce(hass, question, engine, speakers, context="followup")
        await _wait_for_playback(hass, speakers)
        await _start_listening(hass, satellite)
    except Exception as exc:
        _LOGGER.warning("voice_confirm.ask_followup failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "mode": "gated", "satellite": satellite}


# ── Panel test button ───────────────────────────────────────────────────────

async def announce_test(hass, satellite: Optional[str] = None) -> dict:
    """Play a short assist_satellite.announce so the user can hear whether
    the satellite's own audio reaches a speaker, which native mode needs.
    Returns {ok, satellite, note}. Never raises."""
    try:
        target = satellite or _satellite_for_entity(hass, "")
        if not target:
            return {"ok": False, "note": "no assist_satellite found"}
        await hass.services.async_call(
            "assist_satellite", "announce",
            {"entity_id": target, "message": "Nova voice output test."},
            blocking=True)
    except Exception as exc:
        return {"ok": False, "error": str(exc),
                "note": "assist_satellite.announce failed — use gated mode."}
    return {"ok": True, "satellite": target,
            "note": "Announce fired. If you heard it on your speaker, native "
                    "mode works — otherwise use gated mode."}
