"""Package and mail detection at the front door.

Porch and doorbell cameras are checked for parcels and post with a focused
vision question, and each camera's state is tracked so that:

* a package is announced once when it arrives, not on every later check;
* mail is announced once when it arrives;
* a package disappearing while nobody is home is flagged as possible theft.

Sightings come from a slow periodic sweep (so a courier who never rings is
still noticed), from the text of the doorbell press analysis (no extra vision
call), from Eufy's own package sensors, and optionally from a mailbox
sensor. A porch motion sensor can bring the sweep forward. Everything goes
through ``evaluate``, and speech for a camera and kind is limited to once
every 30 minutes.

camera.py is imported lazily for frames and the vision client, which keeps
this module free of a circular import.
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

# camera entity_id -> {"package", "mail", "count", "since", "desc"}
_STATE: dict[str, dict] = {}

# (camera, kind) -> monotonic time that kind was last spoken for that camera.
# Kinds are delivered, mail, removed and stranded, so a recent "delivered"
# never holds back a "removed while away". Only speech is limited; state,
# logs, the observer and camera_semantic always record. Cleared with _STATE.
_LAST_SPOKEN: dict[tuple[str, str], float] = {}
_ANNOUNCE_COOLDOWN_S = 1800.0

# Monotonic stamps for the sensor triggers: one shared "early_check" stamp
# for porch motion, and "mailbox:<entity_id>" per mailbox sensor.
_TRIGGER_LAST: dict[str, float] = {}
_EARLY_KEY = "early_check"
_EARLY_CHECK_DELAY_S = 20.0
_EARLY_CHECK_MIN_GAP_S = 180.0
_MAILBOX_MIN_GAP_S = 300.0


def _either(*words: str) -> str:
    return "|".join(words)


_CARRIER_WORD = "(?:" + _either(
    "amazon", "ups", "fedex", "usps", "dhl", r"an\s+post", "dpd", "gls", "evri",
    "hermes", "yodel", "parcelforce", r"royal\s+mail", "courier",
) + ")"
_PKG_KEYWORDS = re.compile(
    r"\b(?:" + _either("package", "parcel", "box", "delivery", "delivered", _CARRIER_WORD,
                       "carton", "crate", "cardboard") + r")\b",
    re.I,
)
_MAIL_WORDS = ("mail", "letter", "letters", "envelope", "envelopes", "mailman",
               r"mail\s*carrier", "postal", "postman", "postie", "post")
_MAIL_KEYWORDS = re.compile(r"\b(" + _either(*_MAIL_WORDS) + r")\b", re.I)

# Negated mentions ("no package visible", "not carrying a delivery", "no sign
# of packages or mail") are cut out before matching. The doorbell analysis
# often rules a delivery out in exactly these words, and plain keyword
# matching used to turn that into a false "package delivered". Chains joined
# by commas, or, and, nor are cut too.
_DELIVERY_WORD = "(?:" + _either(
    "package", "packages", "parcel", "parcels", "box", "boxes", "delivery",
    "deliveries", *_MAIL_WORDS, _CARRIER_WORD,
) + r")\w*"
_NEGATOR = r"\b(?:" + _either(
    "no", "not", "without", "isn'?t", "aren'?t", "doesn'?t", "don'?t", "nor", "zero",
    "none of", "no sign of", "no signs of", "nobody", "no one",
) + r")\b"
_JOINER = r"\s*(?:,|\bor\b|\band\b|\bnor\b)\s*"
_NEGATION = re.compile(
    _NEGATOR + r"(?:\s+\w+){0,3}?\s+" + _DELIVERY_WORD
    + "(?:" + _JOINER + _DELIVERY_WORD + ")*",
    re.I,
)

_PKG_PROMPT = (
    "You are inspecting a still frame from a doorway / front-porch security camera. "
    "Report ONLY whether a delivered PACKAGE or MAIL is visible right now.\n"
    "• package = a parcel, box, or delivery item sitting on the ground, step, porch, "
    "or by the door.\n"
    "• mail = letters or envelopes left at the door, or a mail carrier actively delivering.\n"
    "Ignore the street, passing vehicles, and people who are NOT leaving or carrying "
    "a delivery. If unsure, say false.\n"
    "Respond with ONLY a compact JSON object and no other text:\n"
    '{"package": true|false, "mail": true|false, '
    '"count": <number of packages>, "description": "<=12 words"}'
)

# "package": true / 'mail': False and the like, for a reply that is JSON
# shaped but not valid JSON (Python booleans, a cut off object).
_FLAG_IN_REPLY = re.compile(r"""["']?\b(package|mail)\b["']?\s*:\s*(true|false)\b""", re.I)


# Detection

def _detection(package: bool, mail: bool, count: int, description: str = "") -> dict:
    return {"package": package, "mail": mail,
            "count": max(count, 1) if package else 0, "description": description}


def _parse_detection(text: str) -> Optional[dict]:
    """The model's JSON answer, tolerating code fences and stray prose."""
    found = re.search(r"\{.*\}", text or "", re.S)
    if found is None:
        return None
    try:
        data = json.loads(found.group(0))
    except Exception:
        return None
    package = bool(data.get("package"))
    try:
        count = int(data.get("count") or (1 if package else 0))
    except Exception:
        count = 1 if package else 0
    return _detection(package, bool(data.get("mail")), count,
                      str(data.get("description") or "")[:120])


def _flags_from_reply(text: str) -> Optional[dict]:
    """The true or false values of a JSON like reply that json cannot read.
    None when the reply holds neither flag: field names alone are never a
    sighting."""
    if "{" not in (text or ""):
        return None
    flags = {key.lower(): value.lower() == "true"
             for key, value in _FLAG_IN_REPLY.findall(text)}
    if not flags:
        return None
    package = flags.get("package", False)
    return _detection(package, flags.get("mail", False), 1 if package else 0)


def detection_from_text(text: str) -> dict:
    """Package and mail from free text such as the doorbell description, by
    keywords, with no vision call. Negated mentions are removed first, so
    text that rules a delivery out never reports one."""
    kept = _NEGATION.sub(" ", text or "")
    package = _PKG_KEYWORDS.search(kept) is not None
    return {"package": package, "mail": _MAIL_KEYWORDS.search(kept) is not None,
            "count": 1 if package else 0, "description": ""}


async def detect_on_camera(hass, groq_client, entity_id: str) -> Optional[dict]:
    """Ask the vision model about one fresh frame. None when there is no
    usable frame or no readable answer."""
    from . import camera as cam  # lazy: camera imports this module

    frame = await cam._get_best_image(hass, entity_id)
    if not frame:
        return None
    # Event media (Nest, Frigate) skips the blank check the normal snapshot
    # path has. A black wake up frame or a broken thumbnail is a classic
    # source of imagined parcels, so it is not classified at all.
    try:
        if cam._looks_blank(frame):
            _LOGGER.debug("Nova package: blank frame from %s — skipping", entity_id)
            return None
    except Exception:
        pass
    frame = cam._downscale_jpeg(frame)
    provider = cam._cfg_opt(hass, "vision_provider", "groq") or "groq"
    model = cam._cfg_opt(hass, "vision_model", cam.VISION_MODEL) or cam.VISION_MODEL
    image_url = "data:image/jpeg;base64," + base64.b64encode(frame).decode()
    question = [
        {"role": "system", "content": _PKG_PROMPT},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": image_url}},
            {"type": "text", "text": "Classify per the instructions. JSON only."},
        ]},
    ]
    try:
        from .providers.activity import execute_chat
        # The vision client camera.py itself uses, owned by the runtime.
        async with cam._camera_client(hass, provider, model, groq_client,
                                      binding="vision") as client:
            # A yes or no check: temperature 0 so one frame gets one answer.
            reply = await execute_chat(
                hass, client, question, role="package", data_category="vision",
                max_tokens=120, temperature=0, model_override=model or None,
            )
        text = (reply.text or "").strip()
    except Exception as exc:
        _LOGGER.debug("Nova package vision error on %s: %s", entity_id, exc)
        return None
    return _parse_detection(text) or _flags_from_reply(text)


