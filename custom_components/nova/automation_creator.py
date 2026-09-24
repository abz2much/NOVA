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

import asyncio
import logging
import os
import tempfile
import yaml
from typing import Any, Optional

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_WRITE_LOCK = asyncio.Lock()


class AutomationWriteError(Exception):
    """Raised when the HA automation file cannot be changed safely."""


def _read_automations(path: str) -> tuple[list[dict[str, Any]], bytes | None]:
    """Read the complete file or fail closed; never reinterpret damage as []."""
    try:
        with open(path, "rb") as file_handle:
            original = file_handle.read()
    except FileNotFoundError:
        return [], None
    try:
        parsed = yaml.safe_load(original.decode("utf-8"))
    except Exception as exc:
        raise AutomationWriteError(
            f"automations.yaml could not be parsed; no changes were made: {exc}") from exc
    if parsed is None:
        return [], original
    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
        raise AutomationWriteError(
            "automations.yaml is not a list of automations; no changes were made")
    return parsed, original


def _atomic_write_yaml(path: str, data: list[dict[str, Any]]) -> None:
    """Use Home Assistant's own atomic writer, with a portable fallback."""
    try:
        from homeassistant.util.file import write_utf8_file_atomic
        from homeassistant.util.yaml import dump
        contents = dump(data)
        write_utf8_file_atomic(path, contents)
        return
    except ImportError:
        contents = yaml.safe_dump(
            data, default_flow_style=False, sort_keys=False, allow_unicode=True)

    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(
        dir=directory, prefix=".nova-automations-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_handle:
            file_handle.write(contents)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def _restore_original(path: str, original: bytes | None) -> None:
    """Atomically restore the exact pre-write bytes after a failed reload."""
    if original is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(
        dir=directory, prefix=".nova-automations-restore-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file_handle:
            file_handle.write(original)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


async def _validate_with_home_assistant(
    hass: HomeAssistant, automation_id: str, config: dict[str, Any],
) -> None:
    """Run the same validator used by HA's automation editor."""
    try:
        from homeassistant.components.automation.config import (
            async_validate_config_item,
        )
    except ImportError:
        # Only reached by the lightweight unit-test harness. A real supported
        # Home Assistant install always provides this module.
        return
    validated = await async_validate_config_item(hass, automation_id, config)
    if validated is None:
        raise AutomationWriteError("Home Assistant rejected the automation configuration")


def _duplicate_automation(hass: HomeAssistant, config: dict[str, Any],
                          automation_id: str) -> str:
    """Return the name of an equivalent automation with a different ID."""
    try:
        from .automation_inventory import get_inventory
        from .automation_matcher import classify
        inventory = get_inventory(hass)
        if inventory is None:
            return ""
        records = [record for record in inventory.records()
                   if getattr(record, "unique_id", "") != automation_id]
        match = classify(config, records)
        if match.get("status") != "already_automated":
            return ""
        first = (match.get("matches") or [{}])[0]
        return str(first.get("name") or first.get("entity_id") or "another automation")
    except Exception:
        return ""


def _confirm_loaded(hass: HomeAssistant, automation_id: str) -> Optional[bool]:
    """Confirm the new ID in the refreshed cache; None means unavailable."""
    try:
        from .automation_inventory import get_inventory
        inventory = get_inventory(hass)
        if inventory is None:
            return None
        inventory.refresh()
        return any(getattr(record, "unique_id", "") == automation_id
                   for record in inventory.records())
    except Exception:
        return None


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

    # Validate serialization first, then use HA's own automation validator.
    try:
        yaml_str = yaml.dump([auto_config], default_flow_style=False)
        _LOGGER.debug("Nova automation YAML:\n%s", yaml_str)
        await _validate_with_home_assistant(hass, automation_id, auto_config)
    except Exception as exc:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(
                action_id, "failed", reason_code="invalid_automation"))
        return {"success": False, "error": f"Automation validation failed: {exc}"}

    duplicate = _duplicate_automation(hass, auto_config, automation_id)
    if duplicate:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(
                action_id, "failed", reason_code="duplicate_automation"))
        return {"success": False,
                "error": f"Equivalent automation already exists: {duplicate}"}

    # HA's config editor also edits automations.yaml. Serialize Nova writes so
    # two Nova requests cannot race, use HA's atomic writer, and restore the
    # exact old bytes if reload or runtime confirmation fails.
    try:
        automations_path = hass.config.path("automations.yaml")
        async with _WRITE_LOCK:
            existing, original = await hass.async_add_executor_job(
                _read_automations, automations_path)
            updated = [item for item in existing
                       if item.get("id") != automation_id]
            updated.append(auto_config)
            await hass.async_add_executor_job(
                _atomic_write_yaml, automations_path, updated)

            try:
                # Supporting work for the existing action-log row, never a
                # second row of its own (ownership rule).
                await hass.services.async_call(
                    "automation", "reload", blocking=True)
                if _confirm_loaded(hass, automation_id) is False:
                    raise AutomationWriteError(
                        "Home Assistant reloaded but did not load the new automation")
            except Exception:
                await hass.async_add_executor_job(
                    _restore_original, automations_path, original)
                try:
                    await hass.services.async_call(
                        "automation", "reload", blocking=True)
                except Exception:
                    _LOGGER.exception(
                        "Automation rollback restored the file but reload failed")
                raise

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
