"""Acceptance tests for boot_guard.AlertBuffer, written from its behaviour spec.

Stdlib-only module; loaded through the conftest `load` fixture (no Home
Assistant needed).
"""
from __future__ import annotations

import ast
import asyncio
import logging
import pathlib

import pytest

SRC = (pathlib.Path(__file__).resolve().parents[2]
       / "custom_components" / "nova" / "boot_guard.py")


@pytest.fixture
def mod(load):
    return load("boot_guard")


def _recorder():
    got: list[dict] = []

    async def dispatch(item):
        got.append(item)

    return got, dispatch


def _fill(buf, n, start=0):
    for i in range(start, start + n):
        buf.enqueue({"n": i})


# ── Interface ────────────────────────────────────────────────────────────────

def test_module_is_stdlib_only():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            roots.add(node.module.split(".")[0])
    assert roots <= {"__future__", "asyncio", "logging", "typing", "collections"}
    assert "homeassistant" not in roots


def test_constants_and_names(mod):
    assert mod.DEFAULT_MAX == 25 and type(mod.DEFAULT_MAX) is int
    assert isinstance(mod._LOGGER, logging.Logger)
    assert mod._LOGGER.name == mod.__name__
    assert asyncio.iscoroutinefunction(mod.AlertBuffer.mark_ready)
    for sync in ("begin", "enqueue", "pending"):
        assert not asyncio.iscoroutinefunction(getattr(mod.AlertBuffer, sync))


def test_fresh_buffer_state_without_running_loop(mod):
    buf = mod.AlertBuffer()
    assert buf.ready is False
    assert buf.pending() == 0
    assert isinstance(buf._queue, asyncio.Queue)
    assert buf._queue.qsize() == 0
    assert buf._queue.maxsize == mod.DEFAULT_MAX


def test_ready_is_a_plain_writable_attribute(mod):
    buf = mod.AlertBuffer()
    buf.ready = True
    assert buf.ready is True
    assert "ready" in vars(buf)


def test_default_capacity_bound_at_definition(mod, monkeypatch):
    monkeypatch.setattr(mod, "DEFAULT_MAX", 2)
    buf = mod.AlertBuffer()
    _fill(buf, 30)
    assert buf.pending() == 25


def test_maxsize_accepted_by_keyword_and_position(mod):
    assert mod.AlertBuffer(maxsize=4)._queue.maxsize == 4
    assert mod.AlertBuffer(7)._queue.maxsize == 7


# ── Enqueue ──────────────────────────────────────────────────────────────────

def test_pending_and_private_queue_agree(mod):
    buf = mod.AlertBuffer()
    _fill(buf, 4)
    assert buf.pending() == 4 == buf._queue.qsize()


def test_enqueue_returns_none(mod):
    assert mod.AlertBuffer().enqueue({"a": 1}) is None


async def test_enqueue_stores_a_shallow_copy(mod):
    buf = mod.AlertBuffer()
    nested = ["x"]
    src = {"text": "hello", "tags": nested}
    buf.enqueue(src)
    src["text"] = "changed"
    src["extra"] = True
    nested.append("y")
    got, dispatch = _recorder()
    await buf.mark_ready(dispatch)
    assert got[0] is not src
    assert got[0]["text"] == "hello"
    assert "extra" not in got[0]
    assert got[0]["tags"] is nested and nested == ["x", "y"]


async def test_enqueue_accepts_pairs_and_stores_dict(mod):
    buf = mod.AlertBuffer()
    buf.enqueue([("k", 1), ("j", 2)])
    got, dispatch = _recorder()
    await buf.mark_ready(dispatch)
    assert got == [{"k": 1, "j": 2}] and type(got[0]) is dict


@pytest.mark.parametrize("bad", [None, 5, [1, 2]])
def test_enqueue_rejects_non_mapping_and_keeps_state(mod, bad):
    buf = mod.AlertBuffer(maxsize=2)
    buf.enqueue({"n": 0})
    with pytest.raises((TypeError, ValueError)):
        buf.enqueue(bad)
    assert buf.pending() == 1


def test_enqueue_rejects_bad_item_even_when_full(mod):
    buf = mod.AlertBuffer(maxsize=2)
    _fill(buf, 2)
    with pytest.raises(TypeError):
        buf.enqueue(None)
    assert buf.pending() == 2


