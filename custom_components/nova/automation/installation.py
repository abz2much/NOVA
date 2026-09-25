"""Nova automation installation: the one path that writes automations.yaml.

Both the ``nova.create_automation`` service and approval of a learned
suggestion install through this module. An installation validates with Home
Assistant's own validator, refuses an equivalent loaded automation, writes the
file atomically, reloads Home Assistant, confirms the automation loaded, and
restores the exact original bytes when any of that fails. ``_WRITE_LOCK`` is
process-wide on purpose: automations.yaml is one file for the whole Home
Assistant process, whichever config entry asks.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import tempfile
import yaml
from typing import TYPE_CHECKING, Any, Optional

from .matching import fingerprint
from .models import (
    MATCH_EXACT, MATCH_UNAVAILABLE, SUGGESTION_APPROVED, SUGGESTION_PENDING,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


_LOGGER = logging.getLogger(__name__)
_WRITE_LOCK = asyncio.Lock()


class AutomationWriteError(Exception):
    """Raised when the HA automation file cannot be changed safely.
    reason_code goes to the action audit log only."""

    def __init__(self, message: str, *, reason_code: str = "write_or_reload_failed"):
        super().__init__(message)
        self.reason_code = reason_code


class _AlreadyInstalled(Exception):
    """The exact automation is already in automations.yaml."""


_ID_PREFIX = "nova_auto_"
_ID_SLUG_CHARS = 40
_ID_DIGEST_CHARS = 12


def automation_id_for(alias: str, config: dict[str, Any]) -> str:
    """Deterministic, collision-resistant id for a Nova automation.

    The readable part is the alias slug Nova has always used (truncated to 40
    characters); the suffix is a digest of the full alias and the canonical
    behaviour (trigger, condition, action, mode), so two automations whose
    aliases share a long prefix, or whose behaviour differs, never share an
    id. Ids of automations Nova already installed are never recomputed."""
    slug = alias.lower().replace(" ", "_")[:_ID_SLUG_CHARS]
    behaviour = fingerprint(config) or json.dumps(
        config, sort_keys=True, default=str)
    digest = hashlib.sha256(
        f"{alias}\n{behaviour}".encode("utf-8")).hexdigest()[:_ID_DIGEST_CHARS]
    return f"{_ID_PREFIX}{slug}_{digest}"


def _existing_with_id(items: list[dict[str, Any]], automation_id: str):
    for item in items:
        if str(item.get("id", "")) == automation_id:
            return item
    return None


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



# ── The installation transaction ────────────────────────────────────────────
# One serialized transaction covers everything that must not interleave:
# the suggestion status check, the duplicate recheck, the file write, the
# reload, the load confirmation and the final suggestion state. A suggestion
# install opens the transaction and create_automation joins it; a direct
# ``nova.create_automation`` call opens its own.
_IN_TRANSACTION: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "nova_automation_transaction", default=False)

_INSTALLABLE_STATUSES = (SUGGESTION_PENDING, SUGGESTION_APPROVED)


@contextlib.asynccontextmanager
async def installation_transaction():
    """Hold the process-wide automation write lock (re-entrant per task)."""
    if _IN_TRANSACTION.get():
        yield
        return
    async with _WRITE_LOCK:
        token = _IN_TRANSACTION.set(True)
        try:
            yield
        finally:
            _IN_TRANSACTION.reset(token)


async def _run_to_completion(awaitable) -> tuple[Any, bool]:
    """Await ``awaitable`` to the end even if the caller is cancelled.

    Returns ``(result, cancelled)``; the caller re-raises the cancellation
    once its cleanup is done, so cleanup can never be skipped."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


def _duplicate_automation(config: dict[str, Any], records: list[Any]) -> str:
    """Return the name of a loaded automation proven equivalent to ``config``."""
    from .matching import classify
    match = classify(config, records)
    if match.get("status") != MATCH_EXACT:
        return ""
    first = (match.get("matches") or [{}])[0]
    return str(first.get("name") or first.get("entity_id") or "another automation")


