"""Resolve and send Nova's normal push notifications."""
from __future__ import annotations

import json
import logging
import re
from typing import Any


_LOGGER = logging.getLogger(__name__)

CONF_NOTIFY_SERVICES = "notify_services"
CONF_NOTIFY_SERVICE = "notify_service"
_NOTIFY_SERVICE_RE = re.compile(r"notify\.[a-z0-9_]+\Z")


def configured_notify_services(config: dict | None) -> list[str]:
    """Return ordered, unique normal notification services.

    ``notify_services`` is authoritative when present.  The legacy singular
    setting remains a fallback so existing installations keep their target.
    """
    config = config if isinstance(config, dict) else {}
    raw: Any = config.get(CONF_NOTIFY_SERVICES)
    if raw is None:
        raw = config.get(CONF_NOTIFY_SERVICE, "")

    if isinstance(raw, str):
        value = raw.strip()
        if value.startswith("["):
            try:
                raw = json.loads(value)
            except (TypeError, ValueError):
                raw = []
        else:
            raw = [value] if value else []

    if not isinstance(raw, (list, tuple)):
        return []

    services: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        service = item.strip()
        if not _NOTIFY_SERVICE_RE.fullmatch(service) or service in seen:
            continue
        domain, name = service.split(".", 1)
        if not name:
            continue
        seen.add(service)
        services.append(f"{domain}.{name}")
    return services


async def async_send_configured_notifications(
    hass,
    config: dict | None,
    payload: dict,
    *,
    action: str = "notify",
    source: str = "proactive",
    request_id: str | None = None,
    entity_id: str | None = None,
    requested_state: str | None = None,
    requested_by_user_id: str | None = None,
) -> list[str]:
    """Send one payload to every selected service, isolating failures."""
    effective_config = dict(config or {})
    from .runtime import domain_runtime_config
    runtime = domain_runtime_config(hass)
    if CONF_NOTIFY_SERVICE in runtime:
        effective_config[CONF_NOTIFY_SERVICE] = runtime[CONF_NOTIFY_SERVICE]
    if CONF_NOTIFY_SERVICES in runtime:
        effective_config[CONF_NOTIFY_SERVICES] = runtime[CONF_NOTIFY_SERVICES]
    services = configured_notify_services(effective_config)
    if not services:
        return []

    from . import action_log

    if request_id is None:
        request_id = action_log.new_request_id()
    targets = []
    for target in services:
        domain, service = target.split(".", 1)
        targets.append({
            "key": target,
            "domain": domain,
            "service": service,
            "entity_id": entity_id,
            "requested_state": requested_state,
        })
    action_ids = await hass.async_add_executor_job(
        lambda: action_log.start_many(
            request_id,
            action,
            source,
            targets,
            requested_by_user_id=requested_by_user_id,
        )
    )

    sent: list[str] = []
    for target in services:
        domain, service = target.split(".", 1)
        action_id = action_ids.get(target)
        try:
            await hass.services.async_call(
                domain, service, dict(payload), blocking=False)
            sent.append(target)
            if action_id is not None:
                await hass.async_add_executor_job(
                    lambda row_id=action_id: action_log.set_execution(
                        row_id, "accepted")
                )
        except Exception as exc:
            _LOGGER.warning("Notification via %s failed: %s", target, exc)
            if action_id is not None:
                await hass.async_add_executor_job(
                    lambda row_id=action_id: action_log.set_execution(
                        row_id, "failed", reason_code="service_call_failed")
                )
    return sent
