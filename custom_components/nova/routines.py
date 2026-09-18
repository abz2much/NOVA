"""
Nova — House routines (sleep / wake / away / home).

Each routine is a sequence of service calls that Nova orchestrates and
narrates. Configurable at runtime — users can override any step via
/config/nova_routines.yaml.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import yaml

from homeassistant.core import HomeAssistant, ServiceCall

from .tts_helper import async_announce

_LOGGER = logging.getLogger(__name__)

ROUTINE_FILE = "/config/nova_routines.yaml"

# Sensible defaults — user can override by creating /config/nova_routines.yaml
DEFAULT_ROUTINES: dict[str, list[dict]] = {
    "goodnight": [
        {"service": "light.turn_off", "target": {"entity_id": "all"}, "announce": "Turning off lights, {honorific}."},
        {"service": "lock.lock",      "target": {"area_id": "all"},   "announce": "Locking up."},
        {"service": "cover.close_cover", "target": {"device_class": "shade"}, "announce": "Closing the shades."},
        # Scene for final state — users should define scene.nighttime or similar
        {"service": "scene.turn_on",  "data": {"entity_id": "scene.nighttime"}, "optional": True},
        {"announce": "Goodnight, {honorific}."},
    ],
    "goodmorning": [
        {"service": "cover.open_cover", "target": {"device_class": "shade"}, "announce": "Opening the shades, {honorific}."},
        {"service": "scene.turn_on",    "data": {"entity_id": "scene.morning"}, "optional": True},
        {"announce": "Good morning, {honorific}. I hope you slept well."},
    ],
    "leaving": [
        {"service": "light.turn_off",  "target": {"entity_id": "all"}, "announce": "Shutting everything down, {honorific}."},
        {"service": "climate.set_preset_mode", "data": {"preset_mode": "eco"}, "target": {"entity_id": "all"}, "optional": True},
        {"service": "lock.lock",       "target": {"area_id": "all"}},
        {"announce": "Locked up. Have a good one, {honorific}."},
    ],
    "arriving": [
        {"service": "scene.turn_on",  "data": {"entity_id": "scene.welcome"}, "optional": True, "announce": "Welcome home, {honorific}."},
        {"service": "climate.set_preset_mode", "data": {"preset_mode": "home"}, "target": {"entity_id": "all"}, "optional": True},
    ],
}


def _load_routines() -> dict[str, list[dict]]:
    """Load user routines from file if present, else use defaults."""
    if os.path.exists(ROUTINE_FILE):
        try:
            with open(ROUTINE_FILE) as f:
                user_routines = yaml.safe_load(f) or {}
            if isinstance(user_routines, dict):
                merged = {**DEFAULT_ROUTINES, **user_routines}
                _LOGGER.debug("Nova: loaded %d user routines", len(user_routines))
                return merged
        except Exception as exc:
            _LOGGER.warning("Nova: could not parse %s: %s", ROUTINE_FILE, exc)
    return DEFAULT_ROUTINES


async def async_run_routine(
    hass: HomeAssistant,
    call: ServiceCall,
    honorific: str,
    tts_entity: str | None,
    speakers: list[str],
) -> dict:
    """
    Service: nova.routine
    Run a named routine by iterating through its steps.
    """
    name: str = call.data["name"].lower().strip()
    routines = _load_routines()

    if name not in routines:
        _LOGGER.warning("Nova: unknown routine '%s'", name)
        # honorific may be "" once nobody specific is home to address (see
        # honorific.py) — addr collapses the trailing ", {honorific}" to
        # nothing rather than a dangling comma.
        addr = f", {honorific}" if honorific else ""
        msg = f"I don't know a routine called {name}{addr}."
        await async_announce(hass, msg, tts_entity, speakers)
        return {"success": False, "error": "unknown_routine"}

    steps = routines[name]
    _LOGGER.info("Nova: running routine '%s' (%d steps)", name, len(steps))

    from . import action_log
    request_id = action_log.new_request_id()
    requested_by_user_id = getattr(getattr(call, "context", None), "user_id", None)
    # Only steps with a service call are audited targets — an announce-only
    # step is pure speech, out of scope for this log. Placeholder rows for
    # the whole routine run are created in one batch (v3 correction), then
    # each step updates its own row as it's processed.
    service_step_indices = [i for i, s in enumerate(steps) if s.get("service")]
    step_targets = []
    for i in service_step_indices:
        s = steps[i]
        svc = s.get("service", "")
        d, sv = svc.split(".", 1) if "." in svc else ("", svc)
        step_targets.append({"key": i, "domain": d or None, "service": sv or None})
    row_ids = await hass.async_add_executor_job(
        lambda: action_log.start_many(
            request_id, f"routine:{name}", "routine", step_targets,
            requested_by_user_id=requested_by_user_id,
        )
    )

    errors = []
    executed = 0
    for step_i, step in enumerate(steps):
        row_id = row_ids.get(step_i)
        try:
            # Announce, if present
            announce_text = step.get("announce")
            if announce_text:
                from .directive_helper import fill_honorific
                text = fill_honorific(announce_text, honorific)
                await async_announce(
                    hass, text, tts_entity, speakers,
                    context="welcome" if name == "arriving" else "routine",
                )

            # Service call, if present
            service = step.get("service")
            if service:
                domain, svc = service.split(".", 1)
                data = step.get("data", {})
                target = step.get("target") or None

                # Authorization gate (audit fix, Sept 2026): a routine step can
                # be just as sensitive as a direct control_device call (the
                # default "goodmorning" routine opens covers; a custom routine
                # could just as easily unlock or disarm, or activate a scene/
                # script/automation whose contents this module can't inspect).
                # Steps aren't always scoped to one entity_id (target can be an
                # area/device_class), so this gates at the step level — one
                # confirmation for whatever the step touches, using the first
                # entity named in its data (if any) for the voice prompt.
                step_entity = (data or {}).get("entity_id", "") if isinstance(data, dict) else ""
                if isinstance(step_entity, list):
                    step_entity = step_entity[0] if step_entity else ""
                from . import policy
                if policy.requires_confirmation(hass, domain, svc, step_entity):
                    await hass.async_add_executor_job(
                        lambda rid=row_id: action_log.mark_awaiting_approval(rid)
                    )
                    ok_gate, gate_note, approval_result = await policy.confirm_gate(
                        hass, domain, svc, step_entity, service.replace(".", " "))
                    await hass.async_add_executor_job(
                        lambda rid=row_id, ar=approval_result: action_log.set_approval(
                            rid, ar, approval_required=(ar != "not_required"))
                    )
                    if not ok_gate:
                        if step.get("optional"):
                            _LOGGER.debug(
                                "Nova: optional step '%s' needs confirmation, skipped: %s",
                                service, gate_note)
                        else:
                            _LOGGER.warning(
                                "Nova: step '%s' needs confirmation, not run: %s",
                                service, gate_note)
                            errors.append(f"{service}: {gate_note or 'confirmation required'}")
                        await hass.async_add_executor_job(
                            lambda rid=row_id: action_log.set_execution(
                                rid, "blocked", reason_code="confirmation_not_approved")
                        )
                        continue

                try:
                    await hass.services.async_call(domain, svc, data, target=target, blocking=True)
                    executed += 1
                    await hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(rid, "accepted")
                    )
                except Exception as exc:
                    if step.get("optional"):
                        _LOGGER.debug("Nova: optional step '%s' skipped: %s", service, exc)
                    else:
                        _LOGGER.warning("Nova: step '%s' failed: %s", service, exc)
                        errors.append(f"{service}: {exc}")
                    await hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(
                            rid, "failed", reason_code="service_call_failed")
                    )
        except Exception as exc:
            _LOGGER.warning("Nova: routine step error: %s", exc)
            errors.append(str(exc))

    return {
        "success": not errors,
        "routine": name,
        "steps_executed": executed,
        "errors": errors,
    }


def list_routines() -> list[str]:
    """Return names of available routines."""
    return list(_load_routines().keys())