async def test_enqueue_while_ready_still_buffers(mod):
    buf = mod.AlertBuffer()
    got, dispatch = _recorder()
    await buf.mark_ready(dispatch)
    buf.enqueue({"n": "late"})
    assert buf.pending() == 1
    # Idempotent while ready: not drained, dispatch not called.
    assert await buf.mark_ready(dispatch) == 0
    assert got == [] and buf.pending() == 1


# ── Overflow ─────────────────────────────────────────────────────────────────

async def test_overflow_evicts_oldest_first(mod):
    buf = mod.AlertBuffer(maxsize=4)
    _fill(buf, 9)
    assert buf.pending() == 4
    got, dispatch = _recorder()
    assert await buf.mark_ready(dispatch) == 4
    assert [g["n"] for g in got] == [5, 6, 7, 8]


async def test_capacity_one_keeps_only_newest(mod):
    buf = mod.AlertBuffer(maxsize=1)
    _fill(buf, 3)
    got, dispatch = _recorder()
    await buf.mark_ready(dispatch)
    assert got == [{"n": 2}]


async def test_default_capacity_is_25(mod):
    buf = mod.AlertBuffer()
    _fill(buf, 30)
    assert buf.pending() == 25
    got, dispatch = _recorder()
    await buf.mark_ready(dispatch)
    assert [g["n"] for g in got] == list(range(5, 30))


def test_overflow_logs_a_warning_only_when_dropping(mod, caplog):
    buf = mod.AlertBuffer(maxsize=2)
    with caplog.at_level(logging.WARNING, logger=mod._LOGGER.name):
        _fill(buf, 2)
        assert not [r for r in caplog.records if r.name == mod._LOGGER.name]
        buf.enqueue({"n": 99})
    warns = [r for r in caplog.records
             if r.name == mod._LOGGER.name and r.levelno == logging.WARNING]
    assert len(warns) == 1


def test_overflow_internal_error_is_swallowed(mod, monkeypatch):
    buf = mod.AlertBuffer(maxsize=1)
    buf.enqueue({"n": 0})

    def boom():
        raise RuntimeError("queue broke")
    monkeypatch.setattr(buf._queue, "get_nowait", boom)
    buf.enqueue({"n": 1})  # must not raise
    assert buf.pending() == 1


@pytest.mark.parametrize("size", [0, -3])
def test_non_positive_maxsize_is_unbounded(mod, size):
    buf = mod.AlertBuffer(maxsize=size)
    _fill(buf, 60)
    assert buf.pending() == 60


# ── Begin / readiness ────────────────────────────────────────────────────────

async def test_begin_regates_and_keeps_held_items(mod):
    buf = mod.AlertBuffer()
    _fill(buf, 2)
    buf.begin()
    buf.begin()
    assert buf.ready is False and buf.pending() == 2
    got, dispatch = _recorder()
    await buf.mark_ready(dispatch)
    assert buf.begin() is None
    assert buf.ready is False
    _fill(buf, 1, start=10)
    assert await buf.mark_ready(dispatch) == 1
    assert [g["n"] for g in got] == [0, 1, 10]


async def test_items_left_while_ready_replay_after_regate(mod):
    buf = mod.AlertBuffer()
    got, dispatch = _recorder()
    await buf.mark_ready(dispatch)
    _fill(buf, 2)
    buf.begin()
    _fill(buf, 1, start=2)
    assert await buf.mark_ready(dispatch) == 3
    assert [g["n"] for g in got] == [0, 1, 2]


async def test_empty_replay_flips_ready_without_dispatch(mod):
    buf = mod.AlertBuffer()
    calls = []

    async def dispatch(item):
        calls.append(item)

    assert await buf.mark_ready(dispatch) == 0
    assert buf.ready is True and calls == []


# ── Replay ───────────────────────────────────────────────────────────────────

async def test_replay_in_arrival_order_and_count(mod):
    buf = mod.AlertBuffer()
    for word in ("alpha", "beta", "gamma"):
        buf.enqueue({"message": word, "target_area": "room"})
    got, dispatch = _recorder()
    result = await buf.mark_ready(dispatch)
    assert result == 3 and type(result) is int
    assert [g["message"] for g in got] == ["alpha", "beta", "gamma"]
    assert buf.ready is True and buf.pending() == 0


