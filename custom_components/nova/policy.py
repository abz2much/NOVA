"""Who may change what: the one gate in front of every state changing call.

Each request is rated (low, medium, high or critical) and then either let
through, held for a confirmation, or refused. The confirming itself is
voice_confirm's job, still only when ``voice_confirm_enabled`` is on. What
this module adds is that authority fails closed: if a protected action needs
a confirmation and asking for it breaks, the action is refused, never let
through. Convenience (lights, heating, media) is low risk and never asks.

bulk_control and execute_plan go through the same gate, so neither can reach
a lock, cover or alarm without the checks a single command gets.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

# ── the ratings ─────────────────────────────────────────────────────────────
# Exact pairs first. Behind them, rules by domain catch services nobody listed,
# so a new service on a lock or alarm can never come out as low by omission.

_CRITICAL = {("alarm_control_panel", "alarm_disarm")}
_HIGH = {("lock", "unlock")}
# Anything that can open a cover (garage doors and gates are covers):
# opening it, flipping it, or setting its position.
_COVER_OPENING = {
    ("cover", "open_cover"), ("cover", "open"),
    ("cover", "toggle"), ("cover", "set_cover_position"),
}
_MEDIUM = _COVER_OPENING | {
    ("alarm_control_panel", "alarm_arm_away"),
    ("alarm_control_panel", "alarm_arm_home"),
    ("alarm_control_panel", "alarm_arm_night"),
    ("lock", "open"),                   # a latch release on some locks
}

# Domains that are security by nature: any service on them that does
# something is rated up, listed or not (a future lock.unlatch, say).
_SECURITY_DOMAINS = {"lock", "alarm_control_panel"}
# Words in a service name that lower a guard: the high end.
_OPENING = ("unlock", "disarm", "open", "unlatch", "unbolt")
# Services on a security domain that raise a guard instead (arming is rated
# above), so they stay free of questions.
_SECURITY_SAFE = {"lock", "close", "close_cover"}
# Services that change nothing at all.
_READ_ONLY = {"", "update", "reload"}

# Scenes, scripts and automations can unlock, disarm or open things inside
# themselves, and the gate cannot look in first. This works on the whole
# domain, not on (domain, service) pairs: every script also gets its own
# script.<object_id> service, and listing pairs would repeat the naming gap
# once found with cover.open_cover. Must match voice_confirm's copy.
_INDIRECTION_DOMAINS = {"scene", "script", "automation"}
# Stopping or reloading one cannot set off what it contains.
_INDIRECTION_SAFE = {"reload", "turn_off", "stop"}

# Words that make a plain switch a security one, for turning it off.
_SECURITY_HINTS = ("alarm", "security", "camera", "lock", "garage", "gate", "door")
# Flipping a switch can turn it off, so it is rated like turning it off.
_SWITCH_OFF_SERVICES = ("turn_off", "toggle")

# Actions that let someone in, so a voice alone never authorises them: a
# voice can be imitated. From a voice satellite (or a Chat turn) they always
# need a phone tap, never a spoken yes, whatever voice_confirm_enabled says.
# Locking and closing carry no such risk. Known gap: a scene or script that
# unlocks a door inside itself is not seen here (see _INDIRECTION_DOMAINS).
_VOICE_BLOCKED_OPENING = _COVER_OPENING | {
    ("lock", "unlock"),
    # 8.24.0: an imitated voice must not switch the alarm off either.
    ("alarm_control_panel", "alarm_disarm"),
}

# Standing down Nova's own intrusion response lowers the home's guard as much
# as a disarm. Always confirmed, and asked by voice only with a phone tap.
# "nova" names Nova's own action, not a Home Assistant service.
_SECURITY_STANDDOWN = {("nova", "dismiss_intrusion")}

# Specialists that act in the world (8.58.6): the Executive Assistant works
# in Gmail and Calendar, the Marketing agent posts for the business. They
# always need a yes first: a spoken one from a voice satellite, a phone tap
# from Chat. "nova" names Nova's own action, not a Home Assistant service.
_SPECIALIST_ACTIONS = {
    ("nova", "ask_executive_assistant"),
    ("nova", "ask_marketing_agent"),
}


def _guard_lowering(domain: str, service: str) -> bool:
    """Unlock, open, disarm or standing down: the actions that only a phone
    tap may allow when asked by voice or in Chat."""
    pair = (domain, service)
    return pair in _VOICE_BLOCKED_OPENING or pair in _SECURITY_STANDDOWN


def classify(domain: str, service: str, entity_id: str = "") -> Tuple[str, str]:
    """``(risk, reason)`` for one call; risk is low, medium, high or critical.

    Pure: no ``hass``, no I/O, so it is safe anywhere, logging included.
    Exact pairs come first; then any service on a lock or alarm that does
    something is rated up (a guard lowering word to high, anything else not
    known safe to medium); then anything that sets off a scene, script or
    automation is medium. Locking, arming and closing stay low.
    """
    dom, svc = domain or "", service or ""
    pair = (dom, svc)
    if pair in _CRITICAL:
        return "critical", f"{dom}.{svc} disarms security"
    if pair in _SECURITY_STANDDOWN:
        return "critical", f"{dom}.{svc} stands down an active security response"
    if pair in _SPECIALIST_ACTIONS:
        return "medium", f"{dom}.{svc} can send, post or change things outside the home"
    if pair in _HIGH:
        return "high", f"{dom}.{svc} unlocks a door"
    if pair in _MEDIUM:
        return "medium", f"{dom}.{svc} opens a physical barrier"

    lowered = svc.lower()
    if dom in _SECURITY_DOMAINS and lowered not in _READ_ONLY:
        if any(word in lowered for word in _OPENING):
            return "high", f"{dom}.{svc} opens a security device"
        if lowered not in _SECURITY_SAFE:
            return "medium", f"unrecognized {dom} action on a security device"
    if (dom in _INDIRECTION_DOMAINS and lowered not in _READ_ONLY
            and lowered not in _INDIRECTION_SAFE):
        return "medium", f"{dom}.{svc} activates a {dom} whose contents aren't visible to this gate"
    if dom == "switch" and svc in _SWITCH_OFF_SERVICES:
        target = (entity_id or "").lower()
        if any(hint in target for hint in _SECURITY_HINTS):
            return "high", "turning off a security switch"
    return "low", ""


# ── who is asking: context set for the length of one turn ───────────────────

# A Chat tab turn (8.31.0). Tools only pass a device_id and user_id, which
# cannot tell typed Chat from other typed requests, so the conversation marks
# the turn here. The gate then treats it like voice for guard lowering actions
# and standing down: always a phone tap. It only ever makes the gate
# stricter, follows the turn into its tasks and executor jobs, and is reset
# when the turn ends.
CHAT_ID_PREFIX = "nova_chat_"
_CHAT_USER: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "nova_chat_user", default=None)


def chat_conversation_id(user_id: str) -> str:
    """The conversation id of one user's Chat tab thread."""
    return f"{CHAT_ID_PREFIX}{user_id}"


