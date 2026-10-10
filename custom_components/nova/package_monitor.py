"""
Nova package & mail detection.

Watches porch / doorbell cameras for delivered packages and mail using a focused
vision classification, and tracks per-camera state so:

  • a package is announced ONCE when it arrives — not on every check while it sits
  • mail arrival is announced once
  • a package vanishing while nobody is home is flagged (possible porch theft)

Two things drive it: a low-frequency periodic check (so deliveries that don't
ring the bell are still caught) and the doorbell-press analysis (which already
describes the doorway — we reuse its text, no extra vision call). Both funnel
through the same state machine in `evaluate`.

Speech is limited to once per camera and kind every 30 minutes. A porch motion
sensor can start the periodic check early, and a mailbox sensor can report mail
through `note_from_mailbox`; neither adds a way to speak.

Vision and capture plumbing is reused from camera.py via lazy import to avoid a
circular dependency; the module itself is otherwise dependency-light.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Callable, Optional

_LOGGER = logging.getLogger(__name__)

# Per-camera state: entity_id -> {"package": bool, "mail": bool, "count": int,
#                                 "since": datetime, "desc": str}
_STATE: dict[str, dict] = {}

# Speech cooldown: (camera entity_id, kind) -> monotonic time of the last
# announcement actually SPOKEN for that pair. kind is delivered / mail /
# removed / stranded, so a "removed while away" alert is never held back by a
# recent "delivered". Only speech is gated by this; state, logs, observer and
# camera_semantic recording happen exactly as before. Kept next to _STATE and
# cleared wherever _STATE is cleared.
_LAST_SPOKEN: dict[tuple[str, str], float] = {}
_ANNOUNCE_COOLDOWN_S = 1800.0

# Debounce stamps (monotonic) for the sensor triggers: "early_check" for the
# porch motion sweep (one stamp for all sensors), "mailbox:<entity_id>" per
# mailbox sensor. Cleared with _STATE as well.
_TRIGGER_LAST: dict[str, float] = {}
_EARLY_KEY = "early_check"
_EARLY_CHECK_DELAY_S = 20.0
_EARLY_CHECK_MIN_GAP_S = 180.0
_MAILBOX_MIN_GAP_S = 300.0

_CARRIER_WORD = (
    r"(?:amazon|ups|fedex|usps|dhl|an\s+post|dpd|gls|evri|hermes|yodel|"
    r"parcelforce|royal\s+mail|courier)"
)
_PKG_KEYWORDS = re.compile(
    r"\b(?:package|parcel|box|delivery|delivered|" + _CARRIER_WORD
    + r"|carton|crate|cardboard)\b", re.I,
)
_MAIL_KEYWORDS = re.compile(
    r"\b(mail|letter|letters|envelope|envelopes|mailman|mail\s*carrier|"
    r"postal|postman|postie|post)\b", re.I,
)
# Negated mentions — "no package visible", "not carrying a delivery",
# "without any boxes", "no sign of packages or mail" — must not count as
# sightings. The doorbell analysis text frequently *rules out* deliveries in
# exactly these words, which raw keyword matching turned into false
# "package delivered" announcements. Strip negated spans (including
# or/and-connected chains) before keyword matching.
_DELIVERY_WORD = (
    r"(?:package|packages|parcel|parcels|box|boxes|delivery|deliveries|"
    r"mail|letter|letters|envelope|envelopes|mailman|mail\s*carrier|postal|"
    r"postman|postie|post|" + _CARRIER_WORD + r")\w*"
)
_NEGATION = re.compile(
    r"\b(?:no|not|without|isn'?t|aren'?t|doesn'?t|don'?t|nor|zero|none of|"
    r"no sign of|no signs of|nobody|no one)\b"
    r"(?:\s+\w+){0,3}?\s+"
    + _DELIVERY_WORD
    + r"(?:\s*(?:,|\bor\b|\band\b|\bnor\b)\s*" + _DELIVERY_WORD + r")*",
    re.I,
)

_PKG_PROMPT = (
    "You are inspecting a still frame from a doorway / front-porch security "
    "camera. Report ONLY whether a delivered PACKAGE or MAIL is visible right "
    "now.\n"
    "• package = a parcel, box, or delivery item sitting on the ground, step, "
    "porch, or by the door.\n"
    "• mail = letters or envelopes left at the door, or a mail carrier actively "
    "delivering.\n"
    "Ignore the street, passing vehicles, and people who are NOT leaving or "
    "carrying a delivery. If unsure, say false.\n"
    "Respond with ONLY a compact JSON object and no other text:\n"
    '{"package": true|false, "mail": true|false, "count": <number of packages>, '
    '"description": "<=12 words"}'
)


# ── Detection ────────────────────────────────────────────────────────────────

def _parse_detection(text: str) -> Optional[dict]:
    """Parse the model's JSON reply; tolerant of code fences / stray prose."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    pkg = bool(d.get("package"))
    try:
        count = int(d.get("count") or (1 if pkg else 0))
    except Exception:
        count = 1 if pkg else 0
    return {
        "package": pkg,
        "mail": bool(d.get("mail")),
        "count": max(count, 1) if pkg else 0,
        "description": str(d.get("description") or "")[:120],
    }


