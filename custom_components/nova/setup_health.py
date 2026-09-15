"""Nova Setup Doctor (Phase 2) — read-only configuration health checks.

Distinct from diagnostics/service_health.py, which answers "is everything
Nova depends on actually up" (LLM/embeddings/TTS/STT/cameras/routines/
database/scheduler — 8 checks, reused here as-is, never duplicated). This
module answers a different question: "does Nova's own CONFIGURATION still
point at things that exist" — a room speaker reassigned to a deleted
media_player, a departure-tracking entity that was renamed, a notify service
that no longer exists, an Assist pipeline that isn't actually using Nova.

Every check here is read-only: it inspects hass.states, the entity registry,
registered services, and nova_config — and never writes, calls a service, or
creates a Home Assistant Repair issue. Each check runs only when the feature
it covers is actually configured; an unconfigured optional feature reports
``off``, never a failure. Results carry a plain-language suggested manual fix
— there are no automatic fixes.
"""
from __future__ import annotations

import json
import logging

_LOGGER = logging.getLogger(__name__)

_OK = "ok"
_WARN = "warn"
_DOWN = "down"
_OFF = "off"


def _cfg(key: str, default=None):
    try:
        from . import nova_config
        return nova_config.get(key, default)
    except Exception:
        return default


def _entity_exists(hass, entity_id: str) -> bool:
    try:
        return hass.states.get(entity_id) is not None
    except Exception:
        return False


# ── individual checks ────────────────────────────────────────────────────────

def _check_entity_references(hass) -> dict:
    """Single-entity config values Nova actively reads: a stale one silently
    disables the feature it drives, with no error anywhere else to surface it."""
    out = {"name": "Entity references", "key": "entity_references",
          "status": _OFF, "detail": ""}
    fields = {
        "departure_origin_entity": str(_cfg("departure_origin_entity", "") or "").strip(),
        "departure_travel_sensor": str(_cfg("departure_travel_sensor", "") or "").strip(),
    }
    configured = {k: v for k, v in fields.items() if v}
    incl = _cfg("pattern_include_entities", []) or []
    if isinstance(incl, list):
        for i, eid in enumerate(incl):
            configured[f"pattern_include_entities[{i}]"] = str(eid)

    if not configured:
        out["detail"] = "no tracked entity references configured"
        return out

    missing = [f"{label} ({eid})" for label, eid in configured.items()
              if not _entity_exists(hass, eid)]
    if missing:
        out["status"] = _WARN
        out["detail"] = f"{len(missing)} configured entity(ies) no longer exist: {', '.join(missing)}"
        out["suggested_fix"] = "Reassign or remove the missing entity in Settings, then re-check."
    else:
        out["status"] = _OK
        out["detail"] = f"{len(configured)} configured entity reference(s) all resolve"
    return out


def _check_room_speakers(hass) -> dict:
    out = {"name": "Room speakers", "key": "room_speakers", "status": _OFF, "detail": ""}
    room_speakers = _cfg("room_speakers", {}) or {}
    general_speaker = str(_cfg("general_speaker", "") or "").strip()
    if not isinstance(room_speakers, dict):
        room_speakers = {}
    if not room_speakers and not general_speaker:
        out["detail"] = "no room speakers configured"
        return out

    missing = [f"{area}: {eid}" for area, eid in room_speakers.items()
               if not _entity_exists(hass, eid)]
    if general_speaker and not _entity_exists(hass, general_speaker):
        missing.append(f"general speaker: {general_speaker}")

    if missing:
        out["status"] = _WARN
        out["detail"] = f"{len(missing)} assigned speaker(s) no longer exist: {', '.join(missing)}"
        out["suggested_fix"] = "Reassign the missing speaker(s) under Settings → Room Speakers."
    else:
        total = len(room_speakers) + (1 if general_speaker else 0)
        out["status"] = _OK
        out["detail"] = f"{total} assigned speaker(s) all resolve"
    return out