@contextlib.contextmanager
def chat_turn(conversation_id: Optional[str], user_id: Optional[str]) -> Iterator[bool]:
    """Inside the block, a Chat conversation id marks the turn as Chat; yields
    whether it did. An empty user id still marks it, and the gate then finds
    no phone and refuses."""
    is_chat = str(conversation_id or "").startswith(CHAT_ID_PREFIX)
    token = _CHAT_USER.set(str(user_id or "")) if is_chat else None
    try:
        yield is_chat
    finally:
        if token is not None:
            _CHAT_USER.reset(token)


def _chat_user() -> Optional[str]:
    """The Chat turn's user ("" if it has none), or None outside Chat."""
    return _CHAT_USER.get()


# A spoken yes to a held request (8.33.0). The reply asked, the mic stayed
# open, and the user's yes on the same satellite replays the request inside
# this scope. Honoured only for that device and those exact targets, and
# only after the phone tap rules, so guard lowering actions still need the
# phone. Set by the conversation's answer handling for one replay.
_APPROVED: contextvars.ContextVar[Optional[Tuple[str, frozenset]]] = contextvars.ContextVar(
    "nova_approved_confirmation", default=None)


def _target_set(targets) -> frozenset:
    return frozenset((str(d), str(s), str(e or "")) for d, s, e in targets)