def _confirm_loaded(hass: HomeAssistant, automation_id: str) -> Optional[bool]:
    """Confirm the new ID in the refreshed cache; None means unavailable."""
    try:
        from .inventory import get_inventory
        inventory = get_inventory(hass)
        if inventory is None:
            return None
        inventory.refresh()
        return any(getattr(record, "unique_id", "") == automation_id
                   for record in inventory.records())
    except Exception:
        return None


async def _rollback(hass: HomeAssistant, path: str, original: bytes | None,
                    automation_id: str) -> str:
    """Restore the exact original bytes, reload them and check the new
    automation is gone. Returns "" when the rollback is confirmed, otherwise
    what went wrong."""
    try:
        await hass.async_add_executor_job(_restore_original, path, original)
    except Exception as exc:
        return f"the original automations.yaml could not be restored ({exc})"
    try:
        await hass.services.async_call("automation", "reload", blocking=True)
    except Exception as exc:
        return f"the restored automations.yaml could not be reloaded ({exc})"
    loaded = _confirm_loaded(hass, automation_id)
    if loaded is None:
        return "Nova could not confirm that Home Assistant unloaded the new automation"
    if loaded:
        return "Home Assistant still has the new automation loaded"
    return ""


def _rollback_error(cause: str, problem: str) -> AutomationWriteError:
    if problem:
        _LOGGER.error("Nova automation rollback failed: %s", problem)
        return AutomationWriteError(
            f"{cause}; rollback failed: {problem}", reason_code="rollback_failed")
    return AutomationWriteError(
        f"{cause}; the original automations.yaml was restored and reloaded")


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
    request_device_id: Optional[str] = None,
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
            step of suggestion installation (install_approved_suggestion). Pass the
            caller's own request_id to fold this row into ITS request
            instead of minting a second one for the same user intent; omit
            it (default) when this call IS the top-level action.

    Returns:
        {"success": True, "automation_id": "...", "alias": "..."}
        or {"success": False, "error": "..."}

    Cancellation after the write starts restores the exact original bytes,
    reloads them, and then re-raises the cancellation.
    """
    from .. import action_log
    if request_id is None:
        request_id = action_log.new_request_id()
    action_id = await hass.async_add_executor_job(
        lambda: action_log.start(
            request_id, "create_automation", source,
            requested_by_user_id=requested_by_user_id,
            requested_by_name=requested_by_name,
            request_device_id=request_device_id,
        )
    )

    async def _audit_failure(reason_code: str) -> None:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "failed", reason_code=reason_code))

    if not trigger or not action:
        await _audit_failure("missing_trigger_or_action")
        return {"success": False, "error": "Both trigger and action are required"}

    # Normalize to lists
    if isinstance(trigger, dict):
        trigger = [trigger]
    if isinstance(action, dict):
        action = [action]
    if condition and isinstance(condition, dict):
        condition = [condition]

    # Build the automation config
    auto_config = {
        "alias": f"Nova · {alias}",
        "description": description or f"Created by Nova: {alias}",
        "mode": mode,
        "triggers": trigger,
        "actions": action,
    }
    if condition:
        auto_config["conditions"] = condition
    automation_id = automation_id_for(alias, auto_config)
    auto_config = {"id": automation_id, **auto_config}

    # Validate serialization first, then use HA's own automation validator.
    try:
        yaml_str = yaml.dump([auto_config], default_flow_style=False)
        _LOGGER.debug("Nova automation YAML:\n%s", yaml_str)
        await _validate_with_home_assistant(hass, automation_id, auto_config)
    except Exception as exc:
        await _audit_failure("invalid_automation")
        return {"success": False, "error": f"Automation validation failed: {exc}"}

    # HA's config editor also edits automations.yaml. Serialize Nova writes so
    # two Nova requests cannot race, use HA's atomic writer, and restore the
    # exact old bytes if reload or runtime confirmation fails.
    try:
        automations_path = hass.config.path("automations.yaml")
        async with installation_transaction():
            existing, original = await hass.async_add_executor_job(
                _read_automations, automations_path)
            # Never replace another automation: an entry that already holds
            # this id is either this exact automation (a duplicate) or a
            # different one Nova must not overwrite.
            clash = _existing_with_id(existing, automation_id)
            if clash is not None:
                if fingerprint(clash) == fingerprint(auto_config):
                    raise _AlreadyInstalled(str(clash.get("alias") or automation_id))
                raise AutomationWriteError(
                    f"automations.yaml already has a different automation with id "
                    f"{automation_id}; no changes were made")

            # Fail closed: without Home Assistant's loaded automations Nova can
            # neither rule out a duplicate nor confirm the new one loaded.
            from .inventory import get_inventory
            inventory = get_inventory(hass)
            if inventory is None:
                raise AutomationWriteError(
                    "Home Assistant's automation list is unavailable; no changes were made",
                    reason_code=MATCH_UNAVAILABLE)
            try:
                records = inventory.refresh()
                duplicate = _duplicate_automation(auto_config, records)
            except Exception as exc:
                raise AutomationWriteError(
                    f"Nova could not check existing automations ({exc}); "
                    "no changes were made", reason_code=MATCH_UNAVAILABLE) from exc
            if duplicate:
                raise _AlreadyInstalled(duplicate)

            updated = list(existing)
            updated.append(auto_config)
            write = asyncio.ensure_future(hass.async_add_executor_job(
                _atomic_write_yaml, automations_path, updated))
            try:
                await asyncio.shield(write)
                # Supporting work for the existing action-log row, never a
                # second row of its own (ownership rule).
                await hass.services.async_call(
                    "automation", "reload", blocking=True)
                loaded = _confirm_loaded(hass, automation_id)
                if loaded is None:
                    raise AutomationWriteError(
                        "Nova could not confirm Home Assistant loaded the new automation")
                if not loaded:
                    raise AutomationWriteError(
                        "Home Assistant reloaded but did not load the new automation")
            except asyncio.CancelledError:
                # The write may still be running in its thread: let it end,
                # then undo it. Neither step may be skipped by a second cancel.
                await _run_to_completion(asyncio.gather(write, return_exceptions=True))
                problem, _ = await _run_to_completion(
                    _rollback(hass, automations_path, original, automation_id))
                if problem:
                    _LOGGER.error("Nova automation rollback after cancellation "
                                  "failed: %s", problem)
                await _run_to_completion(_audit_failure(
                    "rollback_failed" if problem else "installation_cancelled"))
                raise
            except Exception as exc:
                problem, cancelled = await _run_to_completion(
                    _rollback(hass, automations_path, original, automation_id))
                if cancelled:
                    await _run_to_completion(_audit_failure(
                        "rollback_failed" if problem else "installation_cancelled"))
                    raise asyncio.CancelledError from exc
                raise _rollback_error(str(exc), problem) from exc

        _LOGGER.info("Nova created automation: %s (id=%s)", alias, automation_id)
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "accepted")
        )
        return {
            "success": True,
            "automation_id": automation_id,
            "alias": f"Nova · {alias}",
        }

    except _AlreadyInstalled as exc:
        await _audit_failure("duplicate_automation")
        return {"success": False,
                "error": f"Equivalent automation already exists: {exc}"}
    except Exception as exc:
        _LOGGER.error("Nova automation creation failed: %s", exc)
        await _audit_failure(getattr(exc, "reason_code", "") or "write_or_reload_failed")
        return {"success": False, "error": str(exc)}


async def install_approved_suggestion(
    hass, suggestion_id: int, *,
    requested_by_user_id: Optional[str] = None,
    requested_by_name: Optional[str] = None,
    request_device_id: Optional[str] = None,
) -> dict:
    """
    Close the pattern-engine loop (v6.52.0): install an approved suggestion's
    automation into Home Assistant. Returns a dict the caller relays:

        {"ok": True, "installed": True, "automation_id": "...", "alias": "..."}
        {"ok": True, "installed": False, "reason": "..."}   # advisory / covered
        {"ok": False, "installed": False, "error": "...", "reason": "..."}
                                                             # refused / failed
        {"ok": False, "error": "..."}                        # not found

    An installable suggestion stays pending (retryable) until Home Assistant
    confirms its automation loaded; only then is it marked installed. The
    status check, duplicate recheck, write, reload, confirmation and final
    state all run in one serialized installation transaction.

    Advisory suggestions (repeated-command notes with no concrete trigger) are
    still marked approved — the user acknowledged them — but nothing is written
    to HA, and the reason says so plainly — and nothing is logged to the
    Action Audit Log either (no action was actually attempted).

    Action Audit Log ownership (v3 correction): this IS the top-level
    action (a person clicked Approve in the panel) — it generates the
    request_id and passes it into create_automation() below so the whole
    "approve suggestion -> install automation" flow logs as ONE request,
    not two. ``request_device_id`` is accepted so callers can pass the real
    request identity they have; Nova never invents one.
    """
    from .. import action_log
    request_id = action_log.new_request_id()
    from . import patterns
    analyzer = patterns.get_analyzer()

    def _refused(message: str) -> dict:
        return {"ok": False, "installed": False, "error": message,
                "reason": message, "suggestion_id": suggestion_id}

    try:
        async with installation_transaction():
            sug = await hass.async_add_executor_job(analyzer.get_suggestion, suggestion_id)
            if not sug:
                return {"ok": False, "error": f"suggestion #{suggestion_id} not found"}

            status = sug.get("status") or SUGGESTION_PENDING
            if status not in _INSTALLABLE_STATUSES:
                return _refused(f"suggestion #{suggestion_id} is {status} "
                                "and cannot be installed")

            from .suggestions import normalize_suggestion_automation
            norm = normalize_suggestion_automation(sug.get("automation_yaml", ""))
            if not norm.get("installable"):
                await hass.async_add_executor_job(analyzer.approve_suggestion, suggestion_id)
                return {"ok": True, "installed": False,
                        "reason": norm.get("reason", "not installable"),
                        "suggestion_id": suggestion_id}

            # The home may have changed since this suggestion was created.
            # Repeat duplicate detection inside the transaction so a newly
            # added HA automation cannot race Nova into an equivalent rule.
            # create_automation repeats the check and fails closed when the
            # automation list is unavailable.
            try:
                from .inventory import get_inventory
                from .matching import classify
                inventory = get_inventory(hass)
                match = classify({
                    "triggers": norm["trigger"],
                    "conditions": norm.get("condition") or [],
                    "actions": norm["action"],
                }, inventory.records()) if inventory is not None else {}
            except Exception:
                match = {}
            if match.get("status") == MATCH_EXACT:
                await hass.async_add_executor_job(
                    analyzer.mark_covered, suggestion_id)
                names = ", ".join(m.get("name") or m.get("entity_id", "")
                                  for m in match.get("matches", []))
                return {"ok": True, "installed": False,
                        "reason": "already automated" + (f" by {names}" if names else ""),
                        "suggestion_id": suggestion_id,
                        "automation_match": match}

            result = await create_automation(
                hass,
                alias=norm["alias"],
                description=sug.get("description", ""),
                trigger=norm["trigger"],
                condition=norm.get("condition"),
                action=norm["action"],
                request_id=request_id, source="suggestion",
                requested_by_user_id=requested_by_user_id,
                requested_by_name=requested_by_name,
                request_device_id=request_device_id,
            )
            if not result.get("success"):
                # Nothing was installed: the suggestion stays retryable.
                return _refused(f"automation write failed: {result.get('error')}")

            # The automation is live; recording that must finish even if the
            # caller is cancelled now, or a retry would see a stale pending row.
            _, cancelled = await _run_to_completion(hass.async_add_executor_job(
                analyzer.mark_installed, suggestion_id, result["automation_id"]))
            if cancelled:
                raise asyncio.CancelledError
        try:
            from ..websocket import nova_log
            nova_log("LEARN", f"Installed learned automation "
                                f"'{result['alias']}' from suggestion "
                                f"#{suggestion_id}")
        except Exception:
            pass
        return {"ok": True, "installed": True,
                "automation_id": result["automation_id"],
                "alias": result["alias"], "suggestion_id": suggestion_id}
    except Exception as exc:
        _LOGGER.exception("install_approved_suggestion failed: %s", exc)
        return {"ok": False, "error": str(exc)}