# Settings and delivery

def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


def _runtime(hass, key, default):
    """The live runtime_config value, or ``default`` when it is not set."""
    from .runtime import domain_runtime_config
    config = domain_runtime_config(hass)
    return config[key] if key in config else default


def _announcements_on(hass) -> bool:
    return _truthy(_runtime(hass, "announcements_enabled", True))


def _package_detection_on(hass) -> bool:
    return _truthy(_runtime(hass, "package_detection", False))


def _in_quiet_hours(hass) -> bool:
    """The household's quiet hours, as delivery.py reads them."""
    from . import delivery
    return delivery.in_quiet_hours(hass)


def _nobody_home(hass) -> bool:
    """True only when the residents are known to be away (unknown is home)."""
    from . import alert_path
    return alert_path.nobody_home(alert_path.situation(hass))


async def _deliver(hass, event: str, msg: str, entity_id: str, tts_entity, speakers,
                   detected_by: str, **kw):
    """Send one package message through delivery.py, which applies shush, the
    Announcements switch, quiet hours, sleep and "Notifications only", and
    pushes to phones what it does not speak. Recorded as a package decision."""
    from . import delivery
    request = delivery.DeliveryRequest(
        source="package_monitor", message=msg, entity_id=entity_id,
        category=f"package_{event}", speakers=tuple(speakers or ()),
        tts_entity=tts_entity or "", tts_context="package", record_kind="package",
        record_facts={"event": event, "detected_by": detected_by},
        record_assessment=f"package {event}", **kw)
    return await delivery.async_deliver(hass, request)


