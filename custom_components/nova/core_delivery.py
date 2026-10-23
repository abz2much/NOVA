"""Action delivery: announce or push one cognitive action, the phone
notifications, and running a proactive offer's service call.

Moved out of cognitive_core.py unchanged in 8.7.17. cognitive_core.py
still exports every name defined here, as the same object.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from . import core_common as _m_common
from . import core_state as _m_state
from .core_common import _notify_i18n

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


def _live_runtime_config() -> dict:
    """The owning entry's live runtime_config, read on the event loop. {}
    when the core has no owning entry or that entry is not loaded; a loaded
    entry that has lost its runtime raises NovaRuntimeUnavailable. Never
    reads hass.data."""
    entry = _m_state._CORE.entry
    if entry is None:
        return {}
    from .runtime import lifecycle_runtime_config
    return lifecycle_runtime_config(entry)


def habit_key(action: dict) -> str:
    """The action's repeat and habituation label."""
    return action.get("habit_key") or action.get("pattern_key") or action.get("offer_key") or ""


def request_for(config, action: dict, sleeping, *, message: Optional[str] = None,
                reading=None):
    """The delivery request for one core action (8.48.0: shared with the
    convenience path's shadow verdict, so both judge the same request).

    Critical speaks everywhere and is pushed. High is always pushed, spoken
    when the household's rules allow. Below high: a mute, shush, the
    Announcements switch, quiet hours or sleep holds it, and it goes to the
    phones only while nobody is known to be home or with "Notifications
    only" on. A phone only action (the first intrusion alert while
    residents are home) is pushed, not spoken, unless critical. Speaker
    routing still counts live motion, so an intruder's movement while armed
    away is still spoken. The core keeps its own habituation, so the gate
    does not count days again; its habit key is the repeat label. A report
    of what Nova changed itself (the night sweep's locks and covers) always
    reaches the phones, whatever holds the speakers."""
    from . import delivery
    urgency = action.get("urgency", "medium")
    action_type = action.get("type", "unknown")
    high = urgency in ("high", "critical")
    always = high or bool(action.get("reports_change"))
    return delivery.DeliveryRequest(
        source=action_type, message=action.get("message", "") if message is None else message,
        urgency=urgency, entity_id=action.get("entity_id", "") or "", category=action_type,
        push=delivery.PUSH_ALWAYS if always else delivery.PUSH_HIGH,
        phone_only=bool(action.get("phone_only")) and urgency != "critical",
        tts_context="sentinel", config=config, dedup_key=habit_key(action), habit_key="",
        asleep=bool(sleeping), motion_counts=True,
        record_kind=_RECORDED_KINDS.get(action_type, ""), reading=reading)


async def _emit_action(hass, config, action, sleeping, *, reading=None) -> tuple:
    """Announce / push a single cognitive action via the standard routing.
    Returns (delivered, why): whether it was spoken or pushed, and the
    reason it was held, or how it went out."""
    message = action.get("message", "")
    urgency = action.get("urgency", "medium")
    action_type = action.get("type", "unknown")
    notify_all = bool(action.get("notify_all", False))

    # Given three days running, it's normal for this home: the third says so,
    # later ones stay quiet (habituation.py). Emergencies are exempt.
    habit = habit_key(action)
    try:
        from . import habituation
        if habit and not habituation.exempt(
                urgency=urgency, kind=action_type, entity_id=action.get("entity_id", "")):
            if habituation.is_quiet(habit):
                _LOGGER.info("Cognitive action [%s] quiet (normal for this home): %s",
                             action_type, message[:100])
                return False, "normal for this home (given three days running)"
            message = habituation.with_note(habit, message)
            habituation.record(habit, action.get("entity_id", ""))
    except Exception as exc:
        _LOGGER.debug("habituation check failed: %s", exc)

    _m_state._CORE.actions_taken += 1

    _LOGGER.info(
        "Cognitive action [%s] urgency=%s: %s",
        action_type, urgency, message[:100],
    )

    # Set on anticipation alerts: the Decision Record the user can rate.
    decision_id = action.get("decision_id")
    _snap_url = action.get("snapshot_url")

    async def _push(request_id) -> list:
        # The voice announcement and every phone share the one request id.
        if notify_all:
            await _notify_all_devices(hass, config, message, action_type, _snap_url,
                                      request_id=request_id)
            return ["all"]
        # Adaptive awareness: the alert itself carries the rating buttons.
        return list(await _push_notification(
            hass, config, message, action_type, _snap_url,
            request_id=request_id, extra_data=_rating_data(decision_id)) or [])

    # One delivery request (8.45.0); see request_for for the rules.
    from . import delivery
    high = urgency in ("high", "critical")
    out = await delivery.async_deliver(
        hass, request_for(config, action, sleeping, message=message, reading=reading),
        push=_push)

    # A spoken offer can be answered from these rooms (8.39.1).
    if out.spoken:
        try:
            from . import core_runtime
            core_runtime.offer_spoken(action, out.targets)
        except Exception as exc:
            _LOGGER.debug("offer bookkeeping failed: %s", exc)

    # Adaptive awareness: a spoken alert gets a silent phone notification
    # with the rating buttons, so it can be rated too.
    if decision_id is not None and not high and out.spoken and not out.pushed:
        try:
            from . import adaptive_awareness
            await adaptive_awareness.async_send_rating_prompt(
                hass, config, message, decision_id)
        except Exception as exc:
            _LOGGER.debug("rating prompt failed: %s", exc)
    delivered = bool(out.spoken or out.pushed)
    return delivered, out.decision.reason or ("spoken" if out.spoken else
                                              "pushed" if out.pushed else "not delivered")