def detection_from_text(text: str) -> dict:
    """Keyword-based detection from an existing free-text analysis (e.g. the
    doorbell-press description). A cheap reuse — no extra vision call.
    Negated mentions ("no package visible") are stripped first, so text that
    rules a delivery OUT can never announce one (v6.46.0)."""
    cleaned = _NEGATION.sub(" ", text or "")
    has_pkg = bool(_PKG_KEYWORDS.search(cleaned))
    return {
        "package": has_pkg,
        "mail": bool(_MAIL_KEYWORDS.search(cleaned)),
        "count": 1 if has_pkg else 0,
        "description": "",
    }


async def detect_on_camera(hass, groq_client, entity_id: str) -> Optional[dict]:
    """Focused package/mail vision classification on a fresh frame."""
    from . import camera as cam  # lazy to avoid circular import

    img = await cam._get_best_image(hass, entity_id)
    if not img:
        return None
    # v6.46.0: backend-sourced images (Nest event media, Frigate snapshots)
    # bypass the blank check the standard-snapshot path applies. A black
    # wake-up frame or corrupt event thumbnail fed to the vision model is a
    # classic hallucinated-package source — classify nothing instead.
    try:
        if cam._looks_blank(img):
            _LOGGER.debug("Nova package: blank frame from %s — skipping", entity_id)
            return None
    except Exception:
        pass
    img = cam._downscale_jpeg(img)
    provider = cam._cfg_opt(hass, "vision_provider", "groq") or "groq"
    model = cam._cfg_opt(hass, "vision_model", cam.VISION_MODEL) or cam.VISION_MODEL
    b64 = base64.b64encode(img).decode()
    try:
        from .providers.activity import execute_chat
        # The same runtime-owned vision client camera.py uses.
        async with cam._camera_client(hass, provider, model, groq_client,
                                      binding="vision") as client:
            result = await execute_chat(
                hass, client,
                [
                    {"role": "system", "content": _PKG_PROMPT},
                    {"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": "Classify per the instructions. JSON only."},
                    ]},
                ],
                role="package", data_category="vision",
                max_tokens=120, temperature=0.7,
                model_override=model or None,
            )
        text = (result.text or "").strip()
    except Exception as exc:
        _LOGGER.debug("Nova package vision error on %s: %s", entity_id, exc)
        return None
    return _parse_detection(text) or detection_from_text(text)


# ── Gating helpers ───────────────────────────────────────────────────────────

def _runtime(hass, key, default):
    """Live runtime_config value (NovaRuntime), else `default`."""
    from .runtime import domain_runtime_config
    rc = domain_runtime_config(hass)
    return rc[key] if key in rc else default