# Which entities count as the front door
#
# Names are compared on whole tokens of the part after the dot (split on "_"
# and between letters and digits), never on substrings, so a wide view such
# as camera.front_yard is not swept in by "front".

_PORCH_TOKENS = frozenset({
    "doorbell", "frontdoor", "porch", "front",
    # Glued forms that are plainly the same place.
    "frontporch", "frontdoorbell",
})
# Any of these marks a wide view and rules the name out even when it also
# has a porch token (camera.front_driveway).
_WIDE_TOKENS = frozenset({
    "yard", "backyard", "frontyard", "street", "road", "lawn", "garden", "driveway",
    "curb", "field", "garage", "pool", "patio", "deck", "gate", "parking", "lot",
})
_MAILBOX_TOKENS = frozenset({"mailbox", "letterbox", "postbox"})
_MOTION_DEVICE_CLASSES = frozenset({"motion", "occupancy", "presence"})
_MAILBOX_DEVICE_CLASSES = frozenset({"opening", "door", "occupancy"})


def _name_tokens(entity_id: str) -> frozenset[str]:
    object_id = str(entity_id).split(".", 1)[-1].lower()
    return frozenset(re.findall(r"[a-z]+|\d+", object_id))


def _is_porch_view(entity_id: str) -> bool:
    """A front door, porch or doorbell name that is not a wide view."""
    tokens = _name_tokens(entity_id)
    return bool(tokens & _PORCH_TOKENS) and not tokens & _WIDE_TOKENS


def watched_cameras(hass, configured=None) -> list[str]:
    """The cameras the vision sweep checks.

    A Eufy camera with its own package sensors is left out: note_from_eufy
    reads those sensors directly, more reliably than a photo and at no
    vision cost, so a sweep of it would be a wasted call.
    """
    if configured:
        chosen = [configured] if isinstance(configured, str) else list(configured)
        return [c for c in chosen if hass.states.get(c)]
    from . import eufy
    from .camera import active_camera_states
    watched = []
    for state in active_camera_states(hass):
        camera = state.entity_id
        if not _is_porch_view(camera):
            continue
        native = (eufy.is_eufy_camera(hass, camera)
                  and "package_delivered" in eufy.discover_roles(hass, camera))
        if not native:
            watched.append(camera)
    return watched


# State machine and announcements

