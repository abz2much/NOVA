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

Vision and capture plumbing is reused from camera.py via lazy import to avoid a
circular dependency; the module itself is otherwise dependency-light.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# Per-camera state: entity_id -> {"package": bool, "mail": bool, "count": int,
#                                 "since": datetime, "desc": str}
_STATE: dict[str, dict] = {}

_PKG_KEYWORDS = re.compile(
    r"\b(package|parcel|box|delivery|delivered|amazon|ups|fedex|usps|dhl|"
    r"carton|crate|cardboard)\b", re.I,
)
_MAIL_KEYWORDS = re.compile(
    r"\b(mail|letter|letters|envelope|envelopes|mailman|mail\s*carrier|"
    r"postal|postman|post)\b", re.I,
)
# Negated mentions — "no package visible", "not carrying a delivery",
# "without any boxes", "no sign of packages or mail" — must not count as
# sightings. The doorbell analysis text frequently *rules out* deliveries in
# exactly these words, which raw keyword matching turned into false
# "package delivered" announcements. Strip negated spans (including
# or/and-connected chains) before keyword matching.
_DELIVERY_WORD = (
    r"(?:package|packages|parcel|parcels|box|boxes|delivery|deliveries|"
    r"mail|letter|letters|envelope|envelopes)\w*"
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
    # Construct off the event loop — creating a provider does blocking SSL
    # setup (same reason the two camera.py call sites do this).
    client = await hass.async_add_executor_job(
        cam._make_client, hass, provider, model, groq_client,
        cam._client_settings(hass, provider))
    b64 = base64.b64encode(img).decode()
    try:
        result = await hass.async_add_executor_job(
            lambda: client.chat(
                messages=[
                    {"role": "system", "content": _PKG_PROMPT},
                    {"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": "Classify per the instructions. JSON only."},
                    ]},
                ],
                max_tokens=120,
                model_override=model or None,
            )
        )
        text = (result.get("text") or "").strip()
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


def _anyone_home(hass) -> bool:
    """Best-effort presence. Defaults to True (assume home) when undeterminable —
    so a 'package removed while away' alert never false-fires on unknown state."""
    try:
        persons = hass.states.async_all("person")
        if persons:
            if any(st.state == "home" for st in persons):
                return True
            # All persons known and none home → away
            if all(st.state not in ("unknown", "unavailable") for st in persons):
                return False
        z = hass.states.get("zone.home")
        if z and str(z.state).isdigit():
            return int(z.state) > 0
    except Exception:
        pass
    return True


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
        if any(k in e for k in ("doorbell", "front_door", "porch", "front")):
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

    prev = _STATE.get(entity_id, {"package": False, "mail": False, "count": 0})
    quiet = _in_quiet_hours(hass)
    can_speak = _announcements_on(hass) and not quiet
    loc = "the front door"
    spoke = False

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
        if can_speak:
            await async_announce(hass, msg, tts_entity, speakers, context="package")
            spoke = True
    # Package removed
    elif prev.get("package") and not det.get("package"):
        away = not _anyone_home(hass)
        _log(hass, entity_id, "removed", det, source)
        if away and can_speak:
            await async_announce(
                hass,
                _lead(f"a package was just removed from {loc} while no one is home."),
                tts_entity, speakers, context="package",
            )
            spoke = True

    # Mail arrival
    if det.get("mail") and not prev.get("mail"):
        _log(hass, entity_id, "mail", det, source)
        if can_speak:
            await async_announce(
                hass, _lead(f"mail has arrived at {loc}."),
                tts_entity, speakers, context="package",
            )
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
        det = {"package": True, "mail": False, "count": 1}
        if _in_quiet_hours(hass) or not _announcements_on(hass):
            _log(hass, entity_id, "stranded", det, "eufy")
            return
        msg = (f"{honorific}, a package at the front door hasn't been picked up yet."
               if honorific else "A package at the front door hasn't been picked up yet.")
        await async_announce(hass, msg, tts_entity, speakers, context="package")
        _log(hass, entity_id, "stranded", det, "eufy")


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