@contextlib.contextmanager
def approved_confirmation(device_id: str, targets) -> Iterator[None]:
    """Inside the block, these (domain, service, entity_id) targets asked from
    this device count as confirmed."""
    token = _APPROVED.set((str(device_id or ""), _target_set(targets)))
    try:
        yield
    finally:
        _APPROVED.reset(token)


def _approved(domain: str, service: str, entity_id: str, device_id: str) -> bool:
    held = _APPROVED.get()
    if not held or not device_id:
        return False
    device, targets = held
    return device == device_id and (domain, service, entity_id or "") in targets


# A voice phrase said word for word (8.34.0) is its own confirmation, on
# voice or Chat. Set by voice_phrase_runner.try_phrase for that one run,
# checked after the phone tap rules, and never for guard lowering, standing
# down or anything else high or critical.
_EXACT: contextvars.ContextVar[Optional[frozenset]] = contextvars.ContextVar(
    "nova_exact_phrase", default=None)


@contextlib.contextmanager
def exact_phrase_approval(targets) -> Iterator[None]:
    """Inside the block these (domain, service, entity_id) targets count as
    confirmed: the phrase that runs them was said exactly."""
    token = _EXACT.set(_target_set(targets))
    try:
        yield
    finally:
        _EXACT.reset(token)


def _exact_approved(domain: str, service: str, entity_id: str) -> bool:
    targets = _EXACT.get()
    if not targets or (domain, service, entity_id or "") not in targets:
        return False
    # Guard lowering keeps every question, whatever its rating (a garage door
    # opening is only medium).
    if _guard_lowering(domain, service):
        return False
    try:
        risk, _ = classify(domain, service, entity_id)
    except Exception:
        return False
    return risk not in ("high", "critical")


def _voice_satellite_request(hass, device_id: str) -> bool:
    """Whether the request came from a voice satellite. A lookup that fails
    counts as yes: an unknown origin fails closed."""
    if not device_id:
        return False
    try:
        from . import voice_confirm
        return bool(voice_confirm.is_voice_satellite_device(hass, device_id, strict=True))
    except Exception as exc:
        _LOGGER.warning("policy: voice-satellite check failed (%s); treating as voice "
                        "for safety", exc)
        return True


# ── confirming ──────────────────────────────────────────────────────────────

def requires_confirmation(hass, domain: str, service: str, entity_id: str = "",
                          device_id: str = "", *, voice: bool = False) -> bool:
    """Whether this call must be confirmed before it runs.

    Guard lowering from voice or Chat, and standing down, always must,
    whatever ``voice_confirm_enabled`` says, so a caller that cannot ask
    (bulk control) skips them. A phrase said exactly needs nothing more.
    Otherwise voice_confirm.action_is_protected decides (the opt in and the
    per entity list); if it cannot be asked, low risk passes and anything
    higher must be confirmed.
    """
    pair = (domain, service)
    if pair in _VOICE_BLOCKED_OPENING and (
            voice or _chat_user() is not None or _voice_satellite_request(hass, device_id)):
        return True
    if pair in _SECURITY_STANDDOWN or pair in _SPECIALIST_ACTIONS:
        return True
    if _exact_approved(domain, service, entity_id):
        return False
    try:
        from . import voice_confirm
        return bool(voice_confirm.action_is_protected(hass, domain, service, entity_id))
    except Exception as exc:
        failure = exc
    risk = classify(domain, service, entity_id)[0]
    if risk != "low":
        _LOGGER.warning("policy: could not consult voice_confirm for %s.%s (%s); "
                        "requiring confirmation for %s-risk action",
                        domain, service, failure, risk)
    return risk != "low"


def _blocked_reason(standdown: bool, domain: str) -> str:
    if standdown:
        return "stand down a security alert"
    if domain == "nova":
        return "send, post or change things for you"
    if domain == "alarm_control_panel":
        return "disarm the alarm"
    return "unlock or open this"