# Core action types whose delivery is a safety decision of its own (8.23.0).
_RECORDED_KINDS = {
    "intrusion_confirmed": "intrusion_confirmed",
    "intrusion_unresolved": "intrusion_unresolved",
    "freeze_warning": "hazard",
    "freeze_critical": "hazard",
    "critical_hazard": "hazard",
}


def _rating_data(decision_id) -> dict:
    """Adaptive awareness rating buttons for a phone alert, or {}."""
    if decision_id is None:
        return {}
    try:
        from . import adaptive_awareness
        return adaptive_awareness.rating_push_data(decision_id)
    except Exception:
        return {}


async def _push_notification(hass, config, message, action_type, snapshot_url=None,
                              *, request_id=None, extra_data=None):
    """Push notification to phone, with an optional snapshot image (v6.69.0).

    request_id, when given, is the caller's (_emit_action's or
    _notify_all_devices's fallback) — this never mints a second request for
    the same alert."""
    from .notify_targets import async_send_configured_notifications

    data = {"message": message,
            "title": _notify_i18n().title(action_type, _m_common._hass_lang(hass))}
    img_data = dict(_notification_image_data(hass, snapshot_url))
    img_data.update(extra_data or {})
    if img_data:
        data["data"] = img_data
    return await async_send_configured_notifications(
        hass, config, data,
        request_id=request_id, action="cognitive_alert", source="proactive",
        requested_state=action_type,
    )


def _instance_base_url(hass) -> str:
    """The address the companion app can reach this instance on, with no
    trailing slash, or "" when Home Assistant does not say."""
    try:
        return str(hass.config.external_url or hass.config.internal_url or "").rstrip("/")
    except Exception:
        return ""


def _notification_image_data(hass, snapshot_url):
    """The `data` block of a mobile_app notification that attaches an image.
    iOS reads `attachment.url` and Android reads `image`, so both are set and
    either platform shows the snapshot. Returns {} when there is no snapshot."""
    if not snapshot_url:
        return {}
    address = snapshot_url
    try:
        # A /local path is made absolute so the companion app can fetch it
        # when it is away from the home network.
        if address.startswith("/"):
            base = _instance_base_url(hass)
            if base:
                address = base + address
    except Exception:
        pass
    return {"image": address, "attachment": {"url": address}}


