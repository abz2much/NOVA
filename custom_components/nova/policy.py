"""Action authorization policy — the single gate every actuation routes through.

Before Nova calls a Home Assistant service that changes state, the request
passes through here. Two questions are answered:

  * how risky is the action (low / medium / high / critical), and
  * may it proceed right now, or must it be confirmed first?

Confirmation itself is delegated to :mod:`voice_confirm` (unchanged mechanism
and unchanged opt-in: it only ever asks when ``voice_confirm_enabled`` is on).
The guarantee this layer adds is **fail-closed for authority**: if a protected
action needs confirmation and the confirmation path errors, the action is
DENIED rather than allowed through. Convenience actions (lights, climate,
media) are classified LOW and never gain friction.

This also closes a gap where ``bulk_control`` and ``execute_plan`` could
actuate locks / covers / alarms with no confirmation at all — they now route
through the same gate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Tuple

_LOGGER = logging.getLogger(__name__)

# Precise, high-signal (domain, service) pairs. The capability net below catches
# novel/unenumerated services so a new one can't default to LOW just because it
# isn't listed here.
_CRITICAL = {
    ("alarm_control_panel", "alarm_disarm"),
}
_HIGH = {
    ("lock", "unlock"),
}
_MEDIUM = {
    ("cover", "open_cover"),                # garage doors are covers
    ("cover", "open"),
    ("alarm_control_panel", "alarm_arm_away"),
    ("alarm_control_panel", "alarm_arm_home"),
    ("alarm_control_panel", "alarm_arm_night"),
    ("lock", "open"),                       # some locks support latch/open
}

# Domains that are security-relevant by their very nature: an actuating service
# on them is escalated even when we haven't enumerated it, so a future service
# (e.g. lock.unlatch) can't slip through as LOW convenience.
_SECURITY_DOMAINS = {"lock", "alarm_control_panel"}

# Domains whose *contents* are opaque to this gate: a scene/script/automation
# can itself unlock a door, disarm the alarm, or open a cover, and there is no
# cheap way to look inside one before it runs. This has to be domain-level,
# not a (domain, service) tuple — HA auto-registers a dynamic per-object
# service for every script (script.<object_id>) as an alternative to
# script.turn_on, and enumerating tuples here would repeat the exact
# cover.open_cover naming-gap bug this module was already patched for.
_INDIRECTION_DOMAINS = {"scene", "script", "automation"}
# Non-actuating / stopping calls on those domains carry no indirection risk —
# they can't newly trigger whatever the scene/script/automation contains.
_INDIRECTION_SAFE = {"reload", "turn_off", "stop"}
# Actuating verbs (as substrings) that DROP a guard → the high end.
_OPENING = ("unlock", "disarm", "open", "unlatch", "unbolt")
# Known-safe actuating services on a security domain (raising a guard) stay
# frictionless — locking/closing is safe; arming is already MEDIUM above.
_SECURITY_SAFE = {"lock", "close", "close_cover"}
# Non-actuating services carry no risk regardless of entity.
_READ_ONLY = {"", "update", "reload"}

# Substrings that make an otherwise-neutral switch security-relevant.
_SECURITY_HINTS = ("alarm", "security", "camera", "lock", "garage", "gate", "door")

# Actions that grant physical access and must never be authorized by voice
# alone (v7.87.0): a spoken "unlock the front door" could be a deepfake of
# the owner's voice. Independent of voice_confirm_enabled — this always
# applies when the request came from a voice satellite, and always requires
# a PHONE tap, never a spoken confirmation (which would repeat the exact
# weakness this exists to close). Locking/closing carries no such risk and
# stays frictionless, matching _SECURITY_SAFE above. Known gap: a scene or
# script that itself unlocks a door bypasses this — its contents are opaque
# to this gate (see _INDIRECTION_DOMAINS); not solved here.
_VOICE_BLOCKED_OPENING = {
    ("lock", "unlock"),
    ("cover", "open"),
    ("cover", "open_cover"),
    # Disarming by voice needs a phone tap too (8.24.0): a voice that can be
    # imitated must not be able to switch the alarm off on its own.
    ("alarm_control_panel", "alarm_disarm"),
}


def _voice_satellite_request(hass, device_id: str) -> bool:
    if not device_id:
        return False
    try:
        from . import voice_confirm
        # strict: a lookup that can't be completed raises (below) rather than
        # reading as "not a satellite", so an unknown origin fails closed.
        return bool(voice_confirm.is_voice_satellite_device(hass, device_id, strict=True))
    except Exception as exc:
        _LOGGER.warning("policy: voice-satellite check failed (%s); treating as voice "
                        "for safety", exc)
        return True  # fail closed: an unknown answer is treated as "could be voice"


# Standing down Nova's own intrusion response lowers the home's security
# posture as surely as disarming the alarm. It is always confirmed —
# whatever voice_confirm_enabled says — and, when asked for by voice, only by
# a phone tap, exactly like _VOICE_BLOCKED_OPENING. (domain "nova" names
# Nova's own action; it is not a Home Assistant service.)
_SECURITY_STANDDOWN = {
    ("nova", "dismiss_intrusion"),
}


def classify(domain: str, service: str, entity_id: str = "") -> Tuple[str, str]:
    """Return ``(risk, reason)`` for an action.

    Pure — takes no ``hass`` and performs no I/O, so it is safe to call from
    anywhere (including logging / telemetry paths). ``risk`` is one of
    ``"low"``, ``"medium"``, ``"high"``, ``"critical"``.

    Classification is capability-based rather than a bare service allowlist:
    known high-signal pairs match exactly, then any actuating service on an
    inherently security domain is escalated — a guard-dropping verb (unlock,
    disarm, open, unlatch) to HIGH, any other unrecognized actuating service to
    MEDIUM — so a service that isn't enumerated here cannot silently be LOW on a
    lock or alarm. Safe directions (lock, arm, close) keep their low friction.
    """
    dom = domain or ""
    svc = service or ""
    key = (dom, svc)
    hay = (entity_id or "").lower()

    if key in _CRITICAL:
        return "critical", f"{dom}.{svc} disarms security"
    if key in _SECURITY_STANDDOWN:
        return "critical", f"{dom}.{svc} stands down an active security response"
    if key in _HIGH:
        return "high", f"{dom}.{svc} unlocks a door"
    if key in _MEDIUM:
        return "medium", f"{dom}.{svc} opens a physical barrier"

    svc_l = svc.lower()
    # Capability net for inherently security domains (lock, alarm).
    if dom in _SECURITY_DOMAINS and svc_l not in _READ_ONLY:
        if any(w in svc_l for w in _OPENING):
            return "high", f"{dom}.{svc} opens a security device"
        if svc_l not in _SECURITY_SAFE:
            return "medium", f"unrecognized {dom} action on a security device"

    # Capability net for indirection domains (scene/script/automation) — any
    # actuating call, by any service name, including a script's own dynamic
    # per-object service, not just turn_on.
    if dom in _INDIRECTION_DOMAINS and svc_l not in _READ_ONLY and svc_l not in _INDIRECTION_SAFE:
        return "medium", f"{dom}.{svc} activates a {dom} whose contents aren't visible to this gate"

    if key == ("switch", "turn_off"):
        if any(t in hay for t in _SECURITY_HINTS):
            return "high", "turning off a security switch"
    return "low", ""


def requires_confirmation(hass, domain: str, service: str, entity_id: str = "",
                          device_id: str = "", *, voice: bool = False) -> bool:
    """Whether this action must be confirmed before it runs.

    Delegates to ``voice_confirm.action_is_protected`` (which honours the
    ``voice_confirm_enabled`` opt-in and the per-entity override list). If the
    confirmation module can't be consulted, fail closed for anything above LOW
    risk and allow LOW-risk convenience through.

    Independent of that opt-in: a voice-blocked-opening action (v7.87.0)
    requested from a voice satellite always needs confirmation, so a caller
    like bulk_control (which can't meaningfully confirm per-device and just
    skips protected actions) skips it too — not just when the general
    confirmation toggle happens to be on.
    """
    if (domain, service) in _VOICE_BLOCKED_OPENING and (
            voice or _voice_satellite_request(hass, device_id)):
        return True
    if (domain, service) in _SECURITY_STANDDOWN:
        return True
    try:
        from . import voice_confirm
        return bool(voice_confirm.action_is_protected(hass, domain, service, entity_id))
    except Exception as exc:  # module unavailable / errored
        risk, _ = classify(domain, service, entity_id)
        if risk == "low":
            return False
        _LOGGER.warning(
            "policy: could not consult voice_confirm for %s.%s (%s); "
            "requiring confirmation for %s-risk action", domain, service, exc, risk)
        return True


async def confirm_gate(
    hass,
    domain: str,
    service: str,
    entity_id: str = "",
    action_label: str = "",
    device_id: str = "",
    target_name: str = "",
    *,
    voice: bool = False,
) -> Tuple[bool, str, str]:
    """May this action proceed now? Returns ``(allowed, note, approval_result)``.

    ``approval_result`` is the EXACT typed outcome (see voice_confirm.py's
    ConfirmResult) — never a generic reason_code, never something a caller
    derives by parsing ``note``'s text. One of:
      "not_required" — not a protected action, or the confirmation subsystem
                        couldn't be consulted at all but the action's own
                        risk is low enough to proceed anyway.
      "approved"     — a real yes was captured.
      "rejected"     — a real, explicit no was captured.
      "expired"      — a genuine bounded wait (phone-notification timeout)
                        ran out with no answer.
      "deferred"     — the gated voice path asked but cannot capture a
                        synchronous answer by design; not a timeout, not a
                        decline.
      "error"        — an internal failure (import, protection-check, or
                        the confirmation call itself raised) denied the
                        action for safety, unrelated to what the user said.

    ``allowed`` is False for every value except "not_required"/"approved" —
    the fail-closed guarantee: an error anywhere in the confirmation
    subsystem can never let a protected action through.

    ``target_name`` is only for the question a person hears or reads; it
    never takes part in the decision.

    ``device_id`` (v7.87.0): when given and it's a voice satellite, an
    unlock/open action is ALWAYS gated behind a phone tap — never a spoken
    confirmation — regardless of ``voice_confirm_enabled``. See
    _VOICE_BLOCKED_OPENING above for why.
    """
    label = (action_label or service.replace("_", " ")).strip()
    # The question names the entity the way a person knows it (the caller's
    # friendly name); the decision itself only ever uses entity_id.
    ent = (str(target_name).strip() if target_name else
           (entity_id.split(".")[-1].replace("_", " ").strip() if entity_id else ""))
    standdown = (domain, service) in _SECURITY_STANDDOWN
    if (((domain, service) in _VOICE_BLOCKED_OPENING or standdown)
            and (voice or _voice_satellite_request(hass, device_id))):
        try:
            from . import voice_confirm
            if standdown:
                what = "stand down a security alert"
            elif domain == "alarm_control_panel":
                what = "disarm the alarm"
            else:
                what = "unlock or open this"
            question = (f"{label} {ent} was requested by voice — confirm on your phone "
                        f"to proceed. Voice alone can't {what}.").strip()
            result = await voice_confirm.confirm_via_phone_only_typed(hass, question)
        except Exception as exc:
            _LOGGER.warning("policy: voice-unlock phone-confirm failed for %s.%s (%s); denying",
                            domain, service, exc)
            return False, "phone confirmation unavailable — action denied for safety", "error"
        if result == "approved":
            return True, "", "approved"
        return False, (f"{label} {ent} was requested by voice; voice alone can't authorize "
                       f"this, and phone confirmation wasn't received"), result

    # Resolve the confirmation module. If it's missing, LOW-risk proceeds and
    # anything higher is denied (fail closed for authority).
    try:
        from . import voice_confirm
    except Exception as exc:
        risk, _ = classify(domain, service, entity_id)
        if risk == "low":
            return True, "", "not_required"
        _LOGGER.warning("policy: voice_confirm import failed for %s.%s (%s); denying",
                        domain, service, exc)
        return False, "confirmation unavailable — action denied for safety", "error"

    # Does this action need confirmation at all?
    try:
        protected = standdown or bool(
            voice_confirm.action_is_protected(hass, domain, service, entity_id))
    except Exception as exc:
        risk, _ = classify(domain, service, entity_id)
        if risk == "low":
            return True, "", "not_required"
        _LOGGER.warning("policy: protection check failed for %s.%s (%s); denying",
                        domain, service, exc)
        return False, "confirmation check failed — action denied for safety", "error"

    if not protected:
        return True, "", "not_required"

    # Confirmation required — ask. ANY failure here denies (fail closed).
    question = (f"{label} {ent} — are you sure?").strip()
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


# ── authorize: the one authority check (8.24.0) ─────────────────────────────
# Every call that can change a lock, cover, alarm, scene or script goes
# through authorize() (or authorize_now() where nothing may be asked). A test
# scans the package and fails if one does not, and fails if anything outside
# this module calls confirm_gate() or requires_confirmation() directly: those
# two are the confirmation mechanism authorize() uses, not entry points.

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


def _automatic(req: AuthorityRequest, risk: str) -> AuthorityDecision:
    """Nova acting on its own may only do low risk things: lock, close,
    lights, heating. Never unlock, open, disarm or run a scene or script."""
    if risk == "low":
        return AuthorityDecision(True, "not_required", "", risk, SOURCE_AUTOMATIC)
    return AuthorityDecision(
        False, "denied",
        f"Nova never does {req.domain}.{req.service} on its own", risk, SOURCE_AUTOMATIC)


def _kwargs(request: AuthorityRequest, *, with_target: bool) -> dict:
    """Only what was given, so the gate sees exactly the call it always did.
    voice is passed only when the source says so; otherwise the gate works
    it out from device_id, as before."""
    kw: dict = {}
    if request.device_id:
        kw["device_id"] = request.device_id
    if with_target and request.target_name:
        kw["target_name"] = request.target_name
    if request.source == SOURCE_VOICE:
        kw["voice"] = True
    return kw


def authorize_now(hass, request: AuthorityRequest) -> AuthorityDecision:
    """The decision without asking anyone: allowed, or held because it would
    need a confirmation (approval "deferred"). For paths that cannot ask
    (bulk control, the local fast path). Fails closed above low risk."""
    try:
        risk, _ = classify(request.domain, request.service, request.entity_id)
    except Exception:
        risk = "critical"
    source = request.source or SOURCE_CHAT
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
    """May this action run now? Asks for confirmation when it must (a
    spoken yes, or a phone tap for unlock, open, disarm and standing down an
    intrusion when asked by voice). Fails closed: any error denies anything
    above low risk."""
    try:
        risk, _ = classify(request.domain, request.service, request.entity_id)
    except Exception:
        risk = "critical"
    source = request.source or SOURCE_CHAT
    if source == SOURCE_AUTOMATIC:
        return _automatic(request, risk)
    try:
        ok, note, approval = await confirm_gate(
            hass, request.domain, request.service, request.entity_id,
            request.label, **_kwargs(request, with_target=True))
        return AuthorityDecision(bool(ok), approval, note, risk, source)
    except Exception as exc:
        _LOGGER.warning("policy: authorize failed for %s.%s (%s)",
                        request.domain, request.service, exc)
        if risk == "low":
            return AuthorityDecision(True, "not_required", "", risk, source)
        return AuthorityDecision(False, "error",
                                 "authorization failed — action denied for safety",
                                 risk, source)
