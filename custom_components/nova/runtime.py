"""Typed runtime state for one Nova config entry.

async_setup_entry builds one NovaRuntime and stores it on entry.runtime_data.
It names the entry-scoped live objects setup constructs and who owns them:

* NovaResources owns disposables (unsubs, tasks, closeables),
* NovaScheduler owns recurring sweeps (and is itself a NovaResources closeable),
* async_unload_entry owns teardown.

NovaRuntime is the only owner of Nova's runtime state. Nova keeps nothing
in hass.data: every consumer reaches its entry's runtime through
get_runtime(), current_runtime() or, for domain-level code that is not handed
an entry, domain_runtime(). A missing runtime raises NovaRuntimeUnavailable
instead of falling back to another store.

observer_running is changed only through set_observer_running().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias

from .const import DOMAIN
from .migrations import CURRENT_SCHEMA_VERSION

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .automation import EntityLockRegistry
    from .automation.attribution import AutomationContextTracker
    from .automation.inventory import AutomationInventory
    from .boot_guard import AlertBuffer
    from .intent import LocalIntentRouter
    from .llm_provider import LLMProvider
    from .reminders import ReminderWatcher
    from .resources import NovaResources
    from .scheduler import NovaScheduler
    from .sentinel import NovaSentinel
    from .state_ledger import StateLedger


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
    # Panel-live settings. One live dict, changed in place by panel writes.
    runtime_config: dict[str, Any] = field(default_factory=dict)
    schema_version: int = CURRENT_SCHEMA_VERSION
    # Whether this entry started the observer. Change it only through
    # set_observer_running().
    observer_running: bool = False
    # Proactive audio (nova.speak / nova.process_intent). proactive_audio.py
    # builds each object lazily on first use and drops them on unload, so a
    # reload always starts with fresh ones.
    intent_router: LocalIntentRouter | None = None
    state_ledger: StateLedger | None = None
    entity_locks: EntityLockRegistry | None = None
    alert_buffer: AlertBuffer | None = None
    # True while a proactive-audio infrastructure audit is in progress, so a
    # slow announcement never overlaps the next tick.
    audit_running: bool = False


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


def lifecycle_runtime(entry: ConfigEntry) -> NovaRuntime | None:
    """The entry's runtime, or None when it has none.

    Only for lifecycle code where a missing runtime is a valid state: unload
    after a partial setup, a repeated unload, or an entry that is not loaded.
    Normal loaded operation uses get_runtime(), which raises instead."""
    runtime = getattr(entry, "runtime_data", None)
    return runtime if isinstance(runtime, NovaRuntime) else None


def set_observer_running(entry: ConfigEntry, running: bool) -> None:
    """Record whether this entry's observer is running. The only writer.

    Raises NovaRuntimeUnavailable when the entry has no runtime."""
    get_runtime(entry).observer_running = bool(running)


def current_runtime(entry: ConfigEntry) -> NovaRuntime | None:
    """The entry's runtime, or None when the entry is not loaded.

    For code that can also run while setup has not built the runtime yet,
    after setup failed, or after unload: there a missing runtime is a valid
    state and the caller uses its lower-precedence defaults. A LOADED entry
    must have a runtime, so that case raises NovaRuntimeUnavailable instead
    of letting the caller act on made-up defaults. Never reads hass.data."""
    runtime = lifecycle_runtime(entry)
    if runtime is not None:
        return runtime
    from homeassistant.config_entries import ConfigEntryState
    if getattr(entry, "state", None) is ConfigEntryState.LOADED:
        return get_runtime(entry)   # raises
    return None


def domain_runtime(hass: HomeAssistant) -> NovaRuntime | None:
    """The runtime of this Home Assistant's Nova config entry, for code that
    is not handed an entry (domain-level services and shared helpers).

    Nova allows one config entry (its unique_id is the domain). It is found
    through Home Assistant's config-entry registry, never hass.data, and
    checked like current_runtime(): None when no Nova entry is
    loaded (none configured, setup has not built the runtime yet, setup
    failed, or it was unloaded), NovaRuntimeUnavailable when the entry is
    LOADED but has no runtime."""
    config_entries = getattr(hass, "config_entries", None)
    if config_entries is None:
        return None
    for entry in config_entries.async_entries(DOMAIN):
        runtime = current_runtime(entry)
        if runtime is not None:
            return runtime
    return None


def domain_runtime_config(hass: HomeAssistant) -> dict[str, Any]:
    """The live runtime_config of this Home Assistant's Nova entry, for
    synchronous event-loop readers that are not handed an entry.

    Returns runtime.runtime_config itself, so every call sees the latest
    panel writes; {} when no Nova entry is loaded. Raises like
    domain_runtime() for a loaded entry without a runtime. Never hand the
    result to an executor job: take domain_runtime_config_snapshot()."""
    runtime = domain_runtime(hass)
    return runtime.runtime_config if runtime is not None else {}


def domain_runtime_config_snapshot(hass: HomeAssistant) -> dict[str, Any]:
    """A fresh shallow copy of domain_runtime_config(), only for handing to
    an executor job. Same rules as runtime_config_snapshot()."""
    return dict(domain_runtime_config(hass))


def observer_status(entry: ConfigEntry) -> bool:
    """Whether the entry's observer is running, for status readers.

    An entry that is not loaded (setup still running, failed, or unloaded)
    has no observer, so a missing runtime reads as False there. A loaded
    entry must have a runtime: that case raises NovaRuntimeUnavailable
    rather than reporting a made-up False."""
    runtime = current_runtime(entry)
    return runtime.observer_running if runtime is not None else False


def lifecycle_runtime_config(entry: ConfigEntry) -> dict[str, Any]:
    """The entry's live runtime_config, for readers that may run outside a
    loaded entry.

    With a runtime, returns runtime.runtime_config itself, never a copy, so
    every call sees the latest panel writes. An entry that is not loaded
    (setup still running before the runtime exists, failed, or unloaded) has
    no panel settings yet, so it gets an empty dict and the caller's
    defaults. A loaded entry must have a runtime: that case raises
    NovaRuntimeUnavailable rather than acting on made-up defaults. Never
    reads hass.data."""
    runtime = current_runtime(entry)
    return runtime.runtime_config if runtime is not None else {}


def runtime_config_snapshot(
    entry: ConfigEntry, *, strict: bool = False,
) -> dict[str, Any]:
    """A fresh shallow copy of the entry's runtime_config, only for handing
    to an executor job (or any other thread).

    runtime.runtime_config is owned by the event loop, and panel writes
    change it in place there, so a worker thread must never iterate the live
    dict. Take one snapshot on the event loop right before each
    async_add_executor_job call and pass that; never keep it for a later
    operation, so the next one sees newer values. Synchronous event-loop
    readers use the live dict (lifecycle_runtime_config / get_runtime).

    Ownership is checked exactly like the live accessors: strict=True
    behaves like get_runtime() and raises NovaRuntimeUnavailable whenever
    the entry has no runtime; strict=False behaves like
    lifecycle_runtime_config() ({} for an entry that is not loaded, raise
    for a loaded entry with no runtime). Never reads hass.data."""
    if strict:
        return dict(get_runtime(entry).runtime_config)
    return dict(lifecycle_runtime_config(entry))


def clear_runtime(entry: ConfigEntry) -> None:
    """Drop the entry's runtime_data. Idempotent.

    Only forgets references; disposing the objects is async_unload_entry's
    job (NovaResources.close_all() and the explicit stops)."""
    if hasattr(entry, "runtime_data"):
        # ConfigEntry.__setattr__ is guarded; this mirrors how Home Assistant
        # itself drops runtime_data after a successful unload.
        object.__delattr__(entry, "runtime_data")
