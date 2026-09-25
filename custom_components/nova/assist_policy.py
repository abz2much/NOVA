"""Assist policy bridge — Nova's policy gate for Home Assistant Assist tools.

Nova's own device tools (control_device, bulk_control, run_scene_or_script,
execute_plan) pass every actuation through ``policy.confirm_gate``. The main
agent is also offered Home Assistant's generic Assist tools (HassTurnOn,
HassTurnOff, script tools, ...), which ``agent._execute_tool`` runs through
``hass_api.async_call_tool``. Before this module existed that path never
touched the gate, so an exposed lock could be unlocked, or a cover opened,
without Nova's confirmation or its voice-origin rule.

This module sits in front of that call. It does not replace it: once Nova
authorizes an operation, the caller still invokes the SAME ``hass_api``
instance, so Home Assistant keeps its original LLM context, user context,
exposed-entity filtering and permission checks. Nova's gate is an extra
layer on top.

Two parts:

* ``classify_operation`` — pure. Given a description of the Home Assistant
  tool (taken from the tool object, never from the model's text) plus the
  entities it would act on, returns what kind of operation it is and which
  ``(domain, service, entity_id)`` actions it maps to in Nova policy terms.
  Risk comes from ``policy.classify``; there is no second risk table here.
* ``async_authorize`` — looks the tool up in ``hass_api.tools``, resolves
  its targets with Home Assistant's own matcher, classifies, and runs
  ``policy.confirm_gate`` for every target action. Anything malformed,
  unresolvable or unrecognized as safe is refused before Home Assistant is
  called (fail closed).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

# ── Operation kinds ──────────────────────────────────────────────────────────
READ_ONLY = "read_only"
SAFE_MUTATION = "safe_mutation"            # mutates, but every target is low risk
PROTECTED_MUTATION = "protected_mutation"  # at least one target is above low risk
UNKNOWN_MUTATION = "unknown_mutation"      # may mutate; can't be classified safely
MALFORMED = "malformed"                    # arguments don't form a valid call

_BLOCKED_KINDS = {UNKNOWN_MUTATION, MALFORMED}

# ── Tool descriptor kinds (what the Home Assistant tool object is) ──────────
TOOL_INTENT = "intent"
TOOL_ACTION = "action"          # llm.ActionTool: calls <domain>.<action>
TOOL_SCRIPT = "script"          # llm.ScriptTool: runs a script
TOOL_READ_ONLY = "read_only"    # a known read-only tool class, or annotated read-only
TOOL_UNKNOWN = "unknown"

# Home Assistant's own read-only tool classes. Older cores don't carry
# ToolAnnotations, so these are recognized by class; newer cores also
# annotate them read_only, which is honoured below.
_READ_ONLY_TOOL_CLASSES = {
    "GetLiveContextTool", "GetDateTimeTool",
    "CalendarGetEventsTool", "TodoGetItemsTool",
}

# Intents that only read or answer — no state change.
_READ_ONLY_INTENTS = {
    "HassGetState", "HassGetWeather", "HassClimateGetTemperature",
    "HassGetCurrentDate", "HassGetCurrentTime", "HassTimerStatus",
    "HassNevermind", "HassRespond", "HassShoppingListLastItems",
}

# Intents that change state but never actuate a device: device timers, list
# items, and a spoken broadcast. Nothing here can open or unlock anything.
_NON_DEVICE_INTENTS = {
    "HassStartTimer", "HassCancelTimer", "HassCancelAllTimers",
    "HassIncreaseTimer", "HassDecreaseTimer", "HassPauseTimer",
    "HassUnpauseTimer", "HassListAddItem", "HassListCompleteItem",
    "HassShoppingListAddItem", "HassShoppingListCompleteItem",
    "HassBroadcast",
}

# Generic on/off intents. Home Assistant's handler reports a generic
# homeassistant.turn_on/turn_off, then special-cases the real service by
# the target's domain inside the call itself (on = lock / open, off =
# unlock / close). That mapping is reproduced explicitly below.
_ON_OFF_INTENTS = {
    "HassTurnOn": "turn_on",
    "HassTurnOff": "turn_off",
    "HassToggle": "toggle",
}

_SET_POSITION_INTENT = "HassSetPosition"

# Convenience intents whose Home Assistant handler can only act on one
# fixed domain. Used when the handler doesn't expose its own targets.
_FIXED_DOMAIN_INTENTS = {
    "HassLightSet": "light",
    "HassClimateSetTemperature": "climate",
    "HassHumidifierSetpoint": "humidifier",
    "HassHumidifierMode": "humidifier",
    "HassFanSetSpeed": "fan",
    "HassMediaPause": "media_player",
    "HassMediaUnpause": "media_player",
    "HassMediaNext": "media_player",
    "HassMediaPrevious": "media_player",
    "HassMediaSearchAndPlay": "media_player",
    "HassSetVolume": "media_player",
    "HassSetVolumeRelative": "media_player",
    "HassMediaPlayerMute": "media_player",
    "HassMediaPlayerUnmute": "media_player",
    "HassVacuumStart": "vacuum",
    "HassVacuumReturnToBase": "vacuum",
    "HassLawnMowerStartMowing": "lawn_mower",
    "HassLawnMowerDock": "lawn_mower",
}

_ALARM_DOMAIN = "alarm_control_panel"
# Alarm services that are unambiguous. Anything else aimed at an alarm panel
# might be a disarm, so it's refused rather than guessed at.
_ALARM_KNOWN_SERVICES = {
    "alarm_disarm", "alarm_arm_away", "alarm_arm_home", "alarm_arm_night",
    "alarm_arm_vacation", "alarm_arm_custom_bypass", "alarm_trigger",
}


@dataclass(frozen=True)
class ToolDescriptor:
    """What a Home Assistant tool object is, read from the object itself."""
    kind: str
    tool_name: str
    intent_type: str = ""
    domain: str = ""        # ActionTool / ScriptTool domain
    action: str = ""        # ActionTool / ScriptTool action
    entity_id: str = ""     # ScriptTool's script entity, when known
    resolvable: bool = False   # handler resolves entity targets (name/area/floor)
    annotated_read_only: bool = False


@dataclass(frozen=True)
class AssistTarget:
    """One Nova policy action an Assist operation maps to."""
    domain: str
    service: str
    entity_id: str = ""
    risk: str = "low"
    reason: str = ""


@dataclass(frozen=True)
class AssistClassification:
    kind: str
    tool_name: str
    operation: str                       # intent type, or "<domain>.<action>"
    targets: tuple = ()                  # tuple[AssistTarget, ...]
    reason: str = ""

    @property
    def mutating(self) -> bool:
        return self.kind != READ_ONLY


@dataclass(frozen=True)
class AssistDecision:
    """Outcome of ``async_authorize``. ``allowed`` False means the tool must
    not be passed to Home Assistant; ``tool_result()`` is what the model sees."""
    allowed: bool
    classification: AssistClassification
    approval_result: str = "not_required"
    note: str = ""
    request_id: Optional[str] = None
    action_ids: tuple = ()               # action_log row ids, if recorded

    def tool_result(self) -> dict:
        c = self.classification
        if c.kind == MALFORMED:
            status = "invalid_request"
        elif c.kind == UNKNOWN_MUTATION:
            status = "blocked_unclassified"
        else:
            status = "awaiting_confirmation"
        return {
            "status": status,
            "tool": c.tool_name,
            "message": self.note or c.reason or "Nova's safety policy blocked this action.",
        }


# ── Pure classification ─────────────────────────────────────────────────────

def _policy_target(domain: str, service: str, entity_id: str = "") -> AssistTarget:
    from . import policy
    risk, reason = policy.classify(domain, service, entity_id)
    return AssistTarget(domain, service, entity_id, risk, reason)


def _map_on_off(domain: str, verb: str) -> Optional[tuple]:
    """Real (domain, service) Home Assistant's on/off handler calls for a
    target in ``domain``. ``None`` = can't be mapped safely (alarm panels).
    Toggle on a lock, cover or valve maps to the guard-dropping direction:
    the current state isn't a safe basis for assuming the harmless one."""
    if domain == "lock":
        return ("lock", "lock" if verb == "turn_on" else "unlock")
    if domain == "cover":
        return ("cover", "close_cover" if verb == "turn_off" else "open_cover")
    if domain == "valve":
        return ("valve", "close_valve" if verb == "turn_off" else "open_valve")
    if domain in ("button", "input_button"):
        return (domain, "press")
    if domain == _ALARM_DOMAIN:
        return None
    return (domain, verb)