async def _confirm_from_chat(hass, label: str, ent: str, user_id: str,
                             standdown: bool, domain: str) -> Tuple[bool, str, str]:
    """Guard lowering asked in Chat: only a tap on that user's own phone
    allows it. No phone for the user refuses and says why; a refused,
    unanswered or failed tap denies it."""
    unavailable = (False, "phone confirmation unavailable, so this was not done", "error")
    try:
        from . import voice_confirm
        phones = voice_confirm.phone_services_for_user(hass, user_id)
    except Exception as exc:
        _LOGGER.warning("policy: chat phone lookup failed (%s); denying", exc)
        return unavailable
    what = f"{label} {ent}".strip()
    if not phones:
        return False, (f"{what} needs a tap on your phone to confirm, and no phone is "
                       "registered for your Home Assistant account, so it was not done"), "denied"
    question = (f"{what} was requested from Nova chat. Confirm on your phone to proceed. "
                f"Chat alone can't {_blocked_reason(standdown, domain)}.")
    try:
        result = await voice_confirm.confirm_via_phone_only_typed(hass, question, services=phones)
    except Exception as exc:
        _LOGGER.warning("policy: chat phone-confirm failed for %s (%s); denying", what, exc)
        return unavailable
    if result == "approved":
        return True, "", "approved"
    return False, (f"{what} was requested from chat. A request was sent to your phone and "
                   "was not approved, so it was not done"), result


async def _confirm_from_voice(hass, label: str, ent: str, standdown: bool,
                              domain: str, service: str) -> Tuple[bool, str, str]:
    """Guard lowering asked by voice: a phone tap only, never a spoken yes."""
    try:
        from . import voice_confirm
        question = (f"{label} {ent} was requested by voice — confirm on your phone "
                    f"to proceed. Voice alone can't {_blocked_reason(standdown, domain)}.").strip()
        result = await voice_confirm.confirm_via_phone_only_typed(hass, question)
    except Exception as exc:
        _LOGGER.warning("policy: voice-unlock phone-confirm failed for %s.%s (%s); denying",
                        domain, service, exc)
        return False, "phone confirmation unavailable — action denied for safety", "error"
    if result == "approved":
        return True, "", "approved"
    return False, (f"{label} {ent} was requested by voice; voice alone can't authorize "
                   "this, and phone confirmation wasn't received"), result


def _low_risk_passes(domain: str, service: str, entity_id: str, warning: str) -> bool:
    risk, _ = classify(domain, service, entity_id)
    return risk == "low" and not warning


async def confirm_gate(hass, domain: str, service: str, entity_id: str = "",
                       action_label: str = "", device_id: str = "", target_name: str = "",
                       *, voice: bool = False, warning: str = "") -> Tuple[bool, str, str]:
    """May this action go ahead now? ``(allowed, note, approval_result)``.

    ``approval_result`` is the exact typed outcome (voice_confirm's
    ConfirmResult), never something worked out from ``note``:

    * not_required: nothing to confirm, or the confirmation subsystem could
      not be asked but the action is low risk anyway;
    * approved: a real yes;
    * rejected: a real no;
    * expired: a bounded wait (the phone tap) ran out unanswered;
    * deferred: the voice path asked and the answer can only come as a
      later turn, not a timeout or a no;
    * error: something inside failed and the action was denied for safety.

    Only not_required and approved allow it: no failure in the subsystem can
    let a protected action through. ``target_name`` only words the question;
    the decision uses ``entity_id``. ``warning`` is what the live check
    found (an open door, open zones): the action is then always asked, and
    the question opens with it. Guard lowering from a voice satellite
    (``device_id``) always takes a phone tap.
    """
    label = (action_label or service.replace("_", " ")).strip()
    if target_name:
        ent = str(target_name).strip()
    else:
        ent = entity_id.split(".")[-1].replace("_", " ").strip() if entity_id else ""
    standdown = (domain, service) in _SECURITY_STANDDOWN
    specialist = (domain, service) in _SPECIALIST_ACTIONS
    if specialist and _chat_user() is not None:
        return await _confirm_from_chat(hass, label, ent, _chat_user() or "", False, domain)
    if _guard_lowering(domain, service):
        chat_user = _chat_user()
        if chat_user is not None:
            return await _confirm_from_chat(hass, label, ent, chat_user, standdown, domain)
        if voice or _voice_satellite_request(hass, device_id):
            return await _confirm_from_voice(hass, label, ent, standdown, domain, service)

    try:
        from . import voice_confirm
    except Exception as exc:
        if _low_risk_passes(domain, service, entity_id, warning):
            return True, "", "not_required"
        _LOGGER.warning("policy: voice_confirm import failed for %s.%s (%s); denying",
                        domain, service, exc)
        return False, "confirmation unavailable — action denied for safety", "error"

    try:
        protected = standdown or specialist or bool(warning) or bool(
            voice_confirm.action_is_protected(hass, domain, service, entity_id))
    except Exception as exc:
        if _low_risk_passes(domain, service, entity_id, warning):
            return True, "", "not_required"
        _LOGGER.warning("policy: protection check failed for %s.%s (%s); denying",
                        domain, service, exc)
        return False, "confirmation check failed — action denied for safety", "error"
    if not protected:
        return True, "", "not_required"

    # The user already said yes on this satellite for exactly this (8.33.0).
    if _approved(domain, service, entity_id, device_id):
        return True, "", "approved"

    # From a voice satellite, never ask separately (8.33.0): a question played
    # elsewhere, or ask_question on that satellite, would cancel the user's
    # running request. Nova's reply asks and the mic stays open for the answer
    # (pending_confirm.py).
    if _voice_satellite_request(hass, device_id):
        return False, (f"{warning} {label} {ent} needs a spoken yes first; "
                       "ask the user in your reply").strip(), "deferred"

    if warning:
        question = f"{warning} {label} {ent} anyway — are you sure?".strip()
    else:
        question = f"{label} {ent} — are you sure?".strip()
    try:
        result = await voice_confirm.confirm_typed(hass, question, entity_id=entity_id)
    except Exception as exc:
        _LOGGER.warning("policy: confirmation errored for %s.%s (%s); denying",
                        domain, service, exc)
        return False, "confirmation unavailable — action denied for safety", "error"
    if result == "approved":
        return True, "", "approved"
    return False, (f"asked for spoken confirmation before {label} "
                   f"on {ent or entity_id}; not yet confirmed"), result


