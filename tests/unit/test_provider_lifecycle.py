"""Phase 6 provider lifecycle: the runtime-owned ProviderManager pools,
replaces and closes provider clients exactly once, whatever their SDK's close
looks like, and the activity boundary propagates cancellation. Fakes only."""
from __future__ import annotations

import asyncio
import threading

import pytest


class _Hass:
    """Runs executor jobs on a real thread pool, like Home Assistant."""

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)


class SyncClient:
    name = "groq"
    model = "m"

    def __init__(self, tag=""):
        self.tag = tag
        self.closes = 0

    def close(self):
        self.closes += 1

    def chat(self, messages, **kw):
        return {"text": "ok", "tool_calls": [], "usage": {}}


class AsyncClient(SyncClient):
    async def async_close(self):
        self.closes += 1

    def close(self):   # must not be used when async_close exists
        raise AssertionError("sync close called on an async client")


class NoCloseClient:
    name = "custom"
    model = "m"


class FailingClose(SyncClient):
    def close(self):
        self.closes += 1
        raise RuntimeError("close failed")


@pytest.fixture
def mgr(load):
    return load("providers.manager")


@pytest.fixture
def routing(load):
    return load("providers.routing")


@pytest.fixture
def activity(load):
    return load("providers.activity")


def _spec(routing, model="m", key="k", provider="groq"):
    return routing.ProviderSpec(provider=provider, model=model, api_key=key, base_url=None)


def _manager(mgr, factory=SyncClient):
    built = []

    def builder(spec):
        client = factory(spec.model)
        built.append(client)
        return client

    return mgr.ProviderManager(_Hass(), builder=builder), built


# ── Pooling and construction locking ────────────────────────────────────────

async def test_same_configuration_reuses_one_client(mgr, routing):
    manager, built = _manager(mgr)
    a = await manager.async_acquire(_spec(routing), binding="primary")
    b = await manager.async_acquire(_spec(routing), binding="vision")
    assert a is b and len(built) == 1


async def test_concurrent_acquires_build_once(mgr, routing):
    gate = threading.Event()
    built = []

    def slow_builder(spec):
        gate.wait(5)
        client = SyncClient(spec.model)
        built.append(client)
        return client

    manager = mgr.ProviderManager(_Hass(), builder=slow_builder)
    tasks = [asyncio.ensure_future(manager.async_acquire(_spec(routing), binding=f"b{i}"))
             for i in range(5)]
    await asyncio.sleep(0.05)
    gate.set()
    clients = await asyncio.gather(*tasks)
    assert len(built) == 1
    assert all(c is clients[0] for c in clients)


async def test_credential_change_is_a_different_client(mgr, routing):
    manager, built = _manager(mgr)
    a = await manager.async_acquire(_spec(routing, key="old"), binding="primary")
    b = await manager.async_acquire(_spec(routing, key="new"), binding="primary")
    assert a is not b
    await manager._drain()
    assert a.closes == 1 and b.closes == 0


def test_fingerprint_hides_the_credential(routing):
    spec = _spec(routing, key="sk-SECRET")
    assert "sk-SECRET" not in spec.fingerprint()
    assert "sk-SECRET" not in repr(spec)


# ── Replacement ─────────────────────────────────────────────────────────────

async def test_replacement_closes_the_old_client_exactly_once(mgr, routing):
    manager, _ = _manager(mgr)
    old = await manager.async_acquire(_spec(routing, "a"), binding="primary")
    await manager.async_acquire(_spec(routing, "b"), binding="primary")
    await manager.async_acquire(_spec(routing, "c"), binding="primary")
    await manager._drain()
    assert old.closes == 1
    assert manager.client_count() == 1


async def test_replaced_client_closes_only_after_its_in_flight_call(mgr, routing, activity):
    manager, _ = _manager(mgr)
    old = await manager.async_acquire(_spec(routing, "a"), binding="primary")
    manager.call_started(old)
    await manager.async_acquire(_spec(routing, "b"), binding="primary")
    await manager._drain()
    assert old.closes == 0            # still serving a call
    manager.call_finished(old)
    await manager._drain()
    assert old.closes == 1


async def test_a_client_still_bound_elsewhere_is_not_closed(mgr, routing):
    manager, _ = _manager(mgr)
    shared = await manager.async_acquire(_spec(routing), binding="primary")
    await manager.async_acquire(_spec(routing), binding="vision")
    await manager.async_acquire(_spec(routing, "other"), binding="vision")
    await manager._drain()
    assert shared.closes == 0


