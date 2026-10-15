"""House routines: goodnight, good morning, leaving and arriving.

Each routine is a list of steps, each a service call, something to say, or
both, run and narrated in order. A nova_routines.yaml in Home Assistant's
config folder can replace any routine or add new ones.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import yaml

from homeassistant.core import HomeAssistant, ServiceCall

from . import paths
from .tts_helper import async_announce

_LOGGER = logging.getLogger(__name__)

ROUTINE_FILE: Optional[str] = None   # set to use another file; None asks paths.py


def _routine_file() -> str:
    return ROUTINE_FILE or paths.config_path("nova_routines.yaml")


def _step(service: str = "", *, announce: str = "", optional: bool = False,
          target: Optional[dict] = None, data: Optional[dict] = None) -> dict:
    """One routine step, written the way a user's YAML would hold it."""
    step: dict[str, Any] = {}
    if service:
        step["service"] = service
    if target is not None:
        step["target"] = dict(target)
    if data is not None:
        step["data"] = dict(data)
    if optional:
        step["optional"] = True
    if announce:
        step["announce"] = announce
    return step


_EVERY_ENTITY = {"entity_id": "all"}
_EVERY_AREA = {"area_id": "all"}
_SHADES = {"device_class": "shade"}

# The routines a home gets with no nova_routines.yaml. The scenes are
# optional: a home that has scene.nighttime, scene.morning or scene.welcome
# gets it applied.
DEFAULT_ROUTINES: dict[str, list[dict]] = {
    "goodnight": [
        _step("light.turn_off", target=_EVERY_ENTITY,
              announce="Turning off lights, {honorific}."),
        _step("lock.lock", target=_EVERY_AREA, announce="Locking up."),
        _step("cover.close_cover", target=_SHADES, announce="Closing the shades."),
        _step("scene.turn_on", data={"entity_id": "scene.nighttime"}, optional=True),
        _step(announce="Goodnight, {honorific}."),
    ],
    "goodmorning": [
        _step("cover.open_cover", target=_SHADES,
              announce="Opening the shades, {honorific}."),
        _step("scene.turn_on", data={"entity_id": "scene.morning"}, optional=True),
        _step(announce="Good morning, {honorific}. I hope you slept well."),
    ],
    "leaving": [
        _step("light.turn_off", target=_EVERY_ENTITY,
              announce="Shutting everything down, {honorific}."),
        _step("climate.set_preset_mode", data={"preset_mode": "eco"},
              target=_EVERY_ENTITY, optional=True),
        _step("lock.lock", target=_EVERY_AREA),
        _step(announce="Locked up. Have a good one, {honorific}."),
    ],
    "arriving": [
        _step("scene.turn_on", data={"entity_id": "scene.welcome"}, optional=True,
              announce="Welcome home, {honorific}."),
        _step("climate.set_preset_mode", data={"preset_mode": "home"},
              target=_EVERY_ENTITY, optional=True),
    ],
}


def _load_routines() -> dict[str, list[dict]]:
    """The defaults with the user's nova_routines.yaml laid over them; the
    defaults alone when there is no file or it cannot be read."""
    path = _routine_file()
    if not os.path.exists(path):
        return DEFAULT_ROUTINES
    try:
        with open(path) as fh:
            mine = yaml.safe_load(fh) or {}
    except Exception as exc:
        _LOGGER.warning("Nova: could not parse %s: %s", path, exc)
        return DEFAULT_ROUTINES
    if not isinstance(mine, dict):
        return DEFAULT_ROUTINES
    _LOGGER.debug("Nova: loaded %d user routines", len(mine))
    return {**DEFAULT_ROUTINES, **mine}


def _audit_targets(steps: list[dict]) -> list[dict]:
    """One Actions log row per step that calls a service; speech only steps
    are not actions."""
    rows = []
    for index, step in enumerate(steps):
        service = step.get("service")
        if not service:
            continue
        domain, name = service.split(".", 1) if "." in service else ("", service)
        rows.append({"key": index, "domain": domain or None, "service": name or None})
    return rows