async def test_replay_is_sequential(mod):
    buf = mod.AlertBuffer()
    _fill(buf, 3)
    active = 0
    peak = 0

    async def dispatch(item):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        active -= 1

    assert await buf.mark_ready(dispatch) == 3
    assert peak == 1


async def test_ready_is_true_during_dispatch(mod):
    buf = mod.AlertBuffer()
    _fill(buf, 1)
    seen = []

    async def dispatch(item):
        seen.append((buf.ready, buf.pending()))

    await buf.mark_ready(dispatch)
    assert seen == [(True, 0)]


async def test_concurrent_mark_ready_second_returns_zero(mod):
    buf = mod.AlertBuffer()
    _fill(buf, 3)
    got = []

    async def slow(item):
        await asyncio.sleep(0)
        got.append(item["n"])

    first, second = await asyncio.gather(buf.mark_ready(slow), buf.mark_ready(slow))
    assert (first, second) == (3, 0)
    assert got == [0, 1, 2]


async def test_items_enqueued_during_drain_replay_in_same_call(mod):
    buf = mod.AlertBuffer()
    _fill(buf, 2)
    got = []

    async def dispatch(item):
        got.append(item["n"])
        if item["n"] == 0:
            buf.enqueue({"n": "late"})

    assert await buf.mark_ready(dispatch) == 3
    assert got == [0, 1, "late"]
    assert buf.pending() == 0


async def test_dispatch_receives_stored_dict(mod):
    buf = mod.AlertBuffer()
    buf.enqueue({"n": 1})
    stored = list(buf._queue._queue)[0]
    got, dispatch = _recorder()
    await buf.mark_ready(dispatch)
    assert got[0] is stored


async def test_dispatch_return_value_is_ignored(mod):
    buf = mod.AlertBuffer()
    _fill(buf, 2)

    async def dispatch(item):
        return "ignored"

    assert await buf.mark_ready(dispatch) == 2


# ── Failures ─────────────────────────────────────────────────────────────────

async def test_failing_dispatch_is_logged_skipped_and_not_counted(mod, caplog):
    buf = mod.AlertBuffer()
    _fill(buf, 4)
    got = []

    async def dispatch(item):
        if item["n"] in (1, 2):
            raise ValueError("cannot deliver")
        got.append(item["n"])

    with caplog.at_level(logging.ERROR, logger=mod._LOGGER.name):
        assert await buf.mark_ready(dispatch) == 2
    assert got == [0, 3]
    assert buf.pending() == 0 and buf.ready is True
    errors = [r for r in caplog.records
              if r.name == mod._LOGGER.name and r.levelno == logging.ERROR]
    assert len(errors) == 2 and all(r.exc_info for r in errors)

    # Failed items are gone for good: a re-gate replays nothing.
    buf.begin()
    assert await buf.mark_ready(dispatch) == 0


async def test_sync_dispatch_runs_but_counts_as_failure(mod):
    buf = mod.AlertBuffer()
    _fill(buf, 2)
    got = []

    def dispatch(item):
        got.append(item["n"])

    assert await buf.mark_ready(dispatch) == 0
    assert got == [0, 1] and buf.pending() == 0


async def test_base_exception_aborts_drain_and_keeps_rest(mod):
    buf = mod.AlertBuffer()
    _fill(buf, 3)
    got = []

    async def dispatch(item):
        if item["n"] == 1:
            raise asyncio.CancelledError
        got.append(item["n"])

    with pytest.raises(asyncio.CancelledError):
        await buf.mark_ready(dispatch)
    assert got == [0]
    # 8.49.10: the gate closes again so the leftover is not stranded.
    assert buf.ready is False
    assert buf.pending() == 1  # item 1 consumed, item 2 still held

    assert await buf.mark_ready(dispatch) == 1
    assert got == [0, 2]
    assert buf.ready is True and buf.pending() == 0


async def test_task_cancelled_mid_replay_leaves_rest_replayable(mod):
    buf = mod.AlertBuffer()
    _fill(buf, 3)
    started = asyncio.Event()
    got = []

    async def slow(item):
        started.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(buf.mark_ready(slow))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert buf.ready is False and buf.pending() == 2

    async def record(item):
        got.append(item["n"])

    assert await buf.mark_ready(record) == 2
    assert got == [1, 2]