_PACKAGE_KIND_TO_STATE = {
    "delivered": "delivered",
    "mail": "mail",
    "removed": "taken",
    "stranded": "stranded",
}

_OBSERVER_NOTES = {
    "delivered": "A package was delivered",
    "mail": "Mail arrived",
    "removed": "A package was removed",
    "stranded": "A package hasn't been picked up yet",
}

_EMPTY = {"package": False, "mail": False, "count": 0}


def _now_mono() -> float:
    return time.monotonic()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cooldown_open(entity_id: str, kind: str) -> bool:
    """Whether this camera and kind has gone 30 minutes without being spoken."""
    spoken_at = _LAST_SPOKEN.get((entity_id, kind))
    if spoken_at is None:
        return True
    return _now_mono() - spoken_at >= _ANNOUNCE_COOLDOWN_S


def _mark_spoken(entity_id: str, kind: str) -> None:
    _LAST_SPOKEN[(entity_id, kind)] = _now_mono()


def _log(hass, entity_id: str, kind: str, det: dict, source: str) -> None:
    """Record one transition in the Nova log, the observer and, for the
    package kinds, camera_semantic. Each is optional and never raises."""
    try:
        from .websocket import nova_log
        flags = f"(pkg={det.get('package')} mail={det.get('mail')} n={det.get('count')})"
        nova_log("CAMERA", f"{entity_id} package-monitor [{source}]: {kind} {flags}")
    except Exception:
        pass
    try:
        from . import observer
        observer.record_camera_event(entity_id, _OBSERVER_NOTES.get(kind, kind), "delivery",
                                     notable=kind in _OBSERVER_NOTES)
    except Exception:
        pass
    # Semantic learning rides on the same single point every real
    # transition passes through, once each, alongside (never instead of)
    # the announcement. camera_semantic's sources are "eufy" or "vision";
    # the periodic and doorbell passes are Nova's own vision.
    try:
        from . import camera_semantic
        package_state = _PACKAGE_KIND_TO_STATE.get(kind)
        if package_state and hass is not None:
            hass.async_create_task(camera_semantic.record_event(
                hass, label="package", camera_entity=entity_id,
                source="eufy" if source == "eufy" else "vision",
                package_state=package_state, detail=source,
            ))
    except Exception:
        pass


def _with_honorific(honorific, sentence: str) -> str:
    """``honorific`` is "" when nobody in particular is home. It is not title
    cased (unlike persona.lead_in); a sentence on its own gets a capital."""
    if honorific:
        return f"{honorific}, {sentence}"
    return sentence[:1].upper() + sentence[1:]


async def evaluate(hass, groq_client, honorific, tts_entity, speakers,
                   entity_id: str, det: dict, source: str = "periodic") -> bool:
    """Apply one detection to the camera's state and announce what changed.
    Returns whether anything was spoken."""
    from . import alert_path, delivery

    before = _STATE.get(entity_id, _EMPTY)
    place = "the front door"
    spoken = []

    # delivery.py applies the household's rules; the 30 minute limits and
    # "removed only while away" live here.
    async def send(event, text, **kw):
        sent = await _deliver(hass, event, text, entity_id, tts_entity, speakers,
                              source, **kw)
        spoken.append(sent.spoken)
        # A removed while away alert counts once it reached anyone.
        if sent.spoken or (event == "removed" and sent.decision.delivered):
            _mark_spoken(entity_id, event)

    had_package, has_package = bool(before.get("package")), bool(det.get("package"))
    if has_package and not had_package:
        count = det.get("count", 1)
        if count and count > 1:
            sentence = f"{count} packages have been delivered to {place}."
        else:
            sentence = f"a package has been delivered to {place}."
        _log(hass, entity_id, "delivered", det, source)
        if _cooldown_open(entity_id, "delivered"):
            await send("delivered", _with_honorific(honorific, sentence))
    elif had_package and not has_package:
        _log(hass, entity_id, "removed", det, source)
        # Only an alert while the residents are known to be away; then it
        # goes to every phone first and is spoken where allowed.
        sit = alert_path.situation(hass)
        if not alert_path.nobody_home(sit):
            await alert_path.async_record_decision(
                hass, "package", source="package_monitor", entity_id=entity_id, sit=sit,
                plan=alert_path.Plan(alert=False, reason="someone may be home"),
                facts={"event": "removed", "detected_by": source},
                assessment="package removed")
        elif _cooldown_open(entity_id, "removed"):
            sentence = f"a package was just removed from {place} while no one is home."
            await send("removed", _with_honorific(honorific, sentence),
                       push=delivery.PUSH_ALWAYS, all_phones=True,
                       push_first=True, push_kind="package_removed")

    if det.get("mail") and not before.get("mail"):
        _log(hass, entity_id, "mail", det, source)
        if _cooldown_open(entity_id, "mail"):
            await send("mail", _with_honorific(honorific, f"mail has arrived at {place}."))

    _STATE[entity_id] = record = {
        "package": has_package,
        "mail": bool(det.get("mail")),
        "count": int(det.get("count", 0) or 0),
        "since": _utc_now(),
        "desc": det.get("description", ""),
    }
    # Saved so a restart neither repeats a waiting parcel nor misses its
    # pickup (situations.py). Does nothing until Nova has started.
    try:
        from . import situations
        situations.save_delivery(entity_id, record)
    except Exception:
        pass
    return any(spoken)