# ── authorize: the one entry point (8.24.0) ─────────────────────────────────
# Every call that can change a lock, cover, alarm, scene or script goes
# through authorize(), or authorize_now() where nothing may be asked. A test
# scans the package for calls that skip it, and for anything outside this
# module calling confirm_gate() or requires_confirmation(): those are how
# authorize() confirms, not entry points.

SOURCE_VOICE = "voice"          # a voice satellite, or Nova's voice reply window
SOURCE_CHAT = "chat"            # the chat, the app or an automation
SOURCE_PANEL = "panel"          # the Nova panel
SOURCE_AUTOMATIC = "automatic"  # Nova acting on its own (lockdown, sweep, offers it trusts)
SOURCES = (SOURCE_VOICE, SOURCE_CHAT, SOURCE_PANEL, SOURCE_AUTOMATIC)


@dataclass(frozen=True)
class AuthorityRequest:
    """Who asked, from where, to do what, to which device."""
    domain: str
    service: str
    entity_id: str = ""
    source: str = ""            # one of SOURCES; "" works it out from device_id
    device_id: str = ""
    user_id: str = ""
    label: str = ""             # for the question a person hears or reads
    target_name: str = ""       # ditto; never part of the decision


@dataclass(frozen=True)
class AuthorityDecision:
    allowed: bool
    approval: str               # not_required / approved / rejected / expired / deferred / error / denied
    note: str = ""
    risk: str = "low"
    source: str = ""
    code: str = ""              # the live check's reason (safety_check.py), if it stopped this

    @property
    def already_secure(self) -> bool:
        """Nothing was sent: the device is already locked, closed or armed
        (8.46.0)."""
        from .safety_check import ALREADY_SECURE
        return self.code == ALREADY_SECURE


def _rated(request: AuthorityRequest) -> str:
    try:
        risk, _ = classify(request.domain, request.service, request.entity_id)
    except Exception:
        risk = "critical"
    return risk


def _live(hass, request: AuthorityRequest, source: str):
    """The live check (safety_check.py, 8.46.0) of a lock, cover or alarm
    command, or None where it does not apply."""
    from . import safety_check
    if not safety_check.applies(request.domain, request.service, request.entity_id):
        return None
    return safety_check.check(hass, request.domain, request.service, request.entity_id,
                              automatic=source == SOURCE_AUTOMATIC)