def _announcements_on(hass) -> bool:
    v = _runtime(hass, "announcements_enabled", True)
    return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")


def _in_quiet_hours(hass) -> bool:
    try:
        from . import sleep_detection
        return sleep_detection._in_quiet_hours(
            str(_runtime(hass, "observer_quiet_start", "22:00")),
            str(_runtime(hass, "observer_quiet_end", "07:00")),
        )
    except Exception:
        return False


async def _push_phones(hass, message: str) -> None:
    """Push to every phone, through the path every other Nova alert uses.
    Never raises."""
    try:
        from . import cognitive_core as cc
        core = getattr(cc, "_CORE", None)
        await cc._notify_all_devices(hass, getattr(core, "config", None),
                                     message, "package_removed")
    except Exception as exc:
        _LOGGER.debug("package: phone push failed: %s", exc)


# ── Name matching ────────────────────────────────────────────────────────────
# Entity ids are matched on whole word tokens of the object part (the bit after
# the dot, split on "_" and on letter/digit changes), not on substrings. The old
# substring test on "front" swept wide views such as camera.front_yard.

# A token that marks a view of the front door / porch.
_PORCH_TOKENS = frozenset({
    "doorbell", "frontdoor", "porch", "front",
    # Glued compounds the old substring match caught and that are plainly the
    # same thing; kept so they keep matching.
    "frontporch", "frontdoorbell",
})
# A token that marks a wide view. It excludes a camera or sensor that would
# otherwise match on a porch token (camera.front_yard, camera.front_driveway).
_WIDE_TOKENS = frozenset({
    "yard", "backyard", "frontyard", "street", "road", "lawn", "garden",
    "driveway", "curb", "field", "garage", "pool", "patio", "deck", "gate",
    "parking", "lot",
})
_MAILBOX_TOKENS = frozenset({"mailbox", "letterbox", "postbox"})
_MOTION_DEVICE_CLASSES = frozenset({"motion", "occupancy", "presence"})
_MAILBOX_DEVICE_CLASSES = frozenset({"opening", "door", "occupancy"})


def _name_tokens(entity_id: str) -> frozenset[str]:
    obj = str(entity_id).split(".", 1)[-1].lower()
    return frozenset(re.findall(r"[a-z]+|\d+", obj))


def _is_porch_view(entity_id: str) -> bool:
    """True for a front door / porch / doorbell name that is not a wide view."""
    toks = _name_tokens(entity_id)
    return bool(toks & _PORCH_TOKENS) and not (toks & _WIDE_TOKENS)


def watched_cameras(hass, configured=None) -> list[str]:
    """Resolve which cameras to inspect for deliveries with the VISION sweep.

    A camera with native Eufy package sensors (delivered/stranded/taken) is
    excluded here — note_from_eufy() covers it directly off those sensors, at
    zero vision cost and more reliably than a photo guess, so re-checking it
    with the vision sweep too would just be a wasted, redundant LLM call.
    """
    if configured:
        if isinstance(configured, str):
            return [configured] if hass.states.get(configured) else []
        return [c for c in configured if hass.states.get(c)]
    out = []
    from .camera import active_camera_states
    from . import eufy
    for st in active_camera_states(hass):
        e = st.entity_id
        if _is_porch_view(e):
            if eufy.is_eufy_camera(hass, e) and "package_delivered" in eufy.discover_roles(hass, e):
                continue
            out.append(e)
    return out


# ── State machine + announcements ────────────────────────────────────────────

_PACKAGE_KIND_TO_STATE = {
    "delivered": "delivered",
    "mail": "mail",
    "removed": "taken",
    "stranded": "stranded",
}


def _now_mono() -> float:
    return time.monotonic()