async def test_lease_pins_a_client_across_rebinding(mgr, routing):
    manager, _ = _manager(mgr)
    async with manager.lease(_spec(routing, "a"), binding="vision") as leased:
        await manager.async_acquire(_spec(routing, "b"), binding="vision")
        await manager._drain()
        assert leased.closes == 0     # the analysis holding it is not cut off
    await manager._drain()
    assert leased.closes == 1


async def test_unbound_lease_closes_when_released(mgr, routing):
    manager, _ = _manager(mgr)
    async with manager.lease(_spec(routing, "turn")) as client:
        pass
    await manager._drain()
    assert client.closes == 1 and manager.client_count() == 0


async def test_acquire_all_is_all_or_nothing(mgr, routing):
    manager, _ = _manager(mgr)
    first = await manager.async_acquire_all({
        "observer_classifier": (_spec(routing, "c1"), None),
        "observer_reasoning": (_spec(routing, "r1"), None),
    })

    def boom():
        raise RuntimeError("bad key")

    with pytest.raises(RuntimeError):
        await manager.async_acquire_all({
            "observer_classifier": (_spec(routing, "c2"), None),
            "observer_reasoning": (_spec(routing, "r2"), boom),
        })
    await manager._drain()
    assert manager.bound("observer_classifier") is first["observer_classifier"]
    assert manager.bound("observer_reasoning") is first["observer_reasoning"]
    assert first["observer_classifier"].closes == 0
    assert manager.client_count() == 2          # the half-built attempt was closed


# ── Unload, reload and close styles ─────────────────────────────────────────

@pytest.mark.parametrize("factory", [SyncClient, AsyncClient])
async def test_close_closes_every_client_exactly_once(mgr, routing, factory):
    manager, built = _manager(mgr, factory)
    await manager.async_acquire(_spec(routing, "a"), binding="primary")
    await manager.async_acquire(_spec(routing, "b"), binding="vision")
    await manager.async_acquire(_spec(routing, "c"), binding="vision")   # replaces b
    await manager.async_close()
    await manager.async_close()                                          # idempotent
    assert [c.closes for c in built] == [1, 1, 1]


async def test_client_without_close_is_fine(mgr, routing):
    manager, _ = _manager(mgr, lambda model: NoCloseClient())
    await manager.async_acquire(_spec(routing), binding="primary")
    await manager.async_close()
    assert manager.close_errors == 0


async def test_failed_close_never_blocks_the_rest(mgr, routing):
    clients = iter([FailingClose("a"), SyncClient("b")])
    manager = mgr.ProviderManager(_Hass(), builder=lambda spec: next(clients))
    bad = await manager.async_acquire(_spec(routing, "a"), binding="x")
    good = await manager.async_acquire(_spec(routing, "b"), binding="y")
    await manager.async_close()
    assert bad.closes == 1 and good.closes == 1
    assert manager.close_errors == 1


async def test_acquire_after_close_is_refused(mgr, routing, load):
    errors = load("providers.errors")
    manager, _ = _manager(mgr)
    await manager.async_close()
    with pytest.raises(errors.ProviderError) as info:
        await manager.async_acquire(_spec(routing), binding="primary")
    assert info.value.kind is errors.ProviderErrorKind.PROVIDER_UNAVAILABLE


async def test_build_finishing_during_unload_is_closed(mgr, routing):
    gate = threading.Event()
    built = []

    def slow(spec):
        gate.wait(5)
        client = SyncClient()
        built.append(client)
        return client

    manager = mgr.ProviderManager(_Hass(), builder=slow)
    pending = asyncio.ensure_future(manager.async_acquire(_spec(routing), binding="primary"))
    await asyncio.sleep(0.05)
    closing = asyncio.ensure_future(manager.async_close())
    await asyncio.sleep(0.01)
    gate.set()
    await closing
    with pytest.raises(Exception):
        await pending
    assert built[0].closes == 1


async def test_reload_builds_fresh_clients(mgr, routing):
    first, built1 = _manager(mgr)
    old = await first.async_acquire(_spec(routing), binding="primary")
    await first.async_close()                     # unload
    second, _built2 = _manager(mgr)               # the reloaded entry's manager
    new = await second.async_acquire(_spec(routing), binding="primary")
    assert new is not old and old.closes == 1 and new.closes == 0
    await second.async_close()