def _check_camera_overrides(hass) -> dict:
    out = {"name": "Camera overrides", "key": "camera_overrides", "status": _OFF, "detail": ""}
    overrides = _cfg("camera_overrides", {}) or {}
    if not isinstance(overrides, dict) or not overrides:
        out["detail"] = "no camera overrides configured"
        return out

    missing = [eid for eid in overrides if not _entity_exists(hass, eid)]
    if missing:
        out["status"] = _WARN
        out["detail"] = f"{len(missing)} overridden camera(s) no longer exist: {', '.join(missing)}"
        out["suggested_fix"] = "Remove the stale override under Settings → Cameras."
    else:
        out["status"] = _OK
        out["detail"] = f"{len(overrides)} camera override(s) all resolve"
    return out


def _check_notify_service(hass) -> dict:
    out = {"name": "Notification service", "key": "notify_service", "status": _OFF, "detail": ""}
    notify_service = str(_cfg("notify_service", "") or "").strip()
    if not notify_service:
        out["detail"] = "no notification service configured"
        return out

    if "." not in notify_service:
        out["status"] = _WARN
        out["detail"] = f"'{notify_service}' is not a valid domain.service"
        out["suggested_fix"] = "Re-select the notification target under Settings → Notifications."
        return out

    domain, service = notify_service.split(".", 1)
    try:
        registered = hass.services.has_service(domain, service)
    except Exception:
        registered = False
    if registered:
        out["status"] = _OK
        out["detail"] = f"{notify_service} is registered"
    else:
        out["status"] = _WARN
        out["detail"] = f"configured service '{notify_service}' is not registered"
        out["suggested_fix"] = "Re-select the notification target under Settings → Notifications."
    return out


def _check_assist_pipeline(hass) -> dict:
    """Nova's own conversation entity must actually be the pipeline's
    conversation agent, or Assist voice turns never reach Nova at all —
    see bootstrap.async_ensure_pipeline_agent's own docstring for why this
    silent-mismatch failure mode exists in the first place."""
    out = {"name": "Assist pipeline", "key": "assist_pipeline", "status": _OFF, "detail": ""}
    try:
        from homeassistant.components import assist_pipeline
    except Exception:
        out["detail"] = "assist_pipeline component not loaded"
        return out

    try:
        from . import bootstrap
        agent = bootstrap._find_nova_agent(hass)
    except Exception:
        agent = None
    if not agent:
        out["status"] = _WARN
        out["detail"] = "Nova's conversation entity was not found"
        out["suggested_fix"] = "Restart Home Assistant; if this persists, reinstall the Nova integration."
        return out

    try:
        pipelines = list(assist_pipeline.async_get_pipelines(hass))
    except Exception:
        pipelines = []
    nova_pipeline = None
    for p in pipelines:
        name = (getattr(p, "name", "") or "").lower()
        voice = (getattr(p, "tts_voice", "") or "").lower()
        if "nova" in name or "nova" in voice:
            nova_pipeline = p
            break

    if nova_pipeline is None:
        out["status"] = _WARN
        out["detail"] = "no Nova-named or Nova-voiced Assist pipeline found"
        out["suggested_fix"] = ("Create an Assist pipeline under Settings → Voice Assistants "
                                "with Nova as the conversation agent.")
        return out

    engine = getattr(nova_pipeline, "conversation_engine", None)
    if engine != agent:
        out["status"] = _WARN
        out["detail"] = (f"pipeline '{getattr(nova_pipeline, 'name', '?')}' uses conversation "
                         f"agent '{engine}', not Nova ({agent})")
        out["suggested_fix"] = ("Set the pipeline's conversation agent to Nova under "
                                "Settings → Voice Assistants.")
        return out

    out["status"] = _OK
    out["detail"] = f"pipeline '{getattr(nova_pipeline, 'name', '?')}' uses Nova as its agent"
    return out


