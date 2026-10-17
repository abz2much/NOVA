"""Device control: every Home Assistant actuation the agent performs, behind
Nova's policy gate and action audit."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from homeassistant.core import HomeAssistant

from ...safe_errors import tool_error_text
from ..presentation import display_name
from .home import area_entity_ids

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")

# What each control_device action calls; None means the entity's own domain.
_CONTROL_SERVICES = {
    "turn_on": (None, "turn_on"), "turn_off": (None, "turn_off"),
    "toggle": (None, "toggle"),
    "lock": ("lock", "lock"), "unlock": ("lock", "unlock"),
    "open": ("cover", "open_cover"), "close": ("cover", "close_cover"),
    "media_play": ("media_player", "media_play"),
    "media_pause": ("media_player", "media_pause"),
    "media_next": ("media_player", "media_next_track"),
    "volume_up": ("media_player", "volume_up"),
    "volume_down": ("media_player", "volume_down"),
}
# What runs each kind of runnable entity.
_RUNNABLE = {"scene": "turn_on", "script": "turn_on", "automation": "trigger"}


# ── Tool execution ──────────────────────────────────────────────────────────

async def _exec_control_device(hass: HomeAssistant, args: dict, device_id: Optional[str] = None,
                               user_id: Optional[str] = None) -> str:
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
    # The name a person hears or reads (friendly name, with the area when
    # another entity shares it); service calls and policy keep entity_id.
    fname = display_name(hass, entity_id)

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
            requested_by_user_id=user_id or None,
            request_device_id=device_id or None,
            domain=domain, entity_id=entity_id, requested_state=str(value) if value is not None else action,
        )
    )

    try:
        action_map = {name: (svc_dom or domain, svc)
                      for name, (svc_dom, svc) in _CONTROL_SERVICES.items()}

        status = "accepted"
        message: Optional[str] = None
        requested_pct = None
        svc_domain: Optional[str]
        svc_name: Optional[str]

        # Every action — the valued ones (brightness, temperature, volume)
        # included — resolves to one service and passes the same
        # authorization gate before it runs, so a per-entity protection
        # override applies whatever the action.
        # 0 is a real value, never "not given" (8.7.20): brightness 0 and
        # volume 0 used to become 50%.
        if action == "set_brightness":
            requested_pct = int(value if value is not None else 50)
            svc_data["brightness_pct"] = requested_pct
            svc_domain, svc_name = "light", "turn_on"
        elif action == "set_temperature":
            # A missing target is refused (8.7.20); it used to become 72 in
            # whatever unit the home uses.
            if value is None or value == "":
                await hass.async_add_executor_job(
                    lambda: action_log.set_execution(action_id, "failed",
                                                     reason_code="missing_value")
                )
                return json.dumps({
                    "error": f"No target temperature was given for {fname}. "
                             f"Ask what temperature to set; nothing was changed.",
                    "status": "error", "success": False, "entity_id": entity_id,
                })
            svc_data["temperature"] = float(value)
            svc_domain, svc_name = "climate", "set_temperature"
        elif action == "volume_set":
            svc_data["volume_level"] = (value if value is not None else 50) / 100.0
            svc_domain, svc_name = "media_player", "volume_set"
        elif action in action_map:
            svc_domain, svc_name = action_map[action]
        else:
            svc_domain = svc_name = None

        if svc_domain is not None and svc_name is not None:
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
            decision = await policy.authorize(hass, policy.AuthorityRequest(
                svc_domain, svc_name, entity_id, device_id=device_id or "",
                user_id=user_id or "", label=action.replace("_", " "),
                target_name=display_name(hass, entity_id)))
            ok, note, approval_result = decision.allowed, decision.note, decision.approval
            await hass.async_add_executor_job(
                lambda: action_log.set_approval(
                    action_id, approval_result,
                    approval_required=(approval_result != "not_required"),
                )
            )
            if not ok and decision.code and approval_result == "not_required":
                # Stopped by the live check (8.46.0): already secure, or the
                # device or its door sensor cannot be read. Nothing is sent.
                await hass.async_add_executor_job(
                    lambda: action_log.set_execution(action_id, "blocked", reason_code=decision.code)
                )
                return json.dumps({
                    "success": decision.already_secure,
                    "status": "already_secure" if decision.already_secure else "refused",
                    "entity_id": entity_id,
                    "message": decision.note,
                })
            if not ok:
                await hass.async_add_executor_job(
                    lambda: action_log.set_execution(action_id, "blocked", reason_code="confirmation_not_approved")
                )
                return json.dumps({
                    "status": "awaiting_confirmation",
                    "entity_id": entity_id,
                    "message": note or (f"Confirmation required before "
                                        f"{action.replace('_', ' ')} on {display_name(hass, entity_id)}."),
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
            toggle_to = {"on": "off", "off": "on"}.get(pre_state)
            if toggle_to is None:
                status = "accepted"
                message = f"I've sent the toggle command to {fname}."
            else:
                expected = toggle_to
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


def _hold_for_spoken_yes(hass, device_id: Optional[str], approval: str) -> bool:
    """Whether a held request waits for a spoken yes on the next turn: it
    came from a voice satellite and the gate deferred it (never a phone tap,
    a refusal or an error). 8.33.0."""
    if approval != "deferred" or not device_id:
        return False
    try:
        from ...voice_confirm import is_voice_satellite_device
        return bool(is_voice_satellite_device(hass, device_id))
    except Exception:
        return False


async def _exec_run_scene_script(hass: HomeAssistant, args: dict,
                                 device_id: Optional[str] = None,
                                 user_id: Optional[str] = None) -> str:
    """Run a scene or script, or trigger an automation."""
    entity_id = args.get("entity_id", "")
    domain = entity_id.partition(".")[0] if "." in entity_id else ""
    if domain not in _RUNNABLE:
        return json.dumps({"error": f"Not a scene/script/automation: {entity_id}"})
    svc = _RUNNABLE[domain]

    # Authorization gate (audit fix, Sept 2026): a scene/script/automation's
    # contents are opaque to this tool — it could unlock a door, disarm the
    # alarm, or open a cover just as easily as turn on a light. policy.py
    # rates any actuating call on these domains MEDIUM by risk, and
    # voice_confirm already treats them as protected the same way; this was
    # the one call site that never actually consulted either, so the
    # classification had no enforcement point. Same fail-closed gate as
    # every other actuation path.
    from ... import action_log
    from ...voice_confirm import is_voice_satellite_device
    request_id = action_log.new_request_id()
    log_source = "voice" if is_voice_satellite_device(hass, device_id or "") else "chat"
    label = display_name(hass, entity_id)
    action_id = await hass.async_add_executor_job(
        lambda: action_log.start(
            request_id, "run_scene_or_script", log_source,
            requested_by_user_id=user_id or None,
            request_device_id=device_id or None,
            domain=domain, service=svc, entity_id=entity_id,
        )
    )

    from ... import policy
    await hass.async_add_executor_job(
        lambda: action_log.mark_awaiting_approval(action_id)
    )
    decision = await policy.authorize(hass, policy.AuthorityRequest(
        domain, svc, entity_id, device_id=device_id or "", user_id=user_id or "",
        label="activate", target_name=label))
    ok_gate, gate_note, approval_result = decision.allowed, decision.note, decision.approval
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
        # Held for a spoken yes on the satellite the user spoke to (8.33.0):
        # remember what a yes runs, and make the reply end with the question.
        if _hold_for_spoken_yes(hass, device_id, approval_result):
            from ... import pending_confirm
            pending_confirm.put(device_id or "", pending_confirm.KIND_SCENE_SCRIPT,
                                entity_id, user_id)
            return json.dumps({
                "status": "awaiting_confirmation",
                "entity_id": entity_id,
                "message": f"{label} needs a spoken yes first. {pending_confirm.ASK}",
                "ask_user": pending_confirm.ASK,
            })
        return json.dumps({
            "status": "awaiting_confirmation",
            "entity_id": entity_id,
            "message": gate_note or f"Confirmation required before activating {label}.",
        })

    try:
        await hass.services.async_call(domain, svc, {"entity_id": entity_id}, blocking=True)
        # HA exposes no reliable "did the scene/script/automation finish"
        # state -- accepted only, never a completion claim.
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "accepted")
        )
        await hass.async_add_executor_job(
            lambda: action_log.mark_not_checkable(action_id))
        return json.dumps({
            "success": True, "status": "accepted", "entity_id": entity_id,
            "action": "activated",
            "message": f"I've triggered {label}.",
        })
    except Exception as exc:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "failed", reason_code="service_call_failed")
        )
        return json.dumps({"error": tool_error_text(exc, "run_scene_script"),
                            "status": "error", "success": False,
                            "entity_id": entity_id})


async def _exec_run_voice_phrase(hass: HomeAssistant, args: dict, device_id: Optional[str] = None,
                                 user_id: Optional[str] = None) -> str:
    """Run one of the user's own voice phrases (8.32.0), picked by meaning.

    Checked again here, whatever the model saw: the phrase must exist and be
    on, the voice phrases switch must be on, the phrase must allow the AI to
    pick it, and it may never be a shell command. Every target then goes
    through policy.authorize, which asks when it must."""
    import copy
    from ... import nova_config, voice_phrases, voice_phrase_runner
    pid = args.get("phrase_id") if isinstance(args, dict) else None
    if not isinstance(pid, str) or not pid:
        return json.dumps({"error": "phrase_id is required", "status": "error", "success": False})
    cfg = copy.deepcopy(nova_config.get(voice_phrases.CONFIG_KEY))
    if not isinstance(cfg, dict) or cfg.get("enabled", True) is False:
        return json.dumps({"error": "Voice phrases are turned off", "status": "error",
                           "success": False, "phrase_id": pid})
    entry = voice_phrases.find_entry(cfg, pid)
    if entry is None:
        return json.dumps({"error": f"No voice phrase {pid}", "status": "error",
                           "success": False, "phrase_id": pid})
    if entry.get("enabled", True) is False:
        return json.dumps({"error": "That voice phrase is turned off", "status": "error",
                           "success": False, "phrase_id": pid})
    service = str((entry.get("action") or {}).get("service") or "")
    if service.startswith(voice_phrases.SHELL_DOMAIN + "."):
        return json.dumps({"error": "A shell command phrase only runs from the user's own words",
                           "status": "error", "success": False, "phrase_id": pid})
    if entry.get("allow_ai") is not True:
        return json.dumps({"error": "The user has not let the AI pick this phrase",
                           "status": "error", "success": False, "phrase_id": pid})
    outcome = await voice_phrase_runner.run_entry(
        hass, entry, device_id=device_id, user_id=user_id, honorific="", may_ask=True)
    text = outcome.result.text if outcome.result is not None else ""
    if outcome.status == "accepted":
        return json.dumps({"success": True, "status": "accepted", "phrase_id": pid,
                           "action": "ran", "message": text})
    if outcome.status in ("refused", "deferred"):
        if _hold_for_spoken_yes(hass, device_id, outcome.approval):
            from ... import pending_confirm
            pending_confirm.put(device_id or "", pending_confirm.KIND_PHRASE, pid, user_id)
            return json.dumps({"status": "awaiting_confirmation", "phrase_id": pid,
                               "message": f"{entry.get('name')} needs a spoken yes first. "
                                          f"{pending_confirm.ASK}",
                               "ask_user": pending_confirm.ASK})
        return json.dumps({"status": "awaiting_confirmation", "phrase_id": pid,
                           "message": text or f"Confirmation required before running {entry.get('name')}."})
    return json.dumps({"error": text or "The voice phrase failed", "status": "error",
                       "success": False, "phrase_id": pid})


def _reads(hass: HomeAssistant, entity_id: str) -> str:
    st = hass.states.get(entity_id)
    return st.state if st is not None else ""


async def _exec_bulk_control(hass: HomeAssistant, args: dict,
                             device_id: Optional[str] = None,
                             user_id: Optional[str] = None) -> str:
    """One action on every entity of a domain, in one room or everywhere."""
    domain, action = args.get("domain", ""), args.get("action", "")
    area_name = args.get("area_name")
    if area_name:
        # Every entity of the domain in that room, not just the first page.
        entities = area_entity_ids(hass, area_name, domain)
    else:
        entities = [st.entity_id for st in hass.states.async_all(domain)]
    # Only what would change: nothing already off is turned off, nothing
    # already locked is locked.
    only_if = {"turn_off": "on", "lock": "unlocked"}.get(action)
    if only_if:
        entities = [eid for eid in entities if _reads(hass, eid) == only_if]

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
    live_notes: list[str] = []   # what the live check found (8.46.0)
    targets_for_log: list[dict] = []
    for eid in entities:
        svc_domain = eid.split(".")[0]
        if action not in svc_map:
            continue
        sd = svc_map[action][0] or svc_domain
        sn = svc_map[action][1]
        now = policy.authorize_now(hass, policy.AuthorityRequest(
            sd, sn, eid, device_id=device_id or "", user_id=user_id or ""))
        will_block = not now.allowed
        plan.append((eid, sd, sn, not will_block))
        if will_block and now.code:
            live_notes.append(now.note)
            # The live check (8.46.0): already secure, unreadable, or its
            # door is open. Named with the reason; nothing is sent.
            targets_for_log.append({
                "key": eid, "domain": sd, "service": sn, "entity_id": eid,
                "approval_required": now.approval == "deferred",
                "approval_result": ("not_requested" if now.approval == "deferred"
                                    else "not_required"),
                "execution_result": "blocked", "reason_code": now.code,
                "reason_text": now.note,
            })
        elif will_block:
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
            requested_by_user_id=user_id or None,
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
            failed.append({"entity_id": eid, "error": tool_error_text(exc, "bulk_control")})
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
        if live_notes:
            result["checked"] = live_notes
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
    if live_notes:
        result["checked"] = live_notes
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


# service_data may carry settings (brightness, temperature) but never the
# target. The authority check looks at the step's own entity_id, so a second
# target inside service_data would act on something that was never checked.
_PLAN_TARGET_KEYS = ("entity_id", "device_id", "area_id", "floor_id", "label_id")

# Services in an allowed domain that set other entities' states from a
# configuration the authority check cannot read (scene.apply can unlock a
# lock or open a cover inside one call).
_EXECUTE_PLAN_BLOCKED_SERVICES = {("scene", "apply"), ("scene", "create")}


async def _exec_execute_plan(hass: HomeAssistant, args: dict, device_id: Optional[str] = None,
                             user_id: Optional[str] = None) -> str:
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
            requested_by_user_id=user_id or None,
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
        desc = step.get("description") or (
            f"{service.replace('_', ' ')} {display_name(hass, entity_id)}".strip())

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

        if (domain, service) in _EXECUTE_PLAN_BLOCKED_SERVICES:
            results.append({"step": i + 1, "description": desc, "ok": False,
                            "error": f"{domain}.{service} is not allowed in a plan "
                                     "— it can change other devices without each "
                                     "one being checked. Use separate steps instead"})
            await hass.async_add_executor_job(
                lambda rid=row_id: action_log.set_execution(rid, "failed", reason_code="service_not_allowed")
            )
            continue

        if not isinstance(extra, dict) or any(k in extra for k in _PLAN_TARGET_KEYS):
            results.append({"step": i + 1, "description": desc, "ok": False,
                            "error": "service_data cannot name a target "
                                     "(entity_id, device_id, area_id, floor_id or "
                                     "label_id). Put each device in its own step"})
            await hass.async_add_executor_job(
                lambda rid=row_id: action_log.set_execution(rid, "failed", reason_code="target_in_service_data")
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
        decision = await policy.authorize(hass, policy.AuthorityRequest(
            domain, service, entity_id, device_id=device_id or "", user_id=user_id or "",
            label=service.replace("_", " "), target_name=display_name(hass, entity_id)))
        ok_gate, gate_note, approval_result = (
            decision.allowed, decision.note, decision.approval)
        await hass.async_add_executor_job(
            lambda rid=row_id, ar=approval_result: action_log.set_approval(
                rid, ar, approval_required=(ar != "not_required"))
        )
        if not ok_gate:
            if decision.already_secure:
                results.append({"step": i + 1, "description": desc, "ok": True,
                                "note": gate_note})
            else:
                results.append({"step": i + 1, "description": desc,
                                "ok": False, "error": gate_note or "confirmation required"})
            await hass.async_add_executor_job(
                lambda rid=row_id, code=(decision.code or "confirmation_not_approved"):
                action_log.set_execution(rid, "blocked", reason_code=code)
            )
            continue

        try:
            await hass.services.async_call(domain, service, {"entity_id": entity_id, **extra},
                                           blocking=True)
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
            if domain in ("scene", "script", "automation"):
                await hass.async_add_executor_job(
                    lambda rid=row_id: action_log.mark_not_checkable(rid))
        except Exception as exc:
            from ...safe_errors import safe_error_message
            results.append({"step": i + 1, "description": desc,
                            "ok": False, "status": "error",
                            "error": safe_error_message(exc, where="execute_plan", log=True)})
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


# ── checking an action landed ───────────────────────────────────────────────
# After a deterministic action Nova looks again: did the device get there?
# One retry if not, and an honest record if it still has not. Quiet when it
# worked, visible when it did not.

VERIFY_DELAY_SECS = 4.0
_VERIFY_SLEEP = asyncio.sleep    # tests swap this to skip the wait

# The end state each action should reach. A device still moving gets one
# more wait before it is judged.
_EXPECTED_STATES = {action: (state,) for action, state in (
    ("turn_on", "on"), ("turn_off", "off"), ("lock", "locked"),
    ("unlock", "unlocked"), ("open", "open"), ("close", "closed"),
)}
_TRANSITIONAL = ("opening", "closing", "locking", "unlocking")
# Lowering a guard is never retried by the checker (8.7.20).
_NO_RETRY_ACTIONS = ("unlock", "open")


def _state_ok(hass: HomeAssistant, entity_id: str, expected: tuple) -> Optional[bool]:
    """True or False once the device has settled; None while it is still
    moving or cannot be read."""
    st = hass.states.get(entity_id)
    reading = str(st.state).lower() if st is not None else None
    if reading is None or reading in _TRANSITIONAL:
        return None
    return reading in expected


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
        ok = None
        for _wait in range(2):   # a device still moving gets a second look
            await _VERIFY_SLEEP(VERIFY_DELAY_SECS)
            ok = _state_ok(hass, entity_id, expected)
            if ok is not None:
                break
        if ok:
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(action_id, "verified")
            )
            return                           # first-try success stays silent
        if action in _NO_RETRY_ACTIONS:
            # An unlock or an open is never sent a second time on its own
            # (8.7.20): the lock may read locked because someone locked it
            # again on purpose, and repeating the command would undo that.
            st = hass.states.get(entity_id)
            from ... import database
            database.save_activity(
                entity_id=entity_id, category="verify", urgency="medium",
                message=f"{display_name(hass, entity_id)} did not report {action} "
                        f"(state: {st.state if st else 'unknown'}). Not retried "
                        f"automatically, so a deliberate lock or close is never undone.",
                source=source)
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(
                    action_id, "unverified", reason_code="not_retried_could_undo_a_lock"))
            return
        # A retry is Nova acting on its own: only low risk actions are ever
        # repeated without asking (8.24.0).
        from ... import policy
        retry = policy.authorize_now(hass, policy.AuthorityRequest(
            svc_domain, svc_name, entity_id, source=policy.SOURCE_AUTOMATIC))
        if retry.already_secure:
            # It reached the state while the retry was being decided.
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(action_id, "verified"))
            return
        if not retry.allowed:
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(
                    action_id, "unverified",
                    reason_code=retry.code or "retry_not_authorized"))
            return
        _LOGGER.info("verify: %s not %s after %s — retrying once",
                     entity_id, "/".join(expected), action)
        await hass.services.async_call(svc_domain, svc_name, dict(svc_data), blocking=True)
        await _VERIFY_SLEEP(VERIFY_DELAY_SECS)
        ok = _state_ok(hass, entity_id, expected)
        from ... import database
        if ok:
            database.save_activity(
                entity_id=entity_id, category="verify", urgency="low",
                message=f"{display_name(hass, entity_id)} needed a second attempt to {action} — "
                        f"succeeded on retry.", source=source)
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(
                    action_id, "verified", reason_code="verified_on_retry")
            )
        else:
            st = hass.states.get(entity_id)
            database.save_activity(
                entity_id=entity_id, category="verify", urgency="medium",
                message=f"{display_name(hass, entity_id)} did not respond to {action} "
                        f"(state: {st.state if st else 'unknown'}) even after a "
                        f"retry — it may be jammed, obstructed, or offline.",
                source=source)
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(
                    action_id, "unverified", reason_code="no_response_after_retry")
            )
    except Exception as exc:
        _LOGGER.debug("verify_control failed for %s: %s", entity_id, exc)