def _cooldown_open(entity_id: str, kind: str) -> bool:
    """True when this (camera, kind) has not been spoken in the last 30 minutes."""
    last = _LAST_SPOKEN.get((entity_id, kind))
    return last is None or (_now_mono() - last) >= _ANNOUNCE_COOLDOWN_S


def _mark_spoken(entity_id: str, kind: str) -> None:
    _LAST_SPOKEN[(entity_id, kind)] = _now_mono()


def _log(hass, entity_id: str, kind: str, det: dict, source: str) -> None:
    try:
        from .websocket import nova_log
        nova_log("CAMERA", f"{entity_id} package-monitor [{source}]: {kind} "
                             f"(pkg={det.get('package')} mail={det.get('mail')} "
                             f"n={det.get('count')})")
    except Exception:
        pass
    try:
        from . import observer
        note = {
            "delivered": "A package was delivered",
            "mail": "Mail arrived",
            "removed": "A package was removed",
            "stranded": "A package hasn't been picked up yet",
        }.get(kind, kind)
        observer.record_camera_event(entity_id, note, "delivery",
                                     notable=(kind in ("delivered", "removed", "mail", "stranded")))
    except Exception:
        pass
    # Semantic learning (Phase 4, v7.109.0): the SAME single choke point
    # every real package-state transition already flows through — fires
    # once per transition, additively, never in place of the announcement/
    # notification/observer logic above. `source` here is package_monitor's
    # own "eufy"/"periodic"/"doorbell" — the latter two are Nova's own
    # vision-classification passes, recorded as "vision" for camera_semantic's
    # fixed source vocabulary.
    try:
        from . import camera_semantic
        pkg_state = _PACKAGE_KIND_TO_STATE.get(kind)
        if pkg_state and hass is not None:
            hass.async_create_task(camera_semantic.record_event(
                hass, label="package", camera_entity=entity_id,
                source="eufy" if source == "eufy" else "vision",
                package_state=pkg_state, detail=source,
            ))
    except Exception:
        pass


async def evaluate(hass, groq_client, honorific, tts_entity, speakers,
                   entity_id: str, det: dict, source: str = "periodic") -> bool:
    """Apply a detection result to per-camera state and announce transitions."""
    from .tts_helper import async_announce
    from . import alert_path

    prev = _STATE.get(entity_id, {"package": False, "mail": False, "count": 0})
    quiet = _in_quiet_hours(hass)
    announcements_on = _announcements_on(hass)
    # Whether each transition is spoken or pushed is decided in
    # alert_path.for_package (8.22.0); the 30 minute cooldowns stay here.
    sit = alert_path.situation(hass, quiet=quiet)
    loc = "the front door"
    spoke = False

    async def _say(msg):
        await async_announce(hass, msg, tts_entity, speakers, context="package")

    # honorific may be "" once nobody specific is home to address (see
    # honorific.py) — kept as a plain lead-in (not persona.lead_in(), which
    # titlecases the honorific and would change this file's existing,
    # untitled casing) with the sentence itself capitalized when it's
    # opening on its own.
    def _lead(sentence: str) -> str:
        if honorific:
            return f"{honorific}, {sentence}"
        return sentence[0].upper() + sentence[1:]

    # Package arrival
    if det.get("package") and not prev.get("package"):
        n = det.get("count", 1)
        msg = _lead(
              f"{n} packages have been delivered to {loc}."
              if n and n > 1 else
              f"a package has been delivered to {loc}.")
        _log(hass, entity_id, "delivered", det, source)
        plan = alert_path.for_package(sit, "delivered", announcements_on=announcements_on)
        if plan.alert and _cooldown_open(entity_id, "delivered"):
            await alert_path.deliver(plan, speak=lambda _t: _say(msg))
            _mark_spoken(entity_id, "delivered")
            spoke = True
    # Package removed
    elif prev.get("package") and not det.get("package"):
        _log(hass, entity_id, "removed", det, source)
        # Only while the residents are known to be away (unknown counts as
        # home); then pushed, quiet hours or not, and spoken when allowed.
        plan = alert_path.for_package(sit, "removed", announcements_on=announcements_on)
        if plan.alert and _cooldown_open(entity_id, "removed"):
            msg = _lead(f"a package was just removed from {loc} while no one is home.")
            out = await alert_path.deliver(
                plan, speak=lambda _t: _say(msg), push=lambda: _push_phones(hass, msg))
            spoke = out["spoke"]
            _mark_spoken(entity_id, "removed")

    # Mail arrival
    if det.get("mail") and not prev.get("mail"):
        _log(hass, entity_id, "mail", det, source)
        plan = alert_path.for_package(sit, "mail", announcements_on=announcements_on)
        if plan.alert and _cooldown_open(entity_id, "mail"):
            await alert_path.deliver(plan, speak=lambda _t: _say(_lead(f"mail has arrived at {loc}.")))
            _mark_spoken(entity_id, "mail")
            spoke = True

    _STATE[entity_id] = {
        "package": bool(det.get("package")),
        "mail": bool(det.get("mail")),
        "count": int(det.get("count", 0) or 0),
        "since": datetime.now(timezone.utc).replace(tzinfo=None),
        "desc": det.get("description", ""),
    }
    return spoke


