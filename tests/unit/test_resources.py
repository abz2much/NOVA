"""Acceptance tests for the per-entry disposal registry (resources.py).

Written against the behaviour spec, not the implementation: registration
filters, teardown order, method choice, error isolation, counts, idempotence,
and the documented quirks.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import warnings

import pytest


@pytest.fixture
def mod(load):
    return load("resources")


@pytest.fixture
def reg(mod):
    return mod.NovaResources()


class Journal:
    """Collects the order in which things were disposed."""

    def __init__(self):
        self.events: list[str] = []

    def fn(self, tag):
        return lambda: self.events.append(tag)


class Shut:
    def __init__(self, j, tag):
        self.j, self.tag = j, tag

    def shutdown(self):
        self.j.events.append(f"shutdown:{self.tag}")


class Clos:
    def __init__(self, j, tag):
        self.j, self.tag = j, tag

    def close(self):
        self.j.events.append(f"close:{self.tag}")


class AClos:
    def __init__(self, j, tag, exc=None):
        self.j, self.tag, self.exc = j, tag, exc

    async def async_close(self):
        self.j.events.append(f"aclose:{self.tag}")
        if self.exc is not None:
            raise self.exc


def _raiser(exc):
    def _f():
        raise exc
    return _f


# ── Interface ───────────────────────────────────────────────────────────────

def test_fresh_registry_has_three_empty_lists(reg):
    assert reg._unsubs == [] and isinstance(reg._unsubs, list)
    assert reg._closeables == [] and isinstance(reg._closeables, list)
    assert reg._async_closeables == [] and isinstance(reg._async_closeables, list)


def test_registries_are_independent(mod):
    a, b = mod.NovaResources(), mod.NovaResources()
    a.add_unsub(lambda: None)
    assert b._unsubs == []


def test_module_logger_exists(mod):
    assert isinstance(mod._LOGGER, logging.Logger)


def test_init_can_be_wrapped(mod, monkeypatch):
    seen = []
    real = mod.NovaResources.__init__

    def wrapper(self, *a, **kw):
        real(self, *a, **kw)
        seen.append(self)

    monkeypatch.setattr(mod.NovaResources, "__init__", wrapper)
    r = mod.NovaResources()
    assert seen == [r] and r._unsubs == []


def test_add_methods_return_none(reg):
    assert reg.add_unsub(lambda: None) is None
    assert reg.add_unsubs([lambda: None]) is None
    assert reg.add_closeable(object()) is None
    assert reg.add_async_closeable(AClos(Journal(), "x")) is None


# ── Registration filters ────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "text", 42, object(), [], {}])
def test_add_unsub_ignores_non_callables(reg, value):
    reg.add_unsub(value)
    assert reg._unsubs == []


def test_add_unsub_accepts_any_callable(reg):
    class Callable_:
        def __call__(self):
            pass

    class Something:
        pass

    inst = Callable_()
    func = lambda: None  # noqa: E731
    reg.add_unsub(func)
    reg.add_unsub(inst)
    reg.add_unsub(Something)
    reg.add_unsub(print)
    assert reg._unsubs == [func, inst, Something, print]


@pytest.mark.parametrize("value", [None, [], (), 0, ""])
def test_add_unsubs_falsy_is_nothing(reg, value):
    reg.add_unsubs(value)
    assert reg._unsubs == []


def test_add_unsubs_filters_each_element_in_order(reg):
    f1, f2 = (lambda: 1), (lambda: 2)
    reg.add_unsubs([f1, None, "x", f2, 3])
    assert reg._unsubs == [f1, f2]


def test_add_unsubs_accepts_tuples_and_generators(reg):
    f1, f2, f3 = (lambda: 1), (lambda: 2), (lambda: 3)
    reg.add_unsubs((f1,))
    reg.add_unsubs(f for f in [f2, f3])
    assert reg._unsubs == [f1, f2, f3]


def test_add_unsubs_with_bare_callable_raises_type_error(reg):
    with pytest.raises(TypeError):
        reg.add_unsubs(lambda: None)
    assert reg._unsubs == []


def test_add_closeable_rejects_only_none(reg):
    bare = object()
    reg.add_closeable(None)
    for v in (0, "", [], bare):
        reg.add_closeable(v)
    assert len(reg._closeables) == 4
    assert reg._closeables[3] is bare


def test_add_async_closeable_requires_callable_async_close(reg):
    j = Journal()
    good = AClos(j, "g")

    class NotCallable:
        async_close = "nope"

    class Missing:
        def close(self):
            pass

    reg.add_async_closeable(None)
    reg.add_async_closeable(NotCallable())
    reg.add_async_closeable(Missing())
    reg.add_async_closeable(good)
    assert reg._async_closeables == [good]


def test_add_async_closeable_accepts_sync_async_close(reg):
    class SyncOne:
        def async_close(self):
            pass

    obj = SyncOne()
    reg.add_async_closeable(obj)
    assert reg._async_closeables == [obj]


def test_add_async_closeable_lookup_error_propagates(reg):
    class Exploding:
        @property
        def async_close(self):
            raise RuntimeError("lookup failed")

    with pytest.raises(RuntimeError):
        reg.add_async_closeable(Exploding())
    assert reg._async_closeables == []


def test_duplicates_are_kept_and_disposed_each_time(reg):
    j = Journal()
    f = j.fn("u")
    obj = Shut(j, "s")
    reg.add_unsub(f)
    reg.add_unsub(f)
    reg.add_closeable(obj)
    reg.add_closeable(obj)
    summary = reg.close_all()
    assert j.events == ["u", "u", "shutdown:s", "shutdown:s"]
    assert summary == {"unsubs": 2, "closeables": 2, "errors": 0}


# ── close_all: order, method choice, counts ─────────────────────────────────

def test_close_all_order_is_unsubs_then_closeables_fifo(reg):
    j = Journal()
    reg.add_closeable(Shut(j, "c1"))
    reg.add_unsub(j.fn("u1"))
    reg.add_closeable(Clos(j, "c2"))
    reg.add_unsub(j.fn("u2"))
    reg.close_all()
    assert j.events == ["u1", "u2", "shutdown:c1", "close:c2"]


def test_close_all_summary_shape(reg):
    reg.add_unsub(lambda: None)
    reg.add_closeable(Clos(Journal(), "c"))
    summary = reg.close_all()
    assert list(summary) == ["unsubs", "closeables", "errors"]
    assert summary == {"unsubs": 1, "closeables": 1, "errors": 0}


def test_close_all_returns_fresh_dict_each_time(reg):
    a = reg.close_all()
    a["unsubs"] = 99
    assert reg.close_all() == {"unsubs": 0, "closeables": 0, "errors": 0}


def test_unsub_return_value_is_ignored(reg):
    reg.add_unsub(lambda: "anything")
    reg.add_unsub(lambda: False)
    assert reg.close_all()["unsubs"] == 2


def test_shutdown_preferred_over_close(reg):
    j = Journal()

    class Both(Shut):
        def close(self):
            j.events.append("close:both")

    reg.add_closeable(Both(j, "both"))
    reg.close_all()
    assert j.events == ["shutdown:both"]


def test_no_fallback_to_close_when_shutdown_raises(reg):
    j = Journal()

    class Both:
        def shutdown(self):
            raise ValueError("bad")

        def close(self):
            j.events.append("close")

    reg.add_closeable(Both())
    summary = reg.close_all()
    assert j.events == []
    assert summary == {"unsubs": 0, "closeables": 0, "errors": 1}


def test_falsy_shutdown_attribute_falls_through_to_close(reg):
    j = Journal()

    class Disabled:
        shutdown = None

        def close(self):
            j.events.append("close")

    reg.add_closeable(Disabled())
    assert reg.close_all()["closeables"] == 1
    assert j.events == ["close"]


def test_truthy_non_callable_shutdown_blocks_close(reg):
    j = Journal()

    class Odd:
        shutdown = "yes"

        def close(self):
            j.events.append("close")

    reg.add_closeable(Odd())
    summary = reg.close_all()
    assert j.events == []
    assert summary == {"unsubs": 0, "closeables": 0, "errors": 0}
    assert reg._closeables == []


def test_closeable_without_methods_is_skipped_silently(reg):
    reg.add_closeable(object())
    reg.add_closeable(0)
    summary = reg.close_all()
    assert summary == {"unsubs": 0, "closeables": 0, "errors": 0}
    assert reg._closeables == []


def test_attribute_error_during_lookup_counts_as_missing(reg):
    j = Journal()

    class Prop:
        @property
        def shutdown(self):
            raise AttributeError("not here")

        def close(self):
            j.events.append("close")

    reg.add_closeable(Prop())
    assert reg.close_all() == {"unsubs": 0, "closeables": 1, "errors": 0}
    assert j.events == ["close"]


def test_other_error_during_lookup_counts_as_error(reg):
    j = Journal()

    class Prop:
        @property
        def shutdown(self):
            raise RuntimeError("broken")

    reg.add_closeable(Prop())
    reg.add_closeable(Clos(j, "next"))
    assert reg.close_all() == {"unsubs": 0, "closeables": 1, "errors": 1}
    assert j.events == ["close:next"]


# ── close_all: error isolation ──────────────────────────────────────────────

def test_failures_are_isolated_and_counted(reg):
    j = Journal()
    reg.add_unsub(_raiser(RuntimeError("u")))
    reg.add_unsub(j.fn("u-ok"))
    reg.add_closeable(Shut(j, "ok"))

    class BadClose:
        def close(self):
            raise OSError("c")

    reg.add_closeable(BadClose())
    reg.add_closeable(Clos(j, "after"))
    summary = reg.close_all()
    assert j.events == ["u-ok", "shutdown:ok", "close:after"]
    assert summary == {"unsubs": 1, "closeables": 2, "errors": 2}


def test_failed_items_are_forgotten_not_retried(reg):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise RuntimeError

    reg.add_unsub(flaky)
    assert reg.close_all()["errors"] == 1
    assert reg.close_all() == {"unsubs": 0, "closeables": 0, "errors": 0}
    assert calls["n"] == 1


def test_each_failure_is_logged_with_its_exception(mod, reg, caplog):
    # 8.49.10: failures used to be counted silently.
    caplog.set_level(logging.DEBUG)
    after = []
    reg.add_unsub(_raiser(RuntimeError("unsub boom")))
    reg.add_unsub(lambda: after.append("unsub"))
    reg.add_closeable(type("B", (), {"close": lambda self: 1 / 0})())
    reg.add_closeable(type("C", (), {"close": lambda self: after.append("close")})())
    summary = reg.close_all()
    assert summary == {"unsubs": 1, "closeables": 1, "errors": 2}
    assert after == ["unsub", "close"]
    records = [r for r in caplog.records if r.name == mod._LOGGER.name]
    assert [r.levelno for r in records] == [logging.WARNING, logging.WARNING]
    assert isinstance(records[0].exc_info[1], RuntimeError)
    assert isinstance(records[1].exc_info[1], ZeroDivisionError)


async def test_async_failure_is_logged_with_its_exception(mod, reg, caplog):
    caplog.set_level(logging.DEBUG)
    reg.add_async_closeable(AClos(Journal(), "a", exc=RuntimeError("x")))
    reg.add_async_closeable(AClos(Journal(), "b"))
    summary = await reg.async_close_all()
    assert summary["async_closeables"] == 1 and summary["errors"] == 1
    records = [r for r in caplog.records if r.name == mod._LOGGER.name]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    assert isinstance(records[0].exc_info[1], RuntimeError)


def test_success_logs_nothing(reg, caplog):
    caplog.set_level(logging.DEBUG)
    reg.add_unsub(lambda: None)
    reg.add_closeable(type("C", (), {"close": lambda self: None})())
    reg.close_all()
    assert caplog.records == []


# ── close_all: forgetting, idempotence, reuse ───────────────────────────────

def test_lists_cleared_in_place(reg):
    u_list, c_list = reg._unsubs, reg._closeables
    reg.add_unsub(lambda: None)
    reg.add_closeable(object())
    reg.close_all()
    assert reg._unsubs is u_list and u_list == []
    assert reg._closeables is c_list and c_list == []


def test_second_close_all_is_a_no_op(reg):
    j = Journal()
    reg.add_unsub(j.fn("u"))
    reg.add_closeable(Shut(j, "s"))
    reg.close_all()
    assert reg.close_all() == {"unsubs": 0, "closeables": 0, "errors": 0}
    assert j.events == ["u", "shutdown:s"]


def test_empty_registry_close_all(reg):
    assert reg.close_all() == {"unsubs": 0, "closeables": 0, "errors": 0}


def test_registry_reusable_after_teardown(reg):
    j = Journal()
    reg.add_unsub(j.fn("first"))
    reg.close_all()
    reg.add_unsub(j.fn("second"))
    assert reg._unsubs != []
    assert reg.close_all()["unsubs"] == 1
    assert j.events == ["first", "second"]


def test_close_all_leaves_async_closeables_alone(reg):
    j = Journal()
    a = AClos(j, "a")
    reg.add_async_closeable(a)
    reg.close_all()
    assert reg._async_closeables == [a]
    assert j.events == []


def test_directly_appended_non_callable_unsub_is_skipped(reg):
    reg._unsubs.append("not callable")
    reg.add_unsub(lambda: None)
    assert reg.close_all() == {"unsubs": 1, "closeables": 0, "errors": 0}
    assert reg._unsubs == []


# ── close_all: registration during teardown ─────────────────────────────────

def test_unsub_added_during_unsub_pass_runs_same_call(reg):
    j = Journal()

    def first():
        j.events.append("first")
        reg.add_unsub(j.fn("late"))

    reg.add_unsub(first)
    assert reg.close_all()["unsubs"] == 2
    assert j.events == ["first", "late"]
    assert reg._unsubs == []


def test_closeable_added_during_closeable_pass_runs_same_call(reg):
    j = Journal()

    class Spawner:
        def shutdown(self):
            j.events.append("spawner")
            reg.add_closeable(Clos(j, "late"))

    reg.add_closeable(Spawner())
    assert reg.close_all()["closeables"] == 2
    assert j.events == ["spawner", "close:late"]


def test_unsub_added_during_closeable_pass_is_discarded(reg):
    j = Journal()

    class LateUnsub:
        def shutdown(self):
            reg.add_unsub(j.fn("never"))

    reg.add_closeable(LateUnsub())
    assert reg.close_all() == {"unsubs": 0, "closeables": 1, "errors": 0}
    assert reg._unsubs == []
    reg.close_all()
    assert j.events == []


# ── close_all: BaseException ────────────────────────────────────────────────

def test_base_exception_propagates_and_nothing_is_cleared(reg):
    j = Journal()
    reg.add_unsub(j.fn("before"))
    reg.add_unsub(_raiser(KeyboardInterrupt()))
    reg.add_unsub(j.fn("after"))
    reg.add_closeable(Shut(j, "c"))
    with pytest.raises(KeyboardInterrupt):
        reg.close_all()
    assert j.events == ["before"]
    assert len(reg._unsubs) == 3 and len(reg._closeables) == 1


def test_retry_after_base_exception_revisits_everything(reg):
    j = Journal()
    state = {"armed": True}

    def trap():
        if state["armed"]:
            state["armed"] = False
            raise SystemExit
        j.events.append("trap")

    reg.add_unsub(j.fn("first"))
    reg.add_unsub(trap)
    with pytest.raises(SystemExit):
        reg.close_all()
    summary = reg.close_all()
    assert j.events == ["first", "first", "trap"]
    assert summary == {"unsubs": 2, "closeables": 0, "errors": 0}


# ── async_close_all ─────────────────────────────────────────────────────────

async def test_async_close_all_full_order(reg):
    j = Journal()
    reg.add_async_closeable(AClos(j, "a1"))
    reg.add_closeable(Clos(j, "c1"))
    reg.add_unsub(j.fn("u1"))
    reg.add_async_closeable(AClos(j, "a2"))
    reg.add_closeable(Shut(j, "c2"))
    summary = await reg.async_close_all()
    assert j.events == ["u1", "close:c1", "shutdown:c2", "aclose:a1", "aclose:a2"]
    assert list(summary) == ["unsubs", "closeables", "errors", "async_closeables"]
    assert summary == {"unsubs": 1, "closeables": 2, "errors": 0,
                       "async_closeables": 2}


async def test_async_closeables_awaited_one_at_a_time(reg):
    j = Journal()

    class Slow:
        def __init__(self, tag):
            self.tag = tag

        async def async_close(self):
            j.events.append(f"start:{self.tag}")
            await asyncio.sleep(0)
            j.events.append(f"end:{self.tag}")

    reg.add_async_closeable(Slow("a"))
    reg.add_async_closeable(Slow("b"))
    await reg.async_close_all()
    assert j.events == ["start:a", "end:a", "start:b", "end:b"]


async def test_async_errors_isolated_and_summed(reg):
    j = Journal()
    reg.add_unsub(_raiser(RuntimeError("sync")))
    reg.add_async_closeable(AClos(j, "bad", exc=ValueError("async")))
    reg.add_async_closeable(AClos(j, "good"))
    summary = await reg.async_close_all()
    assert j.events == ["aclose:bad", "aclose:good"]
    assert summary == {"unsubs": 0, "closeables": 0, "errors": 2,
                       "async_closeables": 1}


async def test_async_close_all_is_idempotent(reg):
    j = Journal()
    reg.add_async_closeable(AClos(j, "a"))
    reg.add_unsub(j.fn("u"))
    await reg.async_close_all()
    again = await reg.async_close_all()
    assert again == {"unsubs": 0, "closeables": 0, "errors": 0,
                     "async_closeables": 0}
    assert j.events == ["u", "aclose:a"]


async def test_async_close_all_on_empty_registry(reg):
    assert await reg.async_close_all() == {
        "unsubs": 0, "closeables": 0, "errors": 0, "async_closeables": 0}


async def test_async_list_is_replaced_not_cleared(reg):
    old = reg._async_closeables
    a = AClos(Journal(), "a")
    reg.add_async_closeable(a)
    await reg.async_close_all()
    assert reg._async_closeables == [] and reg._async_closeables is not old
    assert old == [a]


async def test_failed_async_closeable_is_forgotten(reg):
    calls = {"n": 0}

    class Bad:
        async def async_close(self):
            calls["n"] += 1
            raise RuntimeError

    reg.add_async_closeable(Bad())
    await reg.async_close_all()
    await reg.async_close_all()
    assert calls["n"] == 1 and reg._async_closeables == []


async def test_sync_async_close_runs_but_counts_as_error(reg):
    j = Journal()

    class SyncOne:
        def async_close(self):
            j.events.append("ran")

    reg.add_async_closeable(SyncOne())
    summary = await reg.async_close_all()
    assert j.events == ["ran"]
    assert summary["async_closeables"] == 0 and summary["errors"] == 1


async def test_registrations_during_async_part_wait_for_next_teardown(reg):
    j = Journal()

    class Spawner:
        async def async_close(self):
            j.events.append("spawner")
            reg.add_unsub(j.fn("late-unsub"))
            reg.add_closeable(Clos(j, "late"))
            reg.add_async_closeable(AClos(j, "late"))

    reg.add_async_closeable(Spawner())
    first = await reg.async_close_all()
    assert first == {"unsubs": 0, "closeables": 0, "errors": 0,
                     "async_closeables": 1}
    assert len(reg._unsubs) == 1 and len(reg._closeables) == 1
    assert len(reg._async_closeables) == 1
    second = await reg.async_close_all()
    assert second == {"unsubs": 1, "closeables": 1, "errors": 0,
                      "async_closeables": 1}
    assert j.events == ["spawner", "late-unsub", "close:late", "aclose:late"]


async def test_base_exception_in_sync_part_skips_async_part(reg):
    j = Journal()
    a = AClos(j, "a")
    reg.add_unsub(_raiser(KeyboardInterrupt()))
    reg.add_async_closeable(a)
    with pytest.raises(KeyboardInterrupt):
        await reg.async_close_all()
    assert j.events == [] and reg._async_closeables == [a]


async def test_cancellation_in_async_part_loses_the_rest(reg):
    j = Journal()
    reg.add_async_closeable(AClos(j, "first"))
    reg.add_async_closeable(AClos(j, "cancel", exc=asyncio.CancelledError()))
    reg.add_async_closeable(AClos(j, "never"))
    with pytest.raises(asyncio.CancelledError):
        await reg.async_close_all()
    assert reg._async_closeables == []
    summary = await reg.async_close_all()
    assert summary["async_closeables"] == 0
    assert j.events == ["aclose:first", "aclose:cancel"]


# ── Coroutine functions used as sync disposers ──────────────────────────────

def test_coroutine_unsub_and_closeable_count_but_never_run(reg):
    j = Journal()

    async def async_unsub():
        j.events.append("unsub-body")

    class AsyncShutdown:
        async def shutdown(self):
            j.events.append("shutdown-body")

    reg.add_unsub(async_unsub)
    reg.add_closeable(AsyncShutdown())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        summary = reg.close_all()
        gc.collect()
    assert summary == {"unsubs": 1, "closeables": 1, "errors": 0}
    assert j.events == []
