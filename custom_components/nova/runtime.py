"""Typed runtime state for one Nova config entry.

async_setup_entry builds one NovaRuntime and stores it on entry.runtime_data.
It names the entry-scoped live objects setup constructs and who owns them:

* NovaResources owns disposables (unsubs, tasks, closeables),
* NovaScheduler owns recurring sweeps (and is itself a NovaResources closeable),
* async_unload_entry owns teardown.

Until Phase 3C the old hass.data[DOMAIN][entry_id] dict stays as a
compatibility bridge. build_compat_bridge() fills it with the SAME objects the
runtime holds, never copies, so readers of either see one live state.

observer_running is a bool, so it cannot be shared by identity. The runtime
owns it, set_observer_running() is its only writer, and that helper mirrors
the value into the bridge on every change.
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
    # Whether this entry started the observer. Change it only through
    # set_observer_running(), which keeps the bridge in step.
    observer_running: bool = False


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

    Every runtime-backed value is the runtime's own object, except
    observer_running: a bool copy that set_observer_running() keeps in step.
    The unsub lists are bridge-only (NovaResources owns the same callables);
    keys other modules add later (proactive-audio unsubs) stay bridge-only
    too."""
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
        "observer_running":    runtime.observer_running,
    }
    if runtime.automation_inventory is not None:
        bridge["automation_inventory"] = runtime.automation_inventory
    return bridge


def lifecycle_runtime(entry: ConfigEntry) -> NovaRuntime | None:
    """The entry's runtime, or None when it has none.

    Only for lifecycle code where a missing runtime is a valid state: unload
    after a partial setup, a repeated unload, or an entry that is not loaded.
    Normal loaded operation uses get_runtime(), which raises instead."""
    runtime = getattr(entry, "runtime_data", None)
    return runtime if isinstance(runtime, NovaRuntime) else None


def set_observer_running(
    hass: HomeAssistant, entry: ConfigEntry, running: bool,
) -> None:
    """Record whether this entry's observer is running. The only writer.

    NovaRuntime is authoritative. The bridge copy is updated in the same
    call while the bridge exists, so unmigrated readers never see a stale
    value. Raises NovaRuntimeUnavailable when the entry has no runtime."""
    runtime = get_runtime(entry)
    runtime.observer_running = bool(running)
    store = hass.data.get(DOMAIN)
    bridge = store.get(entry.entry_id) if isinstance(store, dict) else None
    if isinstance(bridge, dict):
        bridge["observer_running"] = runtime.observer_running


def observer_status(entry: ConfigEntry) -> bool:
    """Whether the entry's observer is running, for status readers.

    An entry that is not loaded (setup still running, failed, or unloaded)
    has no observer, so a missing runtime reads as False there. A loaded
    entry must have a runtime: that case raises NovaRuntimeUnavailable
    rather than reporting a made-up False."""
    runtime = lifecycle_runtime(entry)
    if runtime is not None:
        return runtime.observer_running
    from homeassistant.config_entries import ConfigEntryState
    if getattr(entry, "state", None) is ConfigEntryState.LOADED:
        return get_runtime(entry).observer_running   # raises
    return False


def lifecycle_runtime_config(entry: ConfigEntry) -> dict[str, Any]:
    """The entry's live runtime_config, for readers that may run outside a
    loaded entry.

    With a runtime, returns runtime.runtime_config itself, never a copy, so
    every call sees the latest panel writes. An entry that is not loaded
    (setup still running before the runtime exists, failed, or unloaded) has
    no panel settings yet, so it gets an empty dict and the caller's
    defaults. A loaded entry must have a runtime: that case raises
    NovaRuntimeUnavailable rather than acting on made-up defaults. Never
    reads the hass.data bridge."""
    runtime = lifecycle_runtime(entry)
    if runtime is not None:
        return runtime.runtime_config
    from homeassistant.config_entries import ConfigEntryState
    if getattr(entry, "state", None) is ConfigEntryState.LOADED:
        return get_runtime(entry).runtime_config   # raises
    return {}


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