async def async_run_routine(hass: HomeAssistant, call: ServiceCall, honorific: str,
                            tts_entity: str | None, speakers: list[str]) -> dict:
    """The nova.routine service: run the named routine step by step."""
    name: str = call.data["name"].lower().strip()
    routines = _load_routines()
    if name not in routines:
        _LOGGER.warning("Nova: unknown routine '%s'", name)
        # With no honorific (nobody in particular home) there is no ", sir".
        addr = f", {honorific}" if honorific else ""
        await async_announce(hass, f"I don't know a routine called {name}{addr}.",
                             tts_entity, speakers)
        return {"success": False, "error": "unknown_routine"}

    steps = routines[name]
    _LOGGER.info("Nova: running routine '%s' (%d steps)", name, len(steps))

    from . import action_log
    request_id = action_log.new_request_id()
    user_id = getattr(getattr(call, "context", None), "user_id", None)
    # All the rows for this run in one write, each then updated by its step.
    targets = _audit_targets(steps)
    row_ids = await hass.async_add_executor_job(lambda: action_log.start_many(
        request_id, f"routine:{name}", "routine", targets,
        requested_by_user_id=user_id))

    errors: list[str] = []
    executed = 0
    for index, step in enumerate(steps):
        try:
            ran = await _run_step(hass, step, name, index, row_ids.get(index), honorific,
                                  tts_entity, speakers, errors)
            executed += ran
        except Exception as exc:
            _LOGGER.warning("Nova: routine step error: %s", exc)
            errors.append(str(exc))
    return {"success": not errors, "routine": name, "steps_executed": executed,
            "errors": errors}


def _step_entity(data) -> str:
    """The first entity a step names, for the confirmation question."""
    named = data.get("entity_id", "") if isinstance(data, dict) else ""
    if isinstance(named, list):
        return named[0] if named else ""
    return named


async def _log(hass, call) -> None:
    await hass.async_add_executor_job(call)


async def _run_step(hass, step: dict, routine: str, index: int, row_id, honorific: str,
                    tts_entity, speakers, errors: list) -> int:
    """Say and do one step; 1 when its service call went out. Problems are
    added to ``errors`` unless the step is optional."""
    from . import action_log, policy

    said = step.get("announce")
    if said:
        from .directive_helper import fill_honorific
        await async_announce(hass, fill_honorific(said, honorific), tts_entity, speakers,
                             context="welcome" if routine == "arriving" else "routine")

    service = step.get("service")
    if not service:
        return 0
    domain, svc = service.split(".", 1)
    data = step.get("data", {})
    target = step.get("target") or None
    optional = step.get("optional")

    # A step can be as sensitive as any direct command (good morning opens
    # covers; a user's routine could unlock, disarm, or run a scene or
    # script nobody can see into), so each step goes through the gate. A step
    # may target an area or a class rather than one entity, so it is one
    # confirmation per step, named after the first entity in its data.
    request = policy.AuthorityRequest(domain, svc, _step_entity(data),
                                      label=service.replace(".", " "))
    if not policy.authorize_now(hass, request).allowed:
        await _log(hass, lambda: action_log.mark_awaiting_approval(row_id))
        decision = await policy.authorize(hass, request)
        approval = decision.approval
        await _log(hass, lambda: action_log.set_approval(
            row_id, approval, approval_required=(approval != "not_required")))
        if not decision.allowed and decision.already_secure:
            # Already locked, closed or armed (8.46.0): nothing to send.
            await _log(hass, lambda: action_log.set_execution(
                row_id, "blocked", reason_code="already_secure"))
            return 0
        if not decision.allowed:
            note = decision.note
            if optional:
                _LOGGER.debug("Nova: optional step '%s' needs confirmation, skipped: %s",
                              service, note)
            else:
                _LOGGER.warning("Nova: step '%s' needs confirmation, not run: %s",
                                service, note)
                errors.append(f"{service}: {note or 'confirmation required'}")
            code = decision.code or "confirmation_not_approved"
            await _log(hass, lambda: action_log.set_execution(row_id, "blocked",
                                                              reason_code=code))
            return 0

    try:
        await hass.services.async_call(domain, svc, data, target=target, blocking=True)
    except Exception as exc:
        if optional:
            _LOGGER.debug("Nova: optional step '%s' skipped: %s", service, exc)
        else:
            _LOGGER.warning("Nova: step '%s' failed: %s", service, exc)
            errors.append(f"{service}: {exc}")
        await _log(hass, lambda: action_log.set_execution(
            row_id, "failed", reason_code="service_call_failed"))
        return 0
    await _log(hass, lambda: action_log.set_execution(row_id, "accepted"))
    if domain in ("scene", "script", "automation"):
        await _log(hass, lambda: action_log.mark_not_checkable(row_id))
    return 1


def list_routines() -> list[str]:
    """The names of every routine available."""
    return list(_load_routines())