def _confirm_transitions(prev: dict, det: dict, det2: Optional[dict]) -> dict:
    """A flag newly flipping True (would announce) must be confirmed by the
    second look; unconfirmed new positives are dropped for this cycle.
    Established state and negative transitions pass through untouched —
    pickups still register from a single frame."""
    out = dict(det)
    for flag in ("package", "mail"):
        if det.get(flag) and not prev.get(flag):
            if not (det2 and det2.get(flag)):
                out[flag] = False
                if flag == "package":
                    out["count"] = 0
    if out.get("package") and det2 and det2.get("package"):
        out["count"] = max(int(det.get("count") or 1), int(det2.get("count") or 1))
    return out


async def periodic_check(hass, groq_client, honorific, tts_entity, speakers,
                         configured_camera=None) -> dict:
    """Inspect each watched camera once. Skipped during quiet hours (nothing is
    announced then anyway, and it saves vision calls overnight)."""
    if _in_quiet_hours(hass):
        return {"skipped": "quiet_hours"}
    cams = watched_cameras(hass, configured_camera)
    checked = 0
    for entity_id in cams:
        det = await detect_on_camera(hass, groq_client, entity_id)
        if det is None:
            continue
        # v6.46.0: a NEW positive gets one immediate re-capture + re-classify
        # before it can announce. Two independent frames agreeing kills the
        # single-frame hallucination class outright, at the cost of one extra
        # vision call only when an announcement is on the line.
        prev = _STATE.get(entity_id, {"package": False, "mail": False})
        if (det.get("package") and not prev.get("package")) or \
           (det.get("mail") and not prev.get("mail")):
            det2 = await detect_on_camera(hass, groq_client, entity_id)
            det = _confirm_transitions(prev, det, det2)
        await evaluate(hass, groq_client, honorific, tts_entity, speakers,
                       entity_id, det, source="periodic")
        checked += 1
    return {"checked": checked, "cameras": cams}


async def note_from_doorbell(hass, groq_client, honorific, tts_entity, speakers,
                             entity_id: str, analysis_text: str) -> None:
    """Hook for the doorbell-press flow: derive package/mail from the press
    analysis text (no extra vision call) and run it through the state machine."""
    det = detection_from_text(analysis_text)
    if not (det["package"] or det["mail"]):
        # Nothing delivery-like seen; still record absence so a later pickup of a
        # previously-seen package can be detected — but only if we were tracking
        # this camera already.
        if entity_id not in _STATE:
            return
    await evaluate(hass, groq_client, honorific, tts_entity, speakers,
                   entity_id, det, source="doorbell")


