"""
Nova Automation Creator (v5.6.0).

Registers a custom HA service + tool that allows Nova to create
Home Assistant automations from natural language instructions.

Usage via conversation: "Nova, create an automation that turns off
the living room lights at midnight."

The LLM generates the automation YAML, this module validates and
registers it with HA.
"""
from __future__ import annotations

import logging
import yaml
from typing import Any, Optional

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def create_automation(
    hass: HomeAssistant,
    *,
    alias: str,
    description: str = "",
    trigger: list[dict] | dict = None,
    condition: list[dict] | dict | None = None,
    action: list[dict] | dict = None,
    mode: str = "single",
    request_id: Optional[str] = None,
    source: str = "ha_service",
    requested_by_user_id: Optional[str] = None,
    requested_by_name: Optional[str] = None,
) -> dict[str, Any]:
    """
    Create and register a new HA automation programmatically.

    Args:
        alias: Human-readable name for the automation
        description: Optional description
        trigger: Trigger configuration (HA automation trigger format)
        condition: Optional condition(s)
        action: Action(s) to perform
        mode: Execution mode (single, restart, queued, parallel)
        request_id: Action Audit Log ownership (v3 correction). This
            function is called both directly (the `nova.create_automation`
            HA service — a genuine top-level action) and as a supporting
            step of suggestion installation (pattern_analyzer.py). Pass the
            caller's own request_id to fold this row into ITS request
            instead of minting a second one for the same user intent; omit
            it (default) when this call IS the top-level action.

    Returns:
        {"success": True, "automation_id": "...", "alias": "..."}
        or {"success": False, "error": "..."}
    """
    from . import action_log
    if request_id is None:
        request_id = action_log.new_request_id()
    action_id = await hass.async_add_executor_job(
        lambda: action_log.start(
            request_id, "create_automation", source,
            requested_by_user_id=requested_by_user_id,
            requested_by_name=requested_by_name,
        )
    )

    if not trigger or not action:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "failed", reason_code="missing_trigger_or_action")
        )
        return {"success": False, "error": "Both trigger and action are required"}

    # Normalize to lists
    if isinstance(trigger, dict):
        trigger = [trigger]
    if isinstance(action, dict):
        action = [action]
    if condition and isinstance(condition, dict):
        condition = [condition]

    # Build the automation config
    automation_id = f"nova_auto_{alias.lower().replace(' ', '_')[:40]}"
    auto_config = {
        "id": automation_id,
        "alias": f"Nova · {alias}",
        "description": description or f"Created by Nova: {alias}",
        "mode": mode,
        "triggers": trigger,
        "actions": action,
    }
    if condition:
        auto_config["conditions"] = condition

    # Validate the YAML is well-formed
    try:
        yaml_str = yaml.dump([auto_config], default_flow_style=False)
        _LOGGER.debug("Nova automation YAML:\n%s", yaml_str)
    except Exception as exc:
        return {"success": False, "error": f"YAML generation failed: {exc}"}

    # Write to automations.yaml
    try:
        automations_path = hass.config.path("automations.yaml")

        def _write_automation():
            # Read existing
            try:
                with open(automations_path) as f:
                    existing = yaml.safe_load(f) or []
            except FileNotFoundError:
                existing = []
            except Exception:
                existing = []

            if not isinstance(existing, list):
                existing = []

            # Check for duplicate ID
            existing = [a for a in existing if a.get("id") != automation_id]

            # Append new
            existing.append(auto_config)

            # Write back
            with open(automations_path, "w") as f:
                yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

        await hass.async_add_executor_job(_write_automation)

        # Reload automations — supporting work for the row already created
        # above, never a second row of its own (ownership rule).
        await hass.services.async_call("automation", "reload", blocking=True)

        _LOGGER.info("Nova created automation: %s (id=%s)", alias, automation_id)
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "accepted")
        )
        return {
            "success": True,
            "automation_id": automation_id,
            "alias": f"Nova · {alias}",
        }

    except Exception as exc:
        _LOGGER.error("Nova automation creation failed: %s", exc)
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "failed", reason_code="write_or_reload_failed")
        )
        return {"success": False, "error": str(exc)}