def _confirm_transitions(prev: dict, det: dict, det2: Optional[dict]) -> dict:
    """A flag turning True (which would announce) must also be True on the
    second look, or it is dropped for this round. Flags already set, and
    flags turning False, pass through unchanged."""
    confirmed = dict(det)
    for flag in ("package", "mail"):
        is_new = det.get(flag) and not prev.get(flag)
        if is_new and not (det2 and det2.get(flag)):
            confirmed[flag] = False
            if flag == "package":
                confirmed["count"] = 0
    if confirmed.get("package") and det2 and det2.get("package"):
        confirmed["count"] = max(int(det.get("count") or 1), int(det2.get("count") or 1))
    return confirmed


def _confirm_pickup(prev: dict, det: dict, det2: Optional[dict]) -> dict:
    """A package that seems gone while nobody is home must also be gone on
    the second look before it can raise the theft alert. Otherwise (still
    there, or no second frame) it stays tracked for this round."""
    if det2 is not None and not det2.get("package"):
        return det
    return dict(det, package=True, count=max(1, int(prev.get("count") or 1)),
                description=prev.get("desc", ""))


async def periodic_check(hass, groq_client, honorific, tts_entity, speakers,
                         configured_camera=None) -> dict:
    """Check each watched camera once. Skipped in quiet hours, when nothing
    would be announced and the overnight vision calls are saved."""
    if _in_quiet_hours(hass):
        return {"skipped": "quiet_hours"}
    cameras = watched_cameras(hass, configured_camera)
    checked = 0
    for entity_id in cameras:
        det = await detect_on_camera(hass, groq_client, entity_id)
        if det is None:
            continue
        # A change that would alert gets one more frame first, and both
        # frames must agree: a new sighting, or a package gone while nobody
        # is home. One extra call, only when an announcement is at stake.
        prev = _STATE.get(entity_id, {"package": False, "mail": False})
        arriving = any(det.get(f) and not prev.get(f) for f in ("package", "mail"))
        leaving = (bool(prev.get("package")) and not det.get("package")
                   and _nobody_home(hass))
        if arriving or leaving:
            second = await detect_on_camera(hass, groq_client, entity_id)
            if arriving:
                det = _confirm_transitions(prev, det, second)
            if leaving:
                det = _confirm_pickup(prev, det, second)
        await evaluate(hass, groq_client, honorific, tts_entity, speakers,
                       entity_id, det, source="periodic")
        checked += 1
    return {"checked": checked, "cameras": cameras}


