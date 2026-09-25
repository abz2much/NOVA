"""Typed runtime state for one Nova config entry.

async_setup_entry builds one NovaRuntime and stores it on entry.runtime_data.
It names the entry-scoped live objects setup constructs and who owns them:

* NovaResources owns disposables (unsubs, tasks, closeables),
* NovaScheduler owns recurring sweeps (and is itself a NovaResources closeable),
* async_unload_entry owns teardown.

During Phase 3A the old hass.data[DOMAIN][entry_id] dict stays as a
compatibility bridge. build_compat_bridge() fills it with the SAME objects the
runtime holds, never copies, so readers of either see one live state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias

from .const import DOMAIN
from .migrations import CURRENT_SCHEMA_VERSION

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .automation_inventory import AutomationContextTracker, AutomationInventory
    from .llm_provider import LLMProvider
    from .reminders import ReminderWatcher
    from .resources import NovaResources
    from .scheduler import NovaScheduler
    from .sentinel import NovaSentinel


@dataclass(slots=True)
class NovaRuntime:
    """The live objects one Nova config entry owns while it is loaded."""

    client: LLMProvider
    llm_provider_name: str
    sentinel: NovaSentinel
    reminder_watcher: ReminderWatcher
    scheduler: NovaScheduler
    resources: NovaResources
    automation_contexts: AutomationContextTracker
    automation_inventory: AutomationInventory | None = None
    # Panel-live settings. One dict, shared by identity with the bridge, so a
    # panel write through either path is seen by both.
    runtime_config: dict[str, Any] = field(default_factory=dict)
    schema_version: int = CURRENT_SCHEMA_VERSION


# String form keeps the alias lazy: nothing subscripts ConfigEntry at import
# time, and every annotation using it is postponed (PEP 563).
NovaConfigEntry: TypeAlias = "ConfigEntry[NovaRuntime]"


class NovaRuntimeUnavailable(RuntimeError):
    """The entry has no live NovaRuntime (not set up yet, or already released)."""


def get_runtime(entry: ConfigEntry) -> NovaRuntime:
    """Return the entry's live runtime, or raise NovaRuntimeUnavailable.

    Never substitutes a default: missing runtime state is an internal error
    for new runtime access code, not something to paper over."""
    runtime = getattr(entry, "runtime_data", None)
    if not isinstance(runtime, NovaRuntime):
        raise NovaRuntimeUnavailable(
            f"Nova runtime is not available for config entry "
            f"{getattr(entry, 'entry_id', '?')}")
    return runtime


def build_compat_bridge(
    runtime: NovaRuntime,
    *,
    camera_unsubs: list,
    recognition_unsubs: list,
) -> dict[str, Any]:
    """The hass.data[DOMAIN][entry_id] dict existing readers still use.

    Every runtime-backed value is the runtime's own object. The unsub lists
    are bridge-only (NovaResources owns the same callables); keys other
    modules add later (observer_running, proactive-audio unsubs) stay
    bridge-only too."""
    bridge: dict[str, Any] = {
        "client":              runtime.client,
        "sentinel":            runtime.sentinel,
        "camera_unsubs":       camera_unsubs,
        "recognition_unsubs":  recognition_unsubs,
        "resources":           runtime.resources,
        "scheduler":           runtime.scheduler,
        "reminder_watcher":    runtime.reminder_watcher,
        "llm_provider_name":   runtime.llm_provider_name,
        "schema_version":      runtime.schema_version,
        "automation_contexts": runtime.automation_contexts,
        "runtime_config":      runtime.runtime_config,
    }
    if runtime.automation_inventory is not None:
        bridge["automation_inventory"] = runtime.automation_inventory
    return bridge


def clear_runtime(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the entry's runtime_data and its bridge entry. Idempotent.

    Only forgets references; disposing the objects is async_unload_entry's
    job (NovaResources.close_all() and the explicit stops)."""
    store = hass.data.get(DOMAIN)
    if isinstance(store, dict):
        store.pop(entry.entry_id, None)
    if hasattr(entry, "runtime_data"):
        # ConfigEntry.__setattr__ is guarded; this mirrors how Home Assistant
        # itself drops runtime_data after a successful unload.
        object.__delattr__(entry, "runtime_data")