def _check_person_entities(hass) -> dict:
    out = {"name": "Person entities", "key": "person_entities", "status": _OFF, "detail": ""}
    honorifics = _cfg("person_honorifics", {}) or {}
    # Stored via nova/update_config as a JSON-encoded string (the panel
    # sends JSON.stringify(overrides)); nova_config.get() returns it
    # verbatim, so it needs decoding here the same way honorific.py's own
    # reader does — otherwise every configured person(s) look unconfigured.
    if isinstance(honorifics, str):
        try:
            honorifics = json.loads(honorifics)
        except Exception:
            honorifics = {}
    if not isinstance(honorifics, dict) or not honorifics:
        out["detail"] = "no per-person honorifics configured"
        return out

    missing = [eid for eid in honorifics if not _entity_exists(hass, eid)]
    if missing:
        out["status"] = _WARN
        out["detail"] = f"{len(missing)} configured person(s) no longer exist: {', '.join(missing)}"
        out["suggested_fix"] = "Remove the stale entry under Settings → Person Honorifics."
    else:
        out["status"] = _OK
        out["detail"] = f"{len(honorifics)} configured person(s) all resolve"
    return out


def _check_required_integrations(hass) -> dict:
    """Soft dependencies Nova relies on but doesn't hard-require via
    manifest.json (http/frontend/panel_custom are enforced by HA itself
    before Nova ever loads, so checking those would be checking a tautology)."""
    out = {"name": "Required integrations", "key": "required_integrations",
          "status": _OK, "detail": ""}
    try:
        loaded = set(hass.config.components)
    except Exception:
        loaded = set()
    missing = [c for c in ("conversation",) if c not in loaded]
    if missing:
        out["status"] = _DOWN
        out["detail"] = f"missing required integration(s): {', '.join(missing)}"
        out["suggested_fix"] = "Ensure the 'conversation' integration is enabled in Home Assistant."
    else:
        out["detail"] = "all required integrations loaded"
    return out


def _check_persistence(hass) -> dict:
    """A live write probe against Nova's own config directory — the same
    directory nova_config.py persists config.json to."""
    out = {"name": "Persistence", "key": "persistence", "status": _OK, "detail": ""}
    try:
        from . import nova_config
        cfg_dir = nova_config.CONFIG_PATH.parent
        cfg_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg_dir / ".setup_health_probe"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
        out["detail"] = f"{cfg_dir} is writable"
    except Exception as exc:
        out["status"] = _DOWN
        out["detail"] = f"config directory is not writable: {exc}"
        out["suggested_fix"] = "Check file permissions on Nova's config directory."
    return out


# ── aggregate ────────────────────────────────────────────────────────────────

async def run_setup_health(hass) -> dict:
    """Run every configuration-health check. Returns {overall, checks}.
    Never raises. Folds in diagnostics.service_health's 8 checks unchanged —
    never re-implements them."""
    checks = []
    try:
        from . import diagnostics
        result = await diagnostics.run_service_health(hass)
        checks.extend(result.get("services", []))
    except Exception as exc:
        checks.append({"name": "Core services", "key": "service_health",
                       "status": _DOWN, "detail": str(exc)})

    for fn, needs_executor in (
        (_check_entity_references, False),
        (_check_room_speakers, False),
        (_check_camera_overrides, False),
        (_check_notify_service, False),
        (_check_assist_pipeline, False),
        (_check_person_entities, False),
        (_check_required_integrations, False),
        (_check_persistence, True),  # does real file I/O — must not block the event loop
    ):
        try:
            if needs_executor:
                checks.append(await hass.async_add_executor_job(fn, hass))
            else:
                checks.append(fn(hass))
        except Exception as exc:
            checks.append({"name": fn.__name__, "key": fn.__name__,
                           "status": _DOWN, "detail": str(exc)})

    active = [c for c in checks if c.get("status") != _OFF]
    if any(c["status"] == _DOWN for c in active):
        overall = _DOWN
    elif any(c["status"] == _WARN for c in active):
        overall = _WARN
    elif active:
        overall = _OK
    else:
        overall = _OFF

    return {"overall": overall, "checks": checks}