async def test_resources_close_all_awaits_the_manager(mgr, routing, load):
    resources = load("resources").NovaResources()
    manager, built = _manager(mgr)
    resources.add_async_closeable(manager)
    await manager.async_acquire(_spec(routing), binding="primary")
    summary = await resources.async_close_all()
    assert summary["async_closeables"] == 1
    assert built[0].closes == 1
    assert (await resources.async_close_all())["async_closeables"] == 0


async def test_provider_scope_without_runtime_is_transient(mgr, routing):
    hass = _Hass()
    async with mgr.provider_scope(hass) as scope:
        client = await scope.async_acquire(_spec(routing), binding="x", factory=SyncClient)
    assert client.closes == 1
    assert scope.closed


# ── Cancellation ─────────────────────────────────────────────────────────────

async def test_cancelled_acquire_does_not_leak_the_client(mgr, routing):
    gate = threading.Event()
    built = []

    def slow(spec):
        gate.wait(5)
        client = SyncClient()
        built.append(client)
        return client

    manager = mgr.ProviderManager(_Hass(), builder=slow)
    task = asyncio.ensure_future(manager.async_acquire(_spec(routing), binding="primary"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if built and built[0].closes:
            break
    await manager._drain()
    assert built[0].closes == 1
    assert manager.bound("primary") is None


async def test_cancelled_waiter_does_not_cancel_a_shared_build(mgr, routing):
    gate = threading.Event()

    def slow(spec):
        gate.wait(5)
        return SyncClient()

    manager = mgr.ProviderManager(_Hass(), builder=slow)
    first = asyncio.ensure_future(manager.async_acquire(_spec(routing), binding="a"))
    second = asyncio.ensure_future(manager.async_acquire(_spec(routing), binding="b"))
    await asyncio.sleep(0.05)
    first.cancel()
    gate.set()
    client = await second
    await manager._drain()
    assert client.closes == 0 and manager.bound("b") is client


async def test_cancelled_chat_propagates_and_is_not_recorded(mgr, routing, activity, monkeypatch):
    started, release = threading.Event(), threading.Event()
    recorded = []

    class Slow(SyncClient):
        def chat(self, messages, **kw):
            started.set()
            release.wait(5)
            return {"text": "late", "tool_calls": [], "usage": {}}

    manager = mgr.ProviderManager(_Hass(), builder=lambda spec: Slow())
    client = await manager.async_acquire(_spec(routing), binding="primary")

    async def fake_record(hass, **kw):
        recorded.append(kw)

    monkeypatch.setattr(activity, "_record", fake_record)
    task = asyncio.ensure_future(activity.execute_chat(
        _Hass(), client, [{"role": "user", "content": "hi"}],
        role="llm", data_category="text"))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorded == []

    # The SDK call is still running: replacing the client must not close it
    # until that call has really finished.
    await manager.async_acquire(_spec(routing, "next"), binding="primary")
    await manager._drain()
    assert client.closes == 0
    release.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if client.closes:
            break
    await manager._drain()
    assert client.closes == 1


# ── Concurrency policy ──────────────────────────────────────────────────────

async def test_adapter_concurrency_policy_bounds_in_flight_calls(load, activity):
    ollama = load("providers.ollama")
    in_flight, peak = 0, 0
    lock = threading.Lock()
    release = threading.Event()

    class Bounded(ollama.OllamaProvider):
        def __init__(self):
            object.__init__(self)
            self.model = "m"
            self.api_key = ""
            self.base_url = "http://127.0.0.1:1"

        def complete(self, request):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            release.wait(5)
            with lock:
                in_flight -= 1
            from jc.providers.models import ChatResponse
            return ChatResponse(text="ok", provider="ollama", model="m")

    provider = Bounded()
    limit = provider.concurrency.max_in_flight
    calls = [asyncio.ensure_future(activity.execute_chat(
        _Hass(), provider, [{"role": "user", "content": "x"}],
        role="llm", data_category="text")) for _ in range(limit + 3)]
    await asyncio.sleep(0.2)
    assert peak == limit
    release.set()
    await asyncio.gather(*calls)


def test_every_adapter_declares_a_concurrency_policy(load):
    registry = load("providers.registry")
    for desc in registry.DESCRIPTORS.values():
        policy = desc.adapter.concurrency
        assert policy.max_in_flight >= 1 and policy.reason