async def note_from_doorbell(hass, groq_client, honorific, tts_entity, speakers,
                             entity_id: str, analysis_text: str) -> None:
    """Feed the doorbell press description into the state machine.

    That text describes the person at the door, so it can add a package or
    mail but never clear one: saying nothing about the parcel is not proof
    it has gone. Pickups come from the camera sweep and Eufy.
    """
    det = detection_from_text(analysis_text)
    if not (det["package"] or det["mail"]):
        return
    tracked = _STATE.get(entity_id)
    if tracked:
        det["package"] = det["package"] or bool(tracked.get("package"))
        det["mail"] = det["mail"] or bool(tracked.get("mail"))
        if det["package"]:
            det["count"] = max(1, det["count"], int(tracked.get("count") or 0))
    await evaluate(hass, groq_client, honorific, tts_entity, speakers,
                   entity_id, det, source="doorbell")


async def note_from_eufy(hass, honorific, tts_entity, speakers,
                         entity_id: str, role: str) -> None:
    """Eufy's own package sensors, at no vision cost.

    ``role`` is package_delivered, package_taken or package_stranded (eufy.py
    names). Delivered and taken go through ``evaluate`` like a vision result,
    so the wording, the one time announcement and the panel status are the
    same whichever source saw it. Stranded has no flag to compare (Eufy
    already means "still there, at risk"), so it is a direct one off message.
    """
    if role in ("package_delivered", "package_taken"):
        prev = _STATE.get(entity_id, _EMPTY)
        if role == "package_delivered":
            det = {"package": True, "mail": prev.get("mail", False),
                   "count": max(1, int(prev.get("count") or 0)),
                   "description": "(Eufy) package delivered"}
        else:
            det = {"package": False, "mail": prev.get("mail", False), "count": 0,
                   "description": "(Eufy) package taken"}
        await evaluate(hass, None, honorific, tts_entity, speakers, entity_id, det,
                       source="eufy")
        return
    if role != "package_stranded":
        return
    det = {"package": True, "mail": False, "count": 1}
    if _cooldown_open(entity_id, "stranded"):
        if honorific:
            text = f"{honorific}, a package at the front door hasn't been picked up yet."
        else:
            text = "A package at the front door hasn't been picked up yet."
        sent = await _deliver(hass, "stranded", text, entity_id, tts_entity, speakers, "eufy")
        if sent.spoken:
            _mark_spoken(entity_id, "stranded")
    # A repeat inside 30 minutes is still logged and recorded, just not sent.
    _log(hass, entity_id, "stranded", det, "eufy")


# Early checks from sensors
#
# These only bring a check forward and add no way to speak: motion runs the
# normal periodic_check (second look included) and the mailbox runs
# evaluate(), so its gates and the 30 minute limit still apply.

def _eufy_covered_entities(hass) -> set[str]:
    """Entities on a Eufy camera that note_from_eufy already covers (the same
    cameras watched_cameras leaves out). Never raises."""
    covered: set[str] = set()
    try:
        from homeassistant.helpers import entity_registry as er
        from . import eufy
        registry = er.async_get(hass)
        for camera, roles in eufy.all_camera_roles(hass).items():
            if "package_delivered" not in roles:
                continue
            covered.update(roles.values())
            device = getattr(registry.async_get(camera), "device_id", None)
            if device:
                covered.update(entry.entity_id for entry in registry.entities.values()
                               if entry.device_id == device)
    except Exception as exc:
        _LOGGER.debug("Nova package: eufy coverage lookup failed: %s", exc)
    return covered


def discover_trigger_sensors(hass) -> dict[str, str]:
    """Binary sensors that may bring a check forward, as entity_id to
    "motion" or "mailbox". Found once at setup like the Eufy roles, so a
    sensor added later needs a Nova reload."""
    found: dict[str, str] = {}
    try:
        candidates = hass.states.async_all("binary_sensor")
    except Exception:
        return found
    covered: Optional[set[str]] = None
    for state in candidates:
        entity_id = state.entity_id
        attrs = getattr(state, "attributes", None) or {}
        device_class = str(attrs.get("device_class") or "").lower()
        if _name_tokens(entity_id) & _MAILBOX_TOKENS and (
                device_class == "" or device_class in _MAILBOX_DEVICE_CLASSES):
            found[entity_id] = "mailbox"
        elif device_class in _MOTION_DEVICE_CLASSES and _is_porch_view(entity_id):
            if covered is None:
                covered = _eufy_covered_entities(hass)
            if entity_id not in covered:
                found[entity_id] = "motion"
    return found