async def note_from_eufy(hass, honorific, tts_entity, speakers,
                         entity_id: str, role: str) -> None:
    """
    Hook for Eufy's own native package sensors (delivered/stranded/taken) —
    zero vision cost, Eufy's dedicated model rather than an LLM guessing from
    a photo. `role` is one of "package_delivered" / "package_stranded" /
    "package_taken" (see eufy.py's role vocabulary).

    delivered/taken feed the SAME state machine (`evaluate`) the vision path
    uses, so behavior (announce once, "removed while away" phrasing, panel
    status) stays identical regardless of which source detected it. stranded
    isn't a presence toggle Eufy already flags "still sitting there, at risk"
    itself — it doesn't have a package/mail boolean to compare against, so
    it's a direct one-shot announcement instead of going through `evaluate`.
    """
    from .tts_helper import async_announce

    if role == "package_delivered":
        prev = _STATE.get(entity_id, {"package": False, "mail": False, "count": 0})
        det = {"package": True, "mail": prev.get("mail", False),
               "count": max(1, int(prev.get("count") or 0)), "description": "(Eufy) package delivered"}
        await evaluate(hass, None, honorific, tts_entity, speakers, entity_id, det, source="eufy")
        return

    if role == "package_taken":
        prev = _STATE.get(entity_id, {"package": False, "mail": False, "count": 0})
        det = {"package": False, "mail": prev.get("mail", False), "count": 0,
               "description": "(Eufy) package taken"}
        await evaluate(hass, None, honorific, tts_entity, speakers, entity_id, det, source="eufy")
        return

    if role == "package_stranded":
        from . import alert_path
        det = {"package": True, "mail": False, "count": 1}
        plan = alert_path.for_package(
            alert_path.situation(hass, quiet=_in_quiet_hours(hass)), "stranded",
            announcements_on=_announcements_on(hass))
        if not plan.alert:
            _log(hass, entity_id, "stranded", det, "eufy")
            return
        if not _cooldown_open(entity_id, "stranded"):
            # A repeat inside 30 minutes is logged and recorded, not spoken.
            _log(hass, entity_id, "stranded", det, "eufy")
            return
        msg = (f"{honorific}, a package at the front door hasn't been picked up yet."
               if honorific else "A package at the front door hasn't been picked up yet.")
        await alert_path.deliver(plan, speak=lambda _t: async_announce(
            hass, msg, tts_entity, speakers, context="package"))
        _mark_spoken(entity_id, "stranded")
        _log(hass, entity_id, "stranded", det, "eufy")


# ── Early checks from sensors ───────────────────────────────────────────────
# Both triggers only bring a check forward. They add no announcement path: the
# motion trigger runs the existing periodic_check (second look and state machine
# unchanged) and the mailbox trigger runs evaluate(). Speech still goes through
# evaluate()'s own gates, including the 30 minute cooldown.

def _package_detection_on(hass) -> bool:
    v = _runtime(hass, "package_detection", True)
    return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")


def _eufy_covered_entities(hass) -> set[str]:
    """Entity ids on a Eufy camera whose package roles note_from_eufy already
    handles (the same cameras watched_cameras() leaves out of the vision sweep).
    Never raises."""
    covered: set[str] = set()
    try:
        from . import eufy
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(hass)
        for cam, roles in eufy.all_camera_roles(hass).items():
            if "package_delivered" not in roles:
                continue
            covered.update(roles.values())
            entry = reg.async_get(cam)
            dev = getattr(entry, "device_id", None)
            if dev:
                covered.update(e.entity_id for e in reg.entities.values()
                               if e.device_id == dev)
    except Exception as exc:
        _LOGGER.debug("Nova package: eufy coverage lookup failed: %s", exc)
    return covered