async def _notify_all_devices(hass, config, message, action_type, snapshot_url=None,
                               *, request_id=None):
    """Push to EVERY connected device — every `notify.mobile_app_*` service the
    HA companion app registered — plus a persistent notification for confirmed
    intrusions. Attaches a snapshot image when provided (v6.69.0). Falls back to
    the single configured service if no per-device services exist.

    request_id, when given, is _emit_action's — every device target and the
    persistent-notification catch-all below are logged under that ONE shared
    request, not one apiece."""
    from . import action_log
    if request_id is None:
        request_id = action_log.new_request_id()
    title = _notify_i18n().title(action_type, _m_common._hass_lang(hass))
    img_data = _notification_image_data(hass, snapshot_url)
    sent = 0
    try:
        services = hass.services.async_services().get("notify", {})
        names = [n for n in services if n.startswith("mobile_app_")]
    except Exception as exc:
        _LOGGER.debug("Cognitive: enumerate notify services failed: %s", exc)
        names = []

    row_ids: dict = {}
    if names:
        row_ids = await hass.async_add_executor_job(
            lambda: action_log.start_many(
                request_id, "cognitive_alert", "proactive",
                [{"key": n, "domain": "notify", "service": n} for n in names],
            )
        )
    for name in names:
        row_id = row_ids.get(name)
        try:
            payload = {"message": message, "title": title}
            if img_data:
                payload["data"] = img_data
            await hass.services.async_call("notify", name, payload, blocking=False)
            sent += 1
            if row_id is not None:
                await hass.async_add_executor_job(
                    lambda rid=row_id: action_log.set_execution(rid, "accepted")
                )
        except Exception as exc:
            _LOGGER.debug("notify.%s failed: %s", name, exc)
            if row_id is not None:
                await hass.async_add_executor_job(
                    lambda rid=row_id: action_log.set_execution(
                        rid, "failed", reason_code="service_call_failed")
                )

    # Fall back to the configured single service if nothing device-specific fired.
    if sent == 0:
        await _push_notification(hass, config, message, action_type, snapshot_url,
                                  request_id=request_id)

    # Always-visible catch-all for a confirmed intrusion.
    if action_type == "intrusion_confirmed":
        pn_action_id = await hass.async_add_executor_job(
            lambda: action_log.start(
                request_id, "cognitive_alert", "proactive",
                domain="persistent_notification", service="create",
            )
        )
        try:
            await hass.services.async_call(
                "persistent_notification", "create",
                {"message": message, "title": title,
                 "notification_id": "nova_intrusion"},
                blocking=False)
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(pn_action_id, "accepted")
            )
        except Exception:
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(
                    pn_action_id, "failed", reason_code="service_call_failed")
            )


async def _execute_action_data(
    hass, action_data: dict, *,
    request_id: Optional[str] = None, source: str = "proactive",
) -> bool:
    """
    Run the service call of a proactive action.

    action_data looks like
      {"domain": "light", "service": "turn_on",
       "entity_ids": ["light.x", ...], "service_data": {...optional...}}

    The caller (an autonomous tick, or an explicit accept_pending_offer())
    may own the logged action by passing request_id and source. Without a
    request_id one is minted here. Either way this is the only logger of this
    service call, and never both it and the caller.
    """
    if not action_data:
        return False
    entity_ids = action_data.get("entity_ids", [])
    extra = action_data.get("service_data", {}) or {}
    service = action_data.get("service")
    domain = action_data.get("domain")
    if not (domain and service and entity_ids):
        return False
    from . import action_log
    if request_id is None:
        request_id = action_log.new_request_id()
    shown_entities = (", ".join(entity_ids) if isinstance(entity_ids, list)
                      else str(entity_ids))
    action_id = await hass.async_add_executor_job(
        lambda: action_log.start(
            request_id, "proactive_offer_execute", source,
            domain=domain, service=service, entity_id=shown_entities,
        )
    )
    # The one authority check (8.24.0). An offer Nova runs on its own is
    # automatic and may only do low risk things; one the user said yes to is
    # checked like any other request.
    from . import policy
    automatic = source == "proactive_autonomous"
    for eid in (entity_ids if isinstance(entity_ids, list) else [entity_ids]):
        req = policy.AuthorityRequest(
            domain, service, str(eid), label=service.replace("_", " "),
            source=policy.SOURCE_AUTOMATIC if automatic else policy.SOURCE_CHAT)
        decision = (policy.authorize_now(hass, req) if automatic
                    else await policy.authorize(hass, req))
        if not decision.allowed:
            _LOGGER.info("Proactive action %s.%s on %s not authorized: %s",
                         domain, service, eid, decision.note or decision.approval)
            await hass.async_add_executor_job(
                lambda code=(decision.code or "not_authorized"): action_log.set_execution(
                    action_id, "blocked", reason_code=code)
            )
            return False
    try:
        await hass.services.async_call(
            domain, service,
            {"entity_id": entity_ids, **extra},
            blocking=True,
        )
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "accepted")
        )
        return True
    except Exception as exc:
        _LOGGER.warning("Proactive action failed (%s.%s): %s", domain, service, exc)
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(
                action_id, "failed", reason_code="service_call_failed")
        )
        return False

