"""Runtime-owned provider clients.

A ProviderManager is created by async_setup_entry, stored on
NovaRuntime.providers and registered with NovaResources, which closes it on
unload, reload and setup failure. It is the only lifecycle owner of every
provider client Nova builds while an entry is loaded; NovaRuntime.client is a
non-owning reference to the client bound as "primary".

Clients are pooled by configuration fingerprint (provider, model, endpoint
and a digest of the credential):

* construction is deduplicated per fingerprint, so concurrent requests for
  the same configuration build one client;
* named bindings ("primary", "vision", "observer_reasoning", ...) point at a
  fingerprint; rebinding to a new configuration is a single synchronous
  step on the event loop, so no caller ever sees a half-replaced binding;
* a lease() pins a client for the duration of one operation (an agent
  turn, a camera analysis) without binding it;
* a client no binding or lease references any more is retired and closed
  exactly once, after its in-flight calls finish;
* closing the manager closes every client it built, exactly once, whether
  its SDK closes synchronously, asynchronously or not at all.

Code that runs without a loaded entry (config-flow checks, unit tests) uses
provider_scope(), which yields the runtime's manager when there is one and
otherwise a transient manager closed when the scope exits. Nova keeps no
provider state in module globals or hass.data.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional

from .errors import ProviderError, ProviderErrorKind
from .routing import ProviderSpec, build_provider

_LOGGER = logging.getLogger(__name__)

_TRACKER_ATTR = "_nova_call_tracker"


class ProviderManager:
    """See the module docstring."""

    def __init__(self, hass, *, builder: Callable[[ProviderSpec], Any] = build_provider):
        self._hass = hass
        self._builder = builder
        self._clients: dict[str, Any] = {}            # fingerprint -> client
        self._bindings: dict[str, str] = {}           # binding -> fingerprint
        self._building: dict[str, asyncio.Future] = {}
        self._waiters: dict[str, int] = {}            # fingerprint -> callers awaiting a build
        self._leases: dict[str, int] = {}             # fingerprint -> open leases
        self._inflight: dict[int, int] = {}           # id(client) -> calls
        self._retired: dict[int, Any] = {}            # id(client) -> client
        self._closed_ids: set[int] = set()
        self._pending: set[asyncio.Task] = set()
        self._closed = False
        self.close_errors = 0

    # ── Introspection ───────────────────────────────────────────────────────

    @property
    def closed(self) -> bool:
        return self._closed

    def bound(self, binding: str) -> Optional[Any]:
        """The client currently bound under ``binding`` (non-owning)."""
        fp = self._bindings.get(binding)
        return self._clients.get(fp) if fp is not None else None

    @property
    def primary(self) -> Optional[Any]:
        return self.bound("primary")

    def client_count(self) -> int:
        return len(self._clients) + len(self._retired)

    # ── Acquisition ─────────────────────────────────────────────────────────

    async def async_acquire(self, spec: ProviderSpec, *, binding: str,
                            factory: Optional[Callable[[], Any]] = None) -> Any:
        """Return the client for ``spec`` and bind it under ``binding``.

        Reuses a pooled client with the same fingerprint, otherwise builds
        one in the executor (one build per fingerprint however many callers
        ask at once). ``factory`` builds the client instead of the manager's
        builder; it must build the client ``spec`` describes. Rebinding
        retires the previously bound client when nothing else uses it."""
        fp, client = await self._obtain(spec, factory)
        self._bind(binding, fp)
        return client

    async def async_acquire_all(
        self,
        requests: dict[str, tuple[ProviderSpec, Optional[Callable[[], Any]]]],
    ) -> dict[str, Any]:
        """Acquire several bindings as one step: every client is obtained
        first, and only when all succeed are the bindings moved, together.
        On any failure no binding changes (clients built for the attempt are
        retired) and the error propagates."""
        obtained: dict[str, tuple[str, Any]] = {}
        pinned: list[str] = []
        try:
            for binding, (spec, factory) in requests.items():
                fp, client = await self._obtain(spec, factory)
                self._leases[fp] = self._leases.get(fp, 0) + 1
                pinned.append(fp)
                obtained[binding] = (fp, client)
            self._ensure_open()
            for binding, (fp, _client) in obtained.items():
                self._bind(binding, fp)
            return {binding: client for binding, (_fp, client) in obtained.items()}
        finally:
            for fp in pinned:
                self._unpin(fp)

    def _unpin(self, fp: str) -> None:
        remaining = self._leases.get(fp, 0) - 1
        if remaining > 0:
            self._leases[fp] = remaining
            return
        self._leases.pop(fp, None)
        if not self._closed:
            self._retire_if_unbound(fp)

    @asynccontextmanager
    async def lease(self, spec: ProviderSpec, *, binding: Optional[str] = None,
                    factory: Optional[Callable[[], Any]] = None) -> AsyncIterator[Any]:
        """Hold the client for ``spec`` for the duration of the block.

        A leased client is never closed while the lease is held, even if a
        binding moves away from it meanwhile. With ``binding`` it is also
        bound (kept pooled after the block); without one it is retired when
        the last lease on it ends."""
        fp, client = await self._obtain(spec, factory)
        if binding is not None:
            self._bind(binding, fp)
        self._leases[fp] = self._leases.get(fp, 0) + 1
        try:
            yield client
        finally:
            self._unpin(fp)

    async def _obtain(self, spec: ProviderSpec,
                      factory: Optional[Callable[[], Any]]) -> tuple[str, Any]:
        self._ensure_open()
        fp = spec.fingerprint()
        client = self._clients.get(fp)
        if client is not None:
            return fp, client
        task = self._building.get(fp)
        if task is None:
            task = asyncio.ensure_future(self._build(spec, fp, factory))
            self._building[fp] = task
        self._waiters[fp] = self._waiters.get(fp, 0) + 1
        try:
            # Shielded: one caller's cancellation never cancels a build other
            # callers are waiting on. A build nobody is waiting for any more
            # retires its client as soon as it finishes (see _build).
            client = await asyncio.shield(task)
        finally:
            remaining = self._waiters.get(fp, 0) - 1
            if remaining > 0:
                self._waiters[fp] = remaining
            else:
                self._waiters.pop(fp, None)
        self._ensure_open()
        return fp, client

    async def _build(self, spec: ProviderSpec, fp: str,
                     factory: Optional[Callable[[], Any]]) -> Any:
        build = factory if factory is not None else (lambda: self._builder(spec))
        try:
            client = await self._hass.async_add_executor_job(build)
        finally:
            self._building.pop(fp, None)
        if client is None:
            raise ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, spec.provider,
                                detail="the provider could not be built")
        if self._closed:
            await self._close_client(client)
            raise ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, spec.provider,
                                detail="Nova is unloading")
        existing = self._clients.get(fp)
        if existing is not None:      # defensive: never two clients per fingerprint
            await self._close_client(client)
            return existing
        self._clients[fp] = client
        try:
            setattr(client, _TRACKER_ATTR, self)
        except Exception:
            pass
        if not self._waiters.get(fp):
            # Every caller was cancelled while it was being built.
            self._retire_if_unbound(fp)
        return client

    def _bind(self, binding: str, fp: str) -> None:
        previous = self._bindings.get(binding)
        self._bindings[binding] = fp
        if previous is not None and previous != fp:
            self._retire_if_unbound(previous)

    async def async_release(self, binding: str) -> None:
        """Drop a binding; its client is retired when nothing else uses it."""
        fp = self._bindings.pop(binding, None)
        if fp is not None:
            self._retire_if_unbound(fp)
        await self._drain()

    def _in_use(self, fp: str) -> bool:
        return (fp in self._bindings.values() or bool(self._leases.get(fp))
                or bool(self._waiters.get(fp)))

    def _retire_if_unbound(self, fp: str) -> None:
        if self._in_use(fp):
            return
        client = self._clients.pop(fp, None)
        if client is None:
            return
        if self._inflight.get(id(client), 0) > 0:
            self._retired[id(client)] = client      # closed when its calls end
        else:
            self._spawn_close(client)

    # ── In-flight tracking (called by the activity boundary) ───────────────

    def call_started(self, client: Any) -> None:
        self._inflight[id(client)] = self._inflight.get(id(client), 0) + 1

    def call_finished(self, client: Any) -> None:
        key = id(client)
        remaining = self._inflight.get(key, 0) - 1
        if remaining > 0:
            self._inflight[key] = remaining
            return
        self._inflight.pop(key, None)
        retired = self._retired.pop(key, None)
        if retired is not None:
            self._spawn_close(retired)

    # ── Closing ─────────────────────────────────────────────────────────────

    def _spawn_close(self, client: Any) -> None:
        task = asyncio.ensure_future(self._close_client(client))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _drain(self) -> None:
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def _close_client(self, client: Any) -> None:
        """Close one client exactly once."""
        key = id(client)
        if key in self._closed_ids:
            return
        self._closed_ids.add(key)
        if not await async_close_client(self._hass, client):
            self.close_errors += 1

    async def async_close(self) -> None:
        """Close every client this manager built. Idempotent. Registered
        with NovaResources, which calls it on unload, reload and setup
        failure."""
        if self._closed:
            await self._drain()
            return
        self._closed = True
        clients = list(self._clients.values()) + list(self._retired.values())
        self._clients.clear()
        self._retired.clear()
        self._bindings.clear()
        self._leases.clear()
        for task in list(self._building.values()):
            # A build still running closes its own client when it finishes
            # (see _build); wait for it so nothing outlives the manager.
            try:
                await asyncio.shield(task)
            except Exception:
                pass
        for client in clients:
            await self._close_client(client)
        await self._drain()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, "provider",
                                detail="Nova is unloading")


async def async_close_client(hass, client: Any) -> bool:
    """Close a client the way its SDK supports: an async close is awaited, a
    blocking close runs in the executor, and a client with neither needs
    nothing. Returns False when the close itself failed; never raises."""
    try:
        async_close = getattr(client, "async_close", None)
        if async_close is not None and inspect.iscoroutinefunction(async_close):
            await async_close()
            return True
        close = getattr(client, "close", None)
        if callable(close):
            await hass.async_add_executor_job(close)
        return True
    except Exception as exc:     # a failed close never blocks the rest
        _LOGGER.debug("Provider client close failed: %s", type(exc).__name__)
        return False


def call_tracker(client: Any) -> Optional[ProviderManager]:
    """The manager that owns ``client``, if any (for in-flight tracking)."""
    tracker = getattr(client, _TRACKER_ATTR, None)
    return tracker if isinstance(tracker, ProviderManager) else None


def runtime_manager(hass) -> Optional[ProviderManager]:
    """The loaded Nova entry's ProviderManager, or None when there is no
    loaded entry. Never reads hass.data."""
    try:
        from ..runtime import domain_runtime
        runtime = domain_runtime(hass)
    except Exception:
        return None
    manager = getattr(runtime, "providers", None) if runtime is not None else None
    if isinstance(manager, ProviderManager) and not manager.closed:
        return manager
    return None


def entry_manager(entry) -> Optional[ProviderManager]:
    """The ProviderManager of a specific entry's runtime, or None."""
    from ..runtime import lifecycle_runtime
    runtime = lifecycle_runtime(entry) if entry is not None else None
    manager = getattr(runtime, "providers", None) if runtime is not None else None
    if isinstance(manager, ProviderManager) and not manager.closed:
        return manager
    return None


@asynccontextmanager
async def provider_scope(hass, *, entry=None) -> AsyncIterator[ProviderManager]:
    """Yield the runtime's ProviderManager (never closed here), or a
    transient one that closes every client it built when the scope exits."""
    manager = entry_manager(entry) if entry is not None else None
    if manager is None:
        manager = runtime_manager(hass)
    if manager is not None:
        yield manager
        return
    transient = ProviderManager(hass)
    try:
        yield transient
    finally:
        await transient.async_close()