def _too_soon(key: str, gap: float) -> bool:
    """Whether ``key`` fired less than ``gap`` seconds ago; stamps it if not."""
    now = _now_mono()
    last = _TRIGGER_LAST.get(key)
    if last is not None and now - last < gap:
        return True
    _TRIGGER_LAST[key] = now
    return False


async def note_from_motion(hass, groq_client, ctx: Callable[[], tuple],
                           entity_id: str) -> Optional[dict]:
    """Porch or doorbell motion: wait 20 seconds, then run periodic_check
    once. At most one early check every 3 minutes overall. ``ctx()`` gives
    (honorific, tts_entity, speakers) and is read after the wait, so presence
    is current."""
    allowed = _package_detection_on(hass) and _announcements_on(hass)
    if not allowed or _in_quiet_hours(hass):
        return None
    if _too_soon(_EARLY_KEY, _EARLY_CHECK_MIN_GAP_S):
        return None
    await asyncio.sleep(_EARLY_CHECK_DELAY_S)
    if not _package_detection_on(hass):
        return None
    honorific, tts_entity, speakers = ctx()
    return await periodic_check(hass, groq_client, honorific, tts_entity, speakers,
                                configured_camera=None)


def _mail_still_current(prev: dict) -> bool:
    """Mail is set and was set within the cooldown. An older True is left
    from an earlier delivery, since nothing sweeps a mailbox sensor clear."""
    if not prev.get("mail"):
        return False
    since = prev.get("since")
    if not isinstance(since, datetime):
        return True
    return (_utc_now() - since).total_seconds() < _ANNOUNCE_COOLDOWN_S


async def note_from_mailbox(hass, groq_client, ctx: Callable[[], tuple],
                            entity_id: str) -> bool:
    """A mailbox sensor opened: evaluate() with mail True and the package
    left as it is, source "mailbox". Ignored while mail is already current,
    and at most once every 5 minutes per sensor. Returns whether anything
    was spoken."""
    if not _package_detection_on(hass):
        return False
    if _too_soon(f"mailbox:{entity_id}", _MAILBOX_MIN_GAP_S):
        return False
    prev = _STATE.get(entity_id, _EMPTY)
    if _mail_still_current(prev):
        return False
    if prev.get("mail"):
        # Left over from an earlier delivery: this is a new arrival.
        prev = _STATE[entity_id] = dict(prev, mail=False)
    honorific, tts_entity, speakers = ctx()
    det = {"package": bool(prev.get("package")), "mail": True,
           "count": int(prev.get("count") or 0), "description": "(mailbox sensor) mail"}
    return await evaluate(hass, groq_client, honorific, tts_entity, speakers,
                          entity_id, det, source="mailbox")


def status() -> dict:
    """Tracked package and mail state per camera, for the panel and
    diagnostics."""
    keys = ("package", "mail", "count")
    return {camera: {**{k: rec.get(k) for k in keys}, "desc": rec.get("desc", "")}
            for camera, rec in _STATE.items()}


def restore_from_situations() -> int:
    """Reload the package state saved before a restart (situations.py), so a
    parcel already seen is not announced again and its pickup is still
    noticed. Returns how many cameras came back; never raises."""
    try:
        from . import situations
        saved = situations.restore_deliveries()
    except Exception:
        return 0
    for camera, rec in saved.items():
        try:
            since = datetime.fromisoformat(str(rec["since"])) if rec.get("since") else None
        except ValueError:
            since = None
        _STATE.setdefault(camera, {
            "package": bool(rec.get("package")),
            "mail": bool(rec.get("mail")),
            "count": int(rec.get("count") or 0),
            "since": since or _utc_now(),
            "desc": str(rec.get("desc") or ""),
        })
    return len(saved)
