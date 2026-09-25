"""Device control: every Home Assistant actuation the agent performs, behind
Nova's policy gate and action audit."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from homeassistant.core import HomeAssistant

from .home import _exec_get_area_devices

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


# ── Tool execution ──────────────────────────────────────────────────────────

async def _exec_control_device(hass: HomeAssistant, args: dict, device_id: Optional[str] = None) -> str:
    """Execute a device control action.

    Phase 3 status contract (alongside the existing `success`/error shape,
    never replacing it): `status` is "verified" (a bounded synchronous check
    confirmed the result — light/switch/fan turn_on/turn_off/toggle, and
    light set_brightness), "accepted" (the call completed but confirmation
    isn't available yet or wasn't attempted — covers, locks, other domains,
    set_temperature, volume_set), or "unverified" (the bounded fast check
    ran and did NOT confirm the result — toggle and set_brightness only,
    since those are the two actions explicitly excluded from the existing
    retrying `_verify_control`: a delayed real toggle plus an automatic
    retry-toggle could reverse the outcome, and `_verify_control` has no
    brightness-attribute awareness at all). A raised exception is always
    `status: "error"`, `success: False` — unchanged from before, error
    handling was never optimistic.
    """
    entity_id = args.get("entity_id", "")
    action = args.get("action", "")
    value = args.get("value")

    state = hass.states.get(entity_id)
    if not state:
        return json.dumps({"error": f"Entity '{entity_id}' not found", "status": "error"})

    domain = entity_id.split(".")[0]
    svc_data = {"entity_id": entity_id}
    pre_state = state.state
    fname = state.attributes.get("friendly_name", entity_id)

    # Action Audit Log (top-level boundary: this tool call IS the user's/
    # system's intended action — it owns request_id for everything it does,
    # including the confirmation gate and the background verifier it may
    # schedule; neither of those creates its own row).
    from ... import action_log
    from ...voice_confirm import is_voice_satellite_device
    request_id = action_log.new_request_id()
    log_source = "voice" if is_voice_satellite_device(hass, device_id or "") else "chat"
    action_id = await hass.async_add_executor_job(
        lambda: action_log.start(
            request_id, "control_device", log_source,
            request_device_id=device_id or None,
            domain=domain, entity_id=entity_id, requested_state=str(value) if value is not None else action,
        )
    )

    try:
        action_map = {
            "turn_on":  (domain, "turn_on"),
            "turn_off": (domain, "turn_off"),
            "toggle":   (domain, "toggle"),
            "lock":     ("lock", "lock"),
            "unlock":   ("lock", "unlock"),
            "open":     ("cover", "open_cover"),
            "close":    ("cover", "close_cover"),
            "media_play":  ("media_player", "media_play"),
            "media_pause": ("media_player", "media_pause"),
            "media_next":  ("media_player", "media_next_track"),
            "volume_up":   ("media_player", "volume_up"),
            "volume_down": ("media_player", "volume_down"),
        }

        status = "accepted"
        message = None
        requested_pct = None

        if action == "set_brightness":
            requested_pct = int(value or 50)
            svc_data["brightness_pct"] = requested_pct
            await hass.services.async_call("light", "turn_on", svc_data, blocking=True)
        elif action == "set_temperature":
            svc_data["temperature"] = float(value or 72)
            await hass.services.async_call("climate", "set_temperature", svc_data, blocking=True)
        elif action == "volume_set":
            svc_data["volume_level"] = (value or 50) / 100.0
            await hass.services.async_call("media_player", "volume_set", svc_data, blocking=True)
        elif action in action_map:
            svc_domain, svc_name = action_map[action]
            # Authorization gate (v7.41.0). Protected actions (lock/unlock,
            # garage, disarm) are voice-confirmed when that's enabled, and the
            # gate FAILS CLOSED: if the confirmation path errors, the action
            # does NOT run (previously it fell through and executed unconfirmed).
            # The row is created as "awaiting" before this call (the corrected
            # order) since we don't yet know whether confirm_gate will find
            # this protected at all — its own "not protected" answer is a
            # legitimate exit from "awaiting", not a re-entry into it.
            await hass.async_add_executor_job(
                lambda: action_log.mark_awaiting_approval(action_id)
            )
            from ... import policy
            ok, note, approval_result = await policy.confirm_gate(
                hass, svc_domain, svc_name, entity_id, action.replace("_", " "),
                device_id=device_id or "")
            await hass.async_add_executor_job(
                lambda: action_log.set_approval(
                    action_id, approval_result,
                    approval_required=(approval_result != "not_required"),
                )
            )
            if not ok:
                await hass.async_add_executor_job(
                    lambda: action_log.set_execution(action_id, "blocked", reason_code="confirmation_not_approved")
                )
                return json.dumps({
                    "status": "awaiting_confirmation",
                    "entity_id": entity_id,
                    "message": note or f"Confirmation required before {action} on {entity_id}.",
                })
            await hass.services.async_call(svc_domain, svc_name, svc_data, blocking=True)
        else:
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(action_id, "failed", reason_code="unknown_action")
            )
            return json.dumps({"error": f"Unknown action: {action}", "status": "error"})

        # The service call above succeeded (no exception raised) — the
        # execution outcome moves from pending to accepted now; verification
        # (below, synchronous or later via _verify_control) is a separate,
        # follow-up update to the SAME row, never a new one.
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "accepted")
        )

        # ── Fast-domain synchronous verification (Phase 3) ──────────────
        # Gated strictly by domain — a turn_on/turn_off/toggle on any domain
        # OTHER than light/switch/fan never reaches this branch, falling
        # through to the existing background-only path below unchanged.
        from ... import entity_verify

        if action in ("turn_on", "turn_off") and domain in entity_verify.FAST_VERIFY_DOMAINS:
            expected = _EXPECTED_STATES[action][0]
            verified = await entity_verify.wait_until(
                lambda: entity_verify.check_state_once(hass, entity_id, expected))
            if verified:
                status = "verified"
                message = f"I've {'turned on' if action == 'turn_on' else 'turned off'} {fname}."
                await hass.async_add_executor_job(
                    lambda: action_log.set_execution(action_id, "verified")
                )
            else:
                status = "unverified"
                message = (f"I sent the command to {action.replace('_', ' ')} {fname}, "
                            f"but I can't confirm it worked yet.")
                v_dom, v_svc = action_map[action]
                hass.async_create_task(
                    _verify_control(hass, entity_id, action, v_dom, v_svc, svc_data,
                                    action_id=action_id))
        elif action == "toggle" and domain in entity_verify.FAST_VERIFY_DOMAINS:
            expected = {"on": "off", "off": "on"}.get(pre_state)
            if expected is None:
                status = "accepted"
                message = f"I've sent the toggle command to {fname}."
            else:
                verified = await entity_verify.wait_until(
                    lambda: entity_verify.check_state_once(hass, entity_id, expected))
                if verified:
                    status = "verified"
                    message = f"I've toggled {fname}."
                    await hass.async_add_executor_job(
                        lambda: action_log.set_execution(action_id, "verified")
                    )
                else:
                    status = "unverified"
                    message = (f"I sent the toggle command to {fname}, but I can't "
                                f"confirm it worked yet.")
                    # Never sent to _verify_control: a delayed successful toggle
                    # plus an automatic retry could reverse the outcome.
                    await entity_verify.record_unverified(
                        hass, entity_id, "toggle", source="agent",
                        detail=", not retried automatically because repeating "
                               "toggle could reverse a delayed successful action")
                    await hass.async_add_executor_job(
                        lambda: action_log.set_execution(
                            action_id, "unverified",
                            reason_code="not_retried_toggle_could_reverse")
                    )
        elif action == "set_brightness":
            verified = await entity_verify.wait_until(
                lambda: entity_verify.check_brightness_once(hass, entity_id, requested_pct))
            if verified:
                status = "verified"
                message = f"I've set {fname} to {requested_pct}%."
                await hass.async_add_executor_job(
                    lambda: action_log.set_execution(action_id, "verified")
                )
            else:
                status = "unverified"
                message = (f"I sent the command to set {fname} to {requested_pct}%, "
                            f"but I can't confirm the brightness reached that level yet.")
                # Never sent to _verify_control: it only validates entity
                # state and cannot confirm the requested brightness attribute.
                await entity_verify.record_unverified(
                    hass, entity_id, "set_brightness", source="agent",
                    detail=f", requested {requested_pct}%, not retried "
                           "automatically because the background verifier "
                           "cannot validate the requested brightness level")
                await hass.async_add_executor_job(
                    lambda: action_log.set_execution(
                        action_id, "unverified",
                        reason_code="brightness_not_confirmed")
                )
        elif action in action_map and action in _EXPECTED_STATES:
            # Existing background-only path — covers, locks, and turn_on/
            # turn_off/toggle on any domain outside light/switch/fan.
            # Unchanged: reports accepted now, verified/logged later.
            v_dom, v_svc = action_map[action]
            hass.async_create_task(
                _verify_control(hass, entity_id, action, v_dom, v_svc, svc_data,
                                action_id=action_id))
            message = f"I've sent the command to {action.replace('_', ' ')} {fname}."
        else:
            # set_temperature, volume_set, media_play/pause/next, volume_up/
            # down — no verification attempted this phase (unchanged).
            message = f"I've sent the {action.replace('_', ' ')} command to {fname}."

        # Get updated state
        new_state = hass.states.get(entity_id)

        return json.dumps({
            "success": True,
            "status": status,
            "message": message,
            "entity_id": entity_id,
            "previous_state": pre_state,
            "new_state": new_state.state if new_state else "unknown",
            "action": action,
        })
    except Exception as exc:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "failed", reason_code="service_call_failed")
        )
        return json.dumps({
            "error": f"Failed: {exc}", "status": "error", "success": False,
            "entity_id": entity_id,
        })


async def _exec_run_scene_script(hass: HomeAssistant, args: dict) -> str:
    """Activate a scene or script."""
    entity_id = args.get("entity_id", "")
    domain = entity_id.split(".")[0] if "." in entity_id else ""

    if domain not in ("scene", "script", "automation"):
        return json.dumps({"error": f"Not a scene/script/automation: {entity_id}"})

    svc = "turn_on" if domain in ("scene", "script") else "trigger"

    # Authorization gate (audit fix, Sept 2026): a scene/script/automation's
    # contents are opaque to this tool — it could unlock a door, disarm the
    # alarm, or open a cover just as easily as turn on a light. policy.py
    # rates any actuating call on these domains MEDIUM by risk, and
    # voice_confirm already treats them as protected the same way; this was
    # the one call site that never actually consulted either, so the
    # classification had no enforcement point. Same fail-closed gate as
    # every other actuation path.
    from ... import action_log
    # Note: this tool's dispatch signature carries no device_id, so voice vs.
    # chat can't be distinguished here the way _exec_control_device does —
    # a known, disclosed limitation rather than a guess; source stays "chat"
    # until device_id is threaded into this call site.
    request_id = action_log.new_request_id()
    label = entity_id.split(".", 1)[-1].replace("_", " ").strip()
    action_id = await hass.async_add_executor_job(
        lambda: action_log.start(
            request_id, "run_scene_or_script", "chat",
            domain=domain, service=svc, entity_id=entity_id,
        )
    )

    from ... import policy
    await hass.async_add_executor_job(
        lambda: action_log.mark_awaiting_approval(action_id)
    )
    ok_gate, gate_note, approval_result = await policy.confirm_gate(
        hass, domain, svc, entity_id, f"activate {label}")
    await hass.async_add_executor_job(
        lambda: action_log.set_approval(
            action_id, approval_result,
            approval_required=(approval_result != "not_required"),
        )
    )
    if not ok_gate:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "blocked", reason_code="confirmation_not_approved")
        )
        return json.dumps({
            "status": "awaiting_confirmation",
            "entity_id": entity_id,
            "message": gate_note or f"Confirmation required before activating {entity_id}.",
        })

    try:
        await hass.services.async_call(domain, svc, {"entity_id": entity_id}, blocking=True)
        # HA exposes no reliable "did the scene/script/automation finish"
        # state -- accepted only, never a completion claim.
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "accepted")
        )
        return json.dumps({
            "success": True, "status": "accepted", "entity_id": entity_id,
            "action": "activated",
            "message": f"I've triggered {label}.",
        })
    except Exception as exc:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "failed", reason_code="service_call_failed")
        )
        return json.dumps({"error": str(exc), "status": "error", "success": False,
                            "entity_id": entity_id})


async def _exec_bulk_control(hass: HomeAssistant, args: dict, device_id: Optional[str] = None) -> str:
    """Control multiple devices in a domain/area."""
    domain = args.get("domain", "")
    action = args.get("action", "")
    area_name = args.get("area_name")

    entities = []
    if area_name:
        # Get area-specific entities
        result = await _exec_get_area_devices(hass, {"area_name": area_name})
        area_data = json.loads(result)
        if "devices" in area_data:
            entities = [
                d["entity_id"] for d in area_data["devices"]
                if d["domain"] == domain
            ]
    else:
        entities = [
            s.entity_id for s in hass.states.async_all(domain)
        ]

    # Filter based on action (don't turn off already-off things)
    if action == "turn_off":
        entities = [e for e in entities if (hass.states.get(e) or type("", (), {"state": ""})()).state == "on"]
    elif action in ("lock",):
        entities = [e for e in entities if (hass.states.get(e) or type("", (), {"state": ""})()).state == "unlocked"]

    from ... import policy, action_log
    from ...voice_confirm import is_voice_satellite_device
    request_id = action_log.new_request_id()
    log_source = "voice" if is_voice_satellite_device(hass, device_id or "") else "chat"

    svc_map = {
        "turn_on": (None, "turn_on"), "turn_off": (None, "turn_off"),
        "lock": ("lock", "lock"), "unlock": ("lock", "unlock"),
        "open": ("cover", "open_cover"), "close": ("cover", "close_cover"),
    }

    # One batch insert for the whole request (v3 correction: not one 250ms
    # logging attempt per target) — the policy-skip classification is cheap
    # and synchronous, so it's known before this single transaction, letting
    # every target (attempted or policy-blocked) land in the same insert.
    plan: list[tuple[str, str, str, bool]] = []  # (eid, sd, sn, will_attempt)
    targets_for_log: list[dict] = []
    for eid in entities:
        svc_domain = eid.split(".")[0]
        if action not in svc_map:
            continue
        sd = svc_map[action][0] or svc_domain
        sn = svc_map[action][1]
        will_block = policy.requires_confirmation(hass, sd, sn, eid, device_id=device_id or "")
        plan.append((eid, sd, sn, not will_block))
        if will_block:
            targets_for_log.append({
                "key": eid, "domain": sd, "service": sn, "entity_id": eid,
                "approval_required": True, "approval_result": "not_requested",
                "execution_result": "blocked",
                "reason_code": "confirmation_unavailable_in_bulk",
                "reason_text": "requires per-device confirmation; skipped in bulk control",
            })
        else:
            targets_for_log.append({
                "key": eid, "domain": sd, "service": sn, "entity_id": eid,
            })

    row_ids = await hass.async_add_executor_job(
        lambda: action_log.start_many(
            request_id, "bulk_control", log_source, targets_for_log,
            request_device_id=device_id or None,
        )
    )

    success = 0
    blocked = 0
    failed: list[dict] = []
    for eid, sd, sn, will_attempt in plan:
        row_id = row_ids.get(eid)
        if not will_attempt:
            blocked += 1
            continue
        try:
            await hass.services.async_call(sd, sn, {"entity_id": eid}, blocking=False)
        except Exception as exc:
            # Phase 3: an immediate service-call exception is now recorded,
            # not silently swallowed.
            failed.append({"entity_id": eid, "error": str(exc)})
            await hass.async_add_executor_job(
                lambda rid=row_id: action_log.set_execution(rid, "failed", reason_code="service_call_failed")
            )
            continue
        success += 1
        await hass.async_add_executor_job(
            lambda rid=row_id: action_log.set_execution(rid, "accepted")
        )
        # Successful calls schedule the SAME existing per-entity background
        # verifier single-entity actions already use — every action in
        # svc_map has a known expected state, so this always applies. Fire-
        # and-forget: never delays this response. It updates THIS row via
        # action_id, never creates a new one (ownership rule).
        if action in _EXPECTED_STATES:
            hass.async_create_task(
                _verify_control(hass, eid, action, sd, sn, {"entity_id": eid},
                                action_id=row_id))

    total = len(entities)
    verb = action.replace("_", " ")

    # Nothing actually ran -- success/status must reflect that honestly,
    # not report success=true for a bulk call that changed nothing.
    if total and success == 0:
        if failed and not blocked:
            status = "error"
        elif blocked and not failed:
            status = "awaiting_confirmation"
        else:
            # a mix of blocked and failed, still nothing succeeded
            status = "error"
        parts = []
        if failed:
            parts.append(f"{len(failed)} failed")
        if blocked:
            parts.append(f"{blocked} blocked")
        result = {
            "success": False,
            "status": status,
            "message": (f"I wasn't able to {verb} any of the {total} device(s) "
                        f"({', '.join(parts)})."),
            "action": action,
            "domain": domain,
            "area": area_name,
            "count": success,
            "total": total,
        }
        if blocked:
            result["blocked"] = blocked
        if failed:
            result["failed"] = failed
        return json.dumps(result)

    message = f"I've sent the {verb} command to {success} device(s)."
    if failed or blocked:
        parts = []
        if failed:
            parts.append(f"{len(failed)} failed")
        if blocked:
            parts.append(f"{blocked} blocked")
        message += f" ({', '.join(parts)})."

    result = {
        "success": True,
        "status": "accepted",
        "message": message,
        "action": action,
        "domain": domain,
        "area": area_name,
        "count": success,
        "total": total,
    }
    if blocked:
        result["blocked"] = blocked
        result["note"] = (f"{blocked} protected device(s) not changed in bulk; "
                          f"confirm each individually.")
    if failed:
        result["failed"] = failed
    return json.dumps(result)


# Domains execute_plan may target (v7.87.0, backlog #3). Unlike
# control_device/bulk_control — which can only ever reach a small hardcoded
# action_map (turn_on/lock/open/etc.) and so are structurally incapable of
# calling anything else — execute_plan takes domain/service straight from the
# LLM's own plan JSON with nothing constraining it. The tool's own schema
# description already scopes it to "a device action" (light, climate, lock,
# media_player, cover, switch, ...); this makes that scoping an actual gate
# instead of just a hint. The old "does the entity_id exist" check caught
# almost nothing here: a domain-level service like homeassistant.restart or
# shell_command.* ignores its entity_id argument entirely, so any real
# entity_id in the house would satisfy that check while doing something the
# tool was never meant to do.
_EXECUTE_PLAN_ALLOWED_DOMAINS = {
    "light", "switch", "climate", "cover", "lock", "media_player", "fan",
    "humidifier", "vacuum", "alarm_control_panel", "scene", "script",
    "automation", "input_boolean", "input_number", "input_select",
    "input_text", "input_datetime", "input_button", "water_heater", "valve",
    "siren", "number", "select", "button", "lawn_mower", "remote", "notify",
    "timer", "todo",
}


async def _exec_execute_plan(hass: HomeAssistant, args: dict, device_id: Optional[str] = None) -> str:
    """
    Execute a multi-step plan (v5.9.07).

    Runs each step's service call in order, collecting per-step results so the
    agent can report what succeeded and what didn't. This turns a high-level
    goal into one coordinated, inspectable operation rather than many
    independent tool round-trips.

    Restricted to _EXECUTE_PLAN_ALLOWED_DOMAINS (v7.87.0) — a domain outside
    that set is rejected before the entity/confirmation checks even run, so a
    plan can't reach system-level services (restart, shell_command, backup,
    etc.) no matter what entity_id it names.
    """
    goal = args.get("goal", "the requested plan")
    steps = args.get("steps", [])
    if not steps:
        return json.dumps({"error": "no steps provided", "goal": goal})

    from ... import action_log
    from ...voice_confirm import is_voice_satellite_device
    request_id = action_log.new_request_id()
    log_source = "voice" if is_voice_satellite_device(hass, device_id or "") else "chat"
    # One batch insert for the whole plan (v3 correction) — every step gets
    # a placeholder row up front (pending/not_required defaults); each
    # step's own processing below updates its own row via its pre-assigned
    # id, exactly like a single control_device call would.
    step_targets = [
        {"key": i, "domain": step.get("domain") or None,
         "service": step.get("service") or None,
         "entity_id": step.get("entity_id") or None}
        for i, step in enumerate(steps)
    ]
    row_ids = await hass.async_add_executor_job(
        lambda: action_log.start_many(
            request_id, "execute_plan", log_source, step_targets,
            request_device_id=device_id or None,
        )
    )

    results = []
    succeeded = 0
    for i, step in enumerate(steps):
        row_id = row_ids.get(i)
        domain = step.get("domain", "")
        service = step.get("service", "")
        entity_id = step.get("entity_id", "")
        extra = step.get("service_data", {}) or {}
        desc = step.get("description", f"{service} {entity_id}")

        if not domain or not service or not entity_id:
            results.append({"step": i + 1, "description": desc,
                            "ok": False, "error": "missing domain/service/entity_id"})
            await hass.async_add_executor_job(
                lambda rid=row_id: action_log.set_execution(rid, "failed", reason_code="incomplete_step")
            )
            continue

        # Domain allowlist (v7.87.0) — checked before entity existence on
        # purpose: a domain-level service (homeassistant.restart,
        # shell_command.*, backup.create, ...) ignores its entity_id anyway,
        # so an existing-but-irrelevant entity_id must never be enough to
        # reach it.
        if domain not in _EXECUTE_PLAN_ALLOWED_DOMAINS:
            results.append({"step": i + 1, "description": desc, "ok": False,
                            "error": f"domain '{domain}' is not allowed in a plan "
                                     f"— execute_plan is for device actions "
                                     f"(lights, climate, locks, covers, media, "
                                     f"switches, and similar), not system-level "
                                     f"services"})
            await hass.async_add_executor_job(
                lambda rid=row_id: action_log.set_execution(rid, "failed", reason_code="domain_not_allowed")
            )
            continue

        # Verify entity exists before acting
        if hass.states.get(entity_id) is None:
            results.append({"step": i + 1, "description": desc,
                            "ok": False, "error": f"entity '{entity_id}' not found"})
            await hass.async_add_executor_job(
                lambda rid=row_id: action_log.set_execution(rid, "failed", reason_code="entity_not_found")
            )
            continue

        # Authorization gate (v7.41.0): a protected step is confirmed before it
        # runs, and fails closed if confirmation errors.
        from ... import policy
        await hass.async_add_executor_job(
            lambda rid=row_id: action_log.mark_awaiting_approval(rid)
        )
        ok_gate, gate_note, approval_result = await policy.confirm_gate(
            hass, domain, service, entity_id, service.replace("_", " "),
            device_id=device_id or "")
        await hass.async_add_executor_job(
            lambda rid=row_id, ar=approval_result: action_log.set_approval(
                rid, ar, approval_required=(ar != "not_required"))
        )
        if not ok_gate:
            results.append({"step": i + 1, "description": desc,
                            "ok": False, "error": gate_note or "confirmation required"})
            await hass.async_add_executor_job(
                lambda rid=row_id: action_log.set_execution(rid, "blocked", reason_code="confirmation_not_approved")
            )
            continue

        try:
            await hass.services.async_call(
                domain, service,
                {"entity_id": entity_id, **extra},
                blocking=True,
            )
            succeeded += 1
            # No reliable final state for a generic plan step (this is also
            # the only reachable path to alarm_control_panel — the existing
            # confirm_gate above still applies, and nothing here adds a
            # retry or re-attempt for it): accepted only, never a completion
            # claim, regardless of domain.
            results.append({"step": i + 1, "description": desc, "ok": True,
                            "status": "accepted"})
            await hass.async_add_executor_job(
                lambda rid=row_id: action_log.set_execution(rid, "accepted")
            )
        except Exception as exc:
            results.append({"step": i + 1, "description": desc,
                            "ok": False, "status": "error", "error": str(exc)})
            await hass.async_add_executor_job(
                lambda rid=row_id: action_log.set_execution(rid, "failed", reason_code="service_call_failed")
            )

    return json.dumps({
        "goal": goal,
        "status": "accepted" if succeeded else "error",
        # "accepted" means the service calls were sent, not that HA finished
        # carrying them out (no reliable per-step completion signal exists
        # for a generic plan) -- never claim "completed" here.
        "message": f"I've sent {succeeded} of {len(steps)} step(s) for {goal}.",
        "total_steps": len(steps),
        "succeeded": succeeded,
        "failed": len(steps) - succeeded,
        "results": results,
    })


# ── Verify-after-act (v6.38) ─────────────────────────────────────────────────
# Fire-and-forget control is not agentic: after a deterministic action, Nova
# checks the device actually reached the target, retries once if it didn't, and
# logs honestly if it still hasn't. Silent on success; visible on failure.

VERIFY_DELAY_SECS = 4.0


_VERIFY_SLEEP = asyncio.sleep    # module-level seam so tests can fast-forward


# action -> acceptable end states (transitional states get one extra wait)
_EXPECTED_STATES = {
    "turn_on":  ("on",),
    "turn_off": ("off",),
    "lock":     ("locked",),
    "unlock":   ("unlocked",),
    "open":     ("open",),
    "close":    ("closed",),
}


_TRANSITIONAL = ("opening", "closing", "locking", "unlocking")


def _state_ok(hass: HomeAssistant, entity_id: str, expected: tuple) -> Optional[bool]:
    st = hass.states.get(entity_id)
    if st is None:
        return None
    s = str(st.state).lower()
    if s in _TRANSITIONAL:
        return None            # still moving — check again
    return s in expected


async def _verify_control(hass: HomeAssistant, entity_id: str, action: str,
                          svc_domain: str, svc_name: str, svc_data: dict,
                          *, source: str = "agent",
                          action_id: Optional[int] = None) -> None:
    """Confirm a control action landed; one retry; honest report on failure.

    `source` records which caller scheduled this verification (agent.py's
    tool-calling path defaults to "agent"; local_engine.py's fast path must
    pass source="local_engine") so the resulting activity entries attribute
    correctly rather than always reading "agent".

    `action_id` (Action Audit Log): a verification retry is supporting work
    for the action its CALLER already created a row for — it updates that
    same row via set_execution(), it never creates a new one (see
    action_log.py's ownership rule). Safe to omit: set_execution() no-ops
    on action_id=None, exactly like every other action_log mutator."""
    expected = _EXPECTED_STATES.get(action)
    if not expected:
        return
    from ... import action_log
    try:
        await _VERIFY_SLEEP(VERIFY_DELAY_SECS)
        ok = _state_ok(hass, entity_id, expected)
        if ok is None:                       # transitional / unknown — grace period
            await _VERIFY_SLEEP(VERIFY_DELAY_SECS)
            ok = _state_ok(hass, entity_id, expected)
        if ok:
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(action_id, "verified")
            )
            return                           # first-try success stays silent
        _LOGGER.info("verify: %s not %s after %s — retrying once",
                     entity_id, "/".join(expected), action)
        await hass.services.async_call(svc_domain, svc_name, dict(svc_data),
                                       blocking=True)
        await _VERIFY_SLEEP(VERIFY_DELAY_SECS)
        ok = _state_ok(hass, entity_id, expected)
        from ... import database
        if ok:
            database.save_activity(
                entity_id=entity_id, category="verify", urgency="low",
                message=f"{entity_id} needed a second attempt to {action} — "
                        f"succeeded on retry.", source=source)
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(
                    action_id, "verified", reason_code="verified_on_retry")
            )
        else:
            st = hass.states.get(entity_id)
            database.save_activity(
                entity_id=entity_id, category="verify", urgency="medium",
                message=f"{entity_id} did not respond to {action} "
                        f"(state: {st.state if st else 'unknown'}) even after a "
                        f"retry — it may be jammed, obstructed, or offline.",
                source=source)
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(
                    action_id, "unverified", reason_code="no_response_after_retry")
            )
    except Exception as exc:
        _LOGGER.debug("verify_control failed for %s: %s", entity_id, exc)