def discover_trigger_sensors(hass) -> dict[str, str]:
    """entity_id -> "motion" | "mailbox" for the binary sensors that may bring a
    package check forward. Resolved once at setup, like the Eufy role map, so a
    sensor added later needs a Nova reload."""
    out: dict[str, str] = {}
    try:
        states = hass.states.async_all("binary_sensor")
    except Exception:
        return out
    covered: Optional[set[str]] = None
    for st in states:
        e = st.entity_id
        dc = str((getattr(st, "attributes", None) or {}).get("device_class") or "").lower()
        if (_name_tokens(e) & _MAILBOX_TOKENS) and (dc == "" or dc in _MAILBOX_DEVICE_CLASSES):
            out[e] = "mailbox"
            continue
        if dc in _MOTION_DEVICE_CLASSES and _is_porch_view(e):
            if covered is None:
                covered = _eufy_covered_entities(hass)
            if e not in covered:
                out[e] = "motion"
    return out


async def note_from_motion(hass, groq_client, ctx: Callable[[], tuple],
                           entity_id: str) -> Optional[dict]:
    """A porch or doorbell motion sensor turned on: wait 20 seconds, then run
    periodic_check once for the watched cameras. At most one early check every
    3 minutes in total. `ctx()` returns (honorific, tts_entity, speakers) and is
    called after the wait, so presence is current when the check runs."""
    if not (_package_detection_on(hass) and _announcements_on(hass)) or _in_quiet_hours(hass):
        return None
    now = _now_mono()
    last = _TRIGGER_LAST.get(_EARLY_KEY)
    if last is not None and now - last < _EARLY_CHECK_MIN_GAP_S:
        return None
    _TRIGGER_LAST[_EARLY_KEY] = now
    await asyncio.sleep(_EARLY_CHECK_DELAY_S)
    if not _package_detection_on(hass):
        return None
    honorific, tts_entity, speakers = ctx()
    return await periodic_check(hass, groq_client, honorific, tts_entity, speakers,
                                configured_camera=None)


def _mail_still_current(prev: dict) -> bool:
    """mail is True in the tracked state and was set within the cooldown. An
    older True is left over from an earlier delivery; a mailbox sensor has no
    camera sweep to clear it."""
    if not prev.get("mail"):
        return False
    since = prev.get("since")
    if not isinstance(since, datetime):
        return True
    age = (datetime.now(timezone.utc).replace(tzinfo=None) - since).total_seconds()
    return age < _ANNOUNCE_COOLDOWN_S


async def note_from_mailbox(hass, groq_client, ctx: Callable[[], tuple],
                            entity_id: str) -> bool:
    """A mailbox sensor turned on. Runs evaluate() with source "mailbox" and
    mail True, package unchanged from the current state. Ignored when mail is
    already True for this sensor, and at most once every 5 minutes per sensor.
    Returns whether anything was spoken."""
    if not _package_detection_on(hass):
        return False
    key = f"mailbox:{entity_id}"
    now = _now_mono()
    last = _TRIGGER_LAST.get(key)
    if last is not None and now - last < _MAILBOX_MIN_GAP_S:
        return False
    _TRIGGER_LAST[key] = now
    prev = _STATE.get(entity_id, {"package": False, "mail": False, "count": 0})
    if _mail_still_current(prev):
        return False
    if prev.get("mail"):
        # Stale True from an earlier delivery: start a new arrival.
        prev = dict(prev, mail=False)
        _STATE[entity_id] = prev
    honorific, tts_entity, speakers = ctx()
    det = {"package": bool(prev.get("package")), "mail": True,
           "count": int(prev.get("count") or 0), "description": "(mailbox sensor) mail"}
    return await evaluate(hass, groq_client, honorific, tts_entity, speakers,
                          entity_id, det, source="mailbox")


def status() -> dict:
    """Current tracked package/mail state, for panel/diagnostics."""
    return {
        eid: {
            "package": s.get("package"),
            "mail": s.get("mail"),
            "count": s.get("count"),
            "desc": s.get("desc", ""),
        }
        for eid, s in _STATE.items()
    }