def _stopped(check, risk: str, source: str) -> AuthorityDecision:
    """The live check stopped the command before the policy was asked."""
    return AuthorityDecision(False, "not_required", check.note, risk, source, check.code)


def _automatic(req: AuthorityRequest, risk: str) -> AuthorityDecision:
    """Nova on its own may only do low risk things (lock, close, lights,
    heating), never unlock, open, disarm or run a scene or script."""
    if risk == "low":
        return AuthorityDecision(True, "not_required", "", risk, SOURCE_AUTOMATIC)
    return AuthorityDecision(False, "denied",
                             f"Nova never does {req.domain}.{req.service} on its own",
                             risk, SOURCE_AUTOMATIC)


def _kwargs(request: AuthorityRequest, *, with_target: bool) -> dict:
    """Only what the request carries, so the gate sees the same call it
    always did. ``voice`` only when the source says so; otherwise the gate
    works it out from device_id."""
    kw: dict = {}
    if request.device_id:
        kw["device_id"] = request.device_id
    if with_target and request.target_name:
        kw["target_name"] = request.target_name
    if request.source == SOURCE_VOICE:
        kw["voice"] = True
    return kw


def authorize_now(hass, request: AuthorityRequest, *, live: bool = True) -> AuthorityDecision:
    """The decision without asking anyone: allowed, or held ("deferred")
    because it would need a confirmation. For paths that cannot ask (bulk
    control, the local fast path). Fails closed above low risk.

    A lock, cover or alarm command is checked live first (8.46.0); ``live``
    False skips that, only for questions that send nothing (would a saved
    phrase need a confirmation?)."""
    risk = _rated(request)
    source = request.source or SOURCE_CHAT
    check = _live(hass, request, source) if live else None
    if check is not None and check.verdict != "go":
        if check.verdict == "ask":
            return AuthorityDecision(False, "deferred", check.note, risk, source, check.code)
        return _stopped(check, risk, source)
    if source == SOURCE_AUTOMATIC:
        return _automatic(request, risk)
    try:
        held = bool(requires_confirmation(
            hass, request.domain, request.service, request.entity_id,
            **_kwargs(request, with_target=False)))
    except Exception as exc:
        _LOGGER.warning("policy: authorize_now failed for %s.%s (%s)",
                        request.domain, request.service, exc)
        held = risk != "low"
    if held:
        return AuthorityDecision(False, "deferred", "needs confirmation", risk, source)
    return AuthorityDecision(True, "not_required", "", risk, source)


async def authorize(hass, request: AuthorityRequest) -> AuthorityDecision:
    """May this action run now? Asks when it must (a spoken yes, or a phone
    tap for guard lowering and standing down asked by voice). Fails closed:
    any error refuses anything above low risk.

    A lock, cover or alarm command is checked live first (8.46.0): an
    unreadable device is refused, one already secure gets nothing sent, and
    an open door or open zones are named in the question. After a yes it is
    checked again, since the answer can take a while."""
    risk = _rated(request)
    source = request.source or SOURCE_CHAT
    check = _live(hass, request, source)
    if check is not None and check.verdict not in ("go", "ask"):
        return _stopped(check, risk, source)
    if source == SOURCE_AUTOMATIC:
        return _automatic(request, risk)
    warning = check.note if check is not None and check.verdict == "ask" else ""
    try:
        kw = _kwargs(request, with_target=True)
        if warning:
            kw["warning"] = warning
        ok, note, approval = await confirm_gate(
            hass, request.domain, request.service, request.entity_id, request.label, **kw)
        if ok and check is not None and approval == "approved":
            from . import safety_check
            again = safety_check.after_yes(hass, request.domain, request.service,
                                           request.entity_id, check)
            if again.verdict != "go":
                return AuthorityDecision(False, approval, again.note, risk, source, again.code)
        code = "" if ok or not warning else check.code
        return AuthorityDecision(bool(ok), approval, note, risk, source, code)
    except Exception as exc:
        _LOGGER.warning("policy: authorize failed for %s.%s (%s)",
                        request.domain, request.service, exc)
        if risk == "low" and not warning:
            return AuthorityDecision(True, "not_required", "", risk, source)
        return AuthorityDecision(False, "error", "authorization failed — action denied for safety",
                                 risk, source)