def classify_operation(
    descriptor: ToolDescriptor,
    tool_args: Any,
    target_entities: Optional[list] = None,
) -> AssistClassification:
    """Classify one Assist tool call. Pure: no ``hass``, no I/O.

    ``target_entities`` is what Home Assistant's matcher resolved for an
    entity-targeting intent (a superset of what the handler could act on):
    a list of ``(entity_id, handler_mapping)`` pairs, where
    ``handler_mapping`` is the ``(domain, service)`` the intent handler
    itself reports for that entity, or ``None`` if it reported nothing.
    ``target_entities`` is ``None`` when targets couldn't be resolved.
    """
    d = descriptor
    name = d.tool_name

    if d.kind == TOOL_INTENT:
        it = d.intent_type
        op = it
        if it in _READ_ONLY_INTENTS:
            return AssistClassification(READ_ONLY, name, op)
        if not isinstance(tool_args, dict):
            return AssistClassification(MALFORMED, name, op, reason="arguments are not an object")
        if it in _NON_DEVICE_INTENTS:
            return AssistClassification(SAFE_MUTATION, name, op,
                                        reason="timer, list or broadcast only")

        if it in _ON_OFF_INTENTS or it == _SET_POSITION_INTENT or d.resolvable:
            if target_entities is None:
                return AssistClassification(
                    UNKNOWN_MUTATION, name, op,
                    reason="couldn't identify which devices this would affect")
            if not target_entities:
                return AssistClassification(
                    UNKNOWN_MUTATION, name, op, reason="no matching device found")
            targets = []
            for item in target_entities:
                eid, reported = item if isinstance(item, tuple) else (item, None)
                dom = str(eid).split(".", 1)[0]
                if it in _ON_OFF_INTENTS:
                    mapped = _map_on_off(dom, _ON_OFF_INTENTS[it])
                elif it == _SET_POSITION_INTENT:
                    mapped = _map_set_position(dom, tool_args.get("position"))
                    if mapped == "malformed":
                        return AssistClassification(
                            MALFORMED, name, op, reason="position is not a number from 0 to 100")
                else:
                    mapped = _map_reported(reported, dom)
                if mapped is None:
                    if dom == _ALARM_DOMAIN:
                        return AssistClassification(
                            UNKNOWN_MUTATION, name, op,
                            targets=(AssistTarget(_ALARM_DOMAIN, "alarm_disarm", eid,
                                                  "critical", "ambiguous alarm operation"),),
                            reason="ambiguous alarm operation; it could disarm the alarm")
                    return AssistClassification(
                        UNKNOWN_MUTATION, name, op,
                        reason=f"couldn't map {it} on {eid} to a known action")
                targets.append(_policy_target(mapped[0], mapped[1], eid))
            return _from_targets(name, op, targets)

        fixed = _FIXED_DOMAIN_INTENTS.get(it)
        if fixed:
            return _from_targets(name, op, [_policy_target(fixed, _intent_service(it))])
        if d.annotated_read_only:
            return AssistClassification(READ_ONLY, name, op)
        return AssistClassification(UNKNOWN_MUTATION, name, op,
                                    reason=f"{it} isn't a recognized Assist operation")

    if d.kind in (TOOL_ACTION, TOOL_SCRIPT):
        op = f"{d.domain}.{d.action}"
        if not d.domain or not d.action:
            return AssistClassification(UNKNOWN_MUTATION, name, op or name,
                                        reason="action tool without a domain or action")
        if tool_args is not None and not isinstance(tool_args, dict):
            return AssistClassification(MALFORMED, name, op, reason="arguments are not an object")
        if d.kind == TOOL_SCRIPT:
            eid = d.entity_id or f"script.{d.action}"
            return _from_targets(name, op, [_policy_target("script", "turn_on", eid)],
                                 indirect=True)
        if d.domain == _ALARM_DOMAIN and d.action not in _ALARM_KNOWN_SERVICES:
            return AssistClassification(
                UNKNOWN_MUTATION, name, op,
                targets=(AssistTarget(_ALARM_DOMAIN, "alarm_disarm", "", "critical",
                                      "ambiguous alarm operation"),),
                reason="ambiguous alarm operation; it could disarm the alarm")
        ents = _entity_ids_from_args(tool_args)
        if ents is None:
            return AssistClassification(MALFORMED, name, op, reason="entity_id is not valid")
        if not ents:
            return _from_targets(name, op, [_policy_target(d.domain, d.action)])
        return _from_targets(name, op, [_policy_target(d.domain, d.action, e) for e in ents])

    if d.kind == TOOL_READ_ONLY:
        return AssistClassification(READ_ONLY, name, name)

    return AssistClassification(UNKNOWN_MUTATION, name, name,
                                reason="unrecognized Assist tool")


def _from_targets(name: str, op: str, targets: list, indirect: bool = False) -> AssistClassification:
    protected = indirect or any(t.risk != "low" for t in targets)
    return AssistClassification(PROTECTED_MUTATION if protected else SAFE_MUTATION,
                                name, op, targets=tuple(targets))


def _map_set_position(domain: str, position: Any):
    try:
        pos = int(position)
    except (TypeError, ValueError):
        return "malformed"
    if isinstance(position, bool) or not 0 <= pos <= 100:
        return "malformed"
    if domain == "cover":
        # Moving a cover to any open position opens it.
        return ("cover", "open_cover" if pos > 0 else "close_cover")
    if domain == "valve":
        return ("valve", "set_valve_position")
    return None


def _map_reported(reported: Any, domain: str) -> Optional[tuple]:
    """(domain, service) for a resolvable intent handler other than on/off,
    from what the handler itself reported for the target."""
    if not (isinstance(reported, tuple) and len(reported) == 2
            and all(isinstance(x, str) and x for x in reported)):
        return None
    dom, svc = reported
    if dom == "homeassistant":
        dom = domain
    if dom == _ALARM_DOMAIN and svc not in _ALARM_KNOWN_SERVICES:
        return None
    return (dom, svc)


def _intent_service(intent_type: str) -> str:
    """A readable service label for a fixed-domain convenience intent."""
    base = intent_type[4:] if intent_type.startswith("Hass") else intent_type
    out = []
    for ch in base:
        if ch.isupper() and out:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _entity_ids_from_args(args: Any) -> Optional[list]:
    if not isinstance(args, dict) or "entity_id" not in args:
        return []
    raw = args["entity_id"]
    if isinstance(raw, str):
        vals = [v.strip() for v in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        vals = list(raw)
    else:
        return None
    if not all(isinstance(v, str) and "." in v for v in vals):
        return None
    return vals


# ── Home Assistant tool inspection ──────────────────────────────────────────

def _unwrap(tool: Any) -> Any:
    """The real tool behind a NamespacedTool wrapper (newer cores)."""
    inner = tool
    for _ in range(4):
        wrapped = getattr(inner, "tool", None)
        if wrapped is None or wrapped is inner or not hasattr(wrapped, "name"):
            break
        inner = wrapped
    return inner


def _intent_type_of(inner: Any) -> str:
    """The intent a Home Assistant IntentTool dispatches. Newer cores store
    it as ``intent_type``; older cores (2026.2) dispatch ``intent_type=self.name``
    from IntentTool.async_call, so for that class the tool object's own name
    IS the intent it runs."""
    it = getattr(inner, "intent_type", None)
    if isinstance(it, str) and it:
        return it
    if any(c.__name__ == "IntentTool" for c in type(inner).__mro__):
        name = getattr(inner, "name", None)
        if isinstance(name, str) and name:
            return name
    return ""


def describe_tool(tool: Any, handler: Any = None) -> ToolDescriptor:
    """Build a descriptor from a Home Assistant llm.Tool object (and, for an
    intent tool, its registered intent handler)."""
    inner = _unwrap(tool)
    name = str(getattr(tool, "name", "") or "")
    annotations = getattr(inner, "annotations", None)
    ro = bool(getattr(annotations, "read_only", False)) if annotations is not None else False
    cls = type(inner).__name__

    intent_type = _intent_type_of(inner)
    if intent_type:
        # Only a service-style handler (DynamicServiceIntentHandler) resolves
        # entity targets from name/area/floor slots and reports the service
        # it calls per entity.
        resolvable = bool(handler is not None and hasattr(handler, "get_domain_and_service")
                          and hasattr(handler, "async_validate_slots"))
        return ToolDescriptor(TOOL_INTENT, name, intent_type=intent_type,
                              resolvable=resolvable, annotated_read_only=ro)
    if cls in _READ_ONLY_TOOL_CLASSES or ro:
        return ToolDescriptor(TOOL_READ_ONLY, name, annotated_read_only=ro)
    domain = getattr(inner, "_domain", None)
    action = getattr(inner, "_action", None)
    if isinstance(domain, str) and isinstance(action, str):
        kind = TOOL_SCRIPT if (cls == "ScriptTool" or domain == "script") else TOOL_ACTION
        return ToolDescriptor(kind, name, domain=domain, action=action)
    return ToolDescriptor(TOOL_UNKNOWN, name)


def _script_entity_id(hass, action: str) -> str:
    """Entity id of the script a ScriptTool runs. The tool's action is the
    script's registry unique_id when it has one, else its object id."""
    try:
        from homeassistant.helpers import entity_registry as er
        eid = er.async_get(hass).async_get_entity_id("script", "script", action)
        if eid:
            return eid
    except Exception as exc:
        _LOGGER.debug("assist_policy: script entity lookup failed: %s", exc)
    return f"script.{action}"


def _find_tool(hass_api: Any, tool_name: str) -> Any:
    for tool in (getattr(hass_api, "tools", None) or []):
        if getattr(tool, "name", None) == tool_name:
            return tool
    return None


def _intent_helper():
    """Home Assistant's intent helper module (a seam for tests)."""
    from homeassistant.helpers import intent
    return intent


def _find_handler(hass, intent_type: str) -> Any:
    try:
        for handler in _intent_helper().async_get(hass):
            if getattr(handler, "intent_type", None) == intent_type:
                return handler
    except Exception as exc:
        _LOGGER.debug("assist_policy: intent handler lookup failed: %s", exc)
    return None


def _resolve_targets(hass, handler: Any, tool_args: dict, assistant: Optional[str]):
    """Entity ids Home Assistant's own matcher finds for these slots.

    Mirrors the handler's own constraints (slot validation, name/area/floor,
    domain and device_class filters, required features/states, Assist
    exposure) but WITHOUT area/floor preferences and with duplicate names
    allowed, so the result is a superset of what the handler could act on.
    Returns ``"malformed"`` if HA rejects the slots, ``None`` if targets
    can't be resolved, else the list of entity ids.
    """
    intent = _intent_helper()
    blank = getattr(intent, "is_blank_slot_value", None)
    slots = {k: {"value": v} for k, v in tool_args.items()
             if not (blank(v) if callable(blank) else v in (None, ""))}
    try:
        slots = handler.async_validate_slots(slots)
    except Exception:
        return "malformed"
    try:
        name = slots.get("name", {}).get("value")
        if name == "all":
            name = None
        domains = getattr(handler, "required_domains", None)
        if "domain" in slots:
            domains = set(slots["domain"]["value"])
        device_classes = None
        if "device_class" in slots:
            device_classes = set(slots["device_class"]["value"])
        constraints = intent.MatchTargetsConstraints(
            name=name,
            area_name=slots.get("area", {}).get("value"),
            floor_name=slots.get("floor", {}).get("value"),
            domains=domains,
            device_classes=device_classes,
            assistant=assistant,
            features=getattr(handler, "required_features", None),
            states=getattr(handler, "required_states", None),
            allow_duplicate_names=True,
        )
        if not constraints.has_constraints:
            return "malformed"
        result = intent.async_match_targets(hass, constraints)
    except Exception as exc:
        _LOGGER.debug("assist_policy: target resolution failed: %s", exc)
        return None
    if not getattr(result, "is_match", False):
        return []
    out = []
    for state in (getattr(result, "states", None) or []):
        try:
            reported = tuple(handler.get_domain_and_service(None, state))
        except Exception:
            reported = None
        out.append((state.entity_id, reported))
    return out


# ── Authorization ────────────────────────────────────────────────────────────

async def async_authorize(
    hass,
    hass_api: Any,
    tool_name: str,
    tool_args: Any,
    *,
    device_id: str = "",
    user_id: Optional[str] = None,
) -> AssistDecision:
    """Decide whether an Assist tool call may be passed to Home Assistant.

    Never raises: any internal failure on a call that may mutate denies it.
    """
    try:
        return await _authorize(hass, hass_api, tool_name, tool_args,
                                device_id=device_id or "", user_id=user_id)
    except Exception as exc:
        _LOGGER.warning("assist_policy: authorization failed for %s (%s); denying",
                        tool_name, exc)
        c = AssistClassification(UNKNOWN_MUTATION, str(tool_name), str(tool_name),
                                 reason="safety check failed")
        return AssistDecision(False, c, "error",
                              "Nova's safety check failed, so this action wasn't run.")


async def _authorize(hass, hass_api, tool_name, tool_args, *, device_id, user_id):
    tool = _find_tool(hass_api, tool_name)
    if tool is None:
        c = AssistClassification(UNKNOWN_MUTATION, str(tool_name), str(tool_name),
                                 reason="that tool isn't available")
        return await _finish(hass, c, False, "not_required", c.reason, device_id, user_id)

    intent_type = _intent_type_of(_unwrap(tool))
    handler = _find_handler(hass, intent_type) if intent_type else None
    desc = describe_tool(tool, handler)

    targets = None
    if (desc.kind == TOOL_INTENT and handler is not None and isinstance(tool_args, dict)
            and desc.resolvable and desc.intent_type not in _READ_ONLY_INTENTS
            and desc.intent_type not in _NON_DEVICE_INTENTS):
        llm_context = getattr(hass_api, "llm_context", None)
        assistant = getattr(llm_context, "assistant", None)
        targets = _resolve_targets(hass, handler, tool_args, assistant)
        if targets == "malformed":
            c = AssistClassification(MALFORMED, desc.tool_name, desc.intent_type,
                                     reason="Home Assistant rejected these arguments")
            return await _finish(hass, c, False, "not_required", c.reason, device_id, user_id)
    elif desc.kind == TOOL_SCRIPT:
        desc = ToolDescriptor(TOOL_SCRIPT, desc.tool_name, domain=desc.domain,
                              action=desc.action,
                              entity_id=_script_entity_id(hass, desc.action))

    c = classify_operation(desc, tool_args, targets)
    if c.kind == READ_ONLY:
        return AssistDecision(True, c)
    if c.kind in _BLOCKED_KINDS:
        return await _finish(hass, c, False, "not_required",
                             _blocked_note(c), device_id, user_id)

    from . import policy
    approval = "not_required"
    for t in c.targets:
        ok, note, result = await policy.confirm_gate(
            hass, t.domain, t.service, t.entity_id,
            t.service.replace("_", " "), device_id=device_id)
        if result != "not_required":
            approval = result
        if not ok:
            return await _finish(hass, c, False, result,
                                 note or f"Confirmation required before {t.domain}.{t.service}.",
                                 device_id, user_id)
    return await _finish(hass, c, True, approval, "", device_id, user_id)


def _blocked_note(c: AssistClassification) -> str:
    if c.kind == MALFORMED:
        return f"That request wasn't valid ({c.reason}), so it wasn't run."
    return (f"I couldn't safely check that action ({c.reason}), "
            f"so Nova's safety policy didn't run it.")


async def _finish(hass, c, allowed, approval, note, device_id, user_id) -> AssistDecision:
    """Record the decision (log line + action_log rows) and build the result."""
    _LOGGER.info(
        "assist_policy: tool=%s op=%s kind=%s targets=%s approval=%s execute=%s",
        c.tool_name, c.operation, c.kind,
        json.dumps([f"{t.domain}.{t.service}:{t.entity_id or '-'}:{t.risk}"
                    for t in c.targets]),
        approval, allowed)
    request_id = None
    ids: tuple = ()
    if c.targets or not allowed:
        request_id, ids = await _record(hass, c, allowed, approval, device_id, user_id)
    return AssistDecision(allowed, c, approval, note, request_id, ids)


async def _record(hass, c, allowed, approval, device_id, user_id):
    try:
        from . import action_log
        from .voice_confirm import is_voice_satellite_device
        source = "voice" if device_id and is_voice_satellite_device(hass, device_id) else "chat"
        request_id = action_log.new_request_id()
        reason_code = None if allowed else f"assist_{c.kind}" if c.kind in _BLOCKED_KINDS \
            else "confirmation_not_approved"
        rows = [{
            "key": i,
            "domain": t.domain, "service": t.service, "entity_id": t.entity_id or None,
            "approval_required": approval != "not_required",
            "approval_result": approval,
            "execution_result": "pending" if allowed else "blocked",
            "reason_code": reason_code,
        } for i, t in enumerate(c.targets)] or [{
            "key": 0, "domain": None, "service": None, "entity_id": None,
            "approval_required": False, "approval_result": approval,
            "execution_result": "blocked", "reason_code": reason_code,
        }]
        ids = await hass.async_add_executor_job(
            lambda: action_log.start_many(
                request_id, f"assist:{c.operation}", source, rows,
                requested_by_user_id=user_id, request_device_id=device_id or None))
        return request_id, tuple((ids or {}).values())
    except Exception as exc:
        _LOGGER.debug("assist_policy: action log write failed: %s", exc)
        return None, ()


async def async_record_execution(hass, decision: AssistDecision, succeeded: bool) -> None:
    """Mark the decision's action_log rows accepted/failed after the call."""
    if not decision.action_ids:
        return
    try:
        from . import action_log
        outcome = "accepted" if succeeded else "failed"
        for aid in decision.action_ids:
            await hass.async_add_executor_job(
                lambda a=aid: action_log.set_execution(a, outcome))
    except Exception as exc:
        _LOGGER.debug("assist_policy: action log update failed: %s", exc)
