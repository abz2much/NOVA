"""Executor boundaries get a coherent runtime_config snapshot (PHACC).

NovaRuntime.runtime_config is owned by the event loop and changed in place
by panel writes. Each executor job that merges it into the effective
config gets a fresh shallow snapshot taken on the loop just before the job.

Each test holds that executor job mid-flight, changes the live dict on the
event loop, then lets the job finish, and proves:

* the in-flight job used its own snapshot (equal to the live dict when
  taken, never the live dict itself), so the change cannot leak into it,
* the next operation sees the new value (no cached snapshot).

Paths covered: the conversation reasoning fallback, the host-health and
sleep-prompt ticks, and lockdown setup.

Everything outward is faked, as in the files the fixtures come from. No
device is touched: there are no lock, alarm or cover entities here.
"""
import asyncio
import threading
import types

import pytest

from .test_conversation_runtime import (  # noqa: F401  (fixtures)
    _converse,
    _no_real_config_state,
    _setup as _setup_with_conversation,
    _text,
    fakes,
    isolated_memory,
)
from .test_init_runtime_consumers import (  # noqa: F401  (fixtures)
    _setup,
    _tick,
    before_lockdown,
    lockdown_calls,
    tick_calls,
)
from .test_runtime_data import (  # noqa: F401  (autouse fixtures)
    _no_real_config,
    _restore_nova_config,
)


@pytest.fixture
def gate(monkeypatch):
    """Wrap effective_config_with_runtime. When armed, the next call records
    what it was given, signals `started` and waits in its executor thread
    for `release` before doing the real merge."""
    from custom_components.nova import nova_config
    real = nova_config.effective_config_with_runtime
    g = types.SimpleNamespace(armed=False, started=threading.Event(),
                              release=threading.Event(), seen=[], merged=[])

    def _gated(entry=None, runtime_config=None):
        g.seen.append((runtime_config, dict(runtime_config or {})))
        if g.armed:
            g.armed = False
            assert threading.current_thread() is not threading.main_thread()
            g.started.set()
            assert g.release.wait(10), "test never released the executor job"
        out = real(entry, runtime_config)
        g.merged.append(out)
        return out

    monkeypatch.setattr(nova_config, "effective_config_with_runtime", _gated)
    yield g
    g.release.set()


async def _wait_started(hass, g):
    assert await hass.async_add_executor_job(g.started.wait, 10)


async def _hold_mutate_release(hass, g, live, key, new):
    """With the job parked in its executor thread, change the live dict on
    the event loop, then let the job finish."""
    await _wait_started(hass, g)
    given, when_given = g.seen[-1]
    assert given is not live            # the job never holds the live dict
    live[key] = new                     # panel write, on the event loop
    g.release.set()
    return given, when_given


# ── Scheduled ticks ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(("name", "key"), [
    ("host_health", "host_health_cpu_threshold"),
    ("sleep_prompt", "sleep_prompt_time"),
])
async def test_tick_in_flight_uses_its_snapshot(hass, tick_calls, gate, name, key):
    entry = await _setup(hass)
    live = entry.runtime_data.runtime_config
    tick = _tick(entry, name)
    live[key] = "first"

    gate.armed = True
    task = hass.async_create_task(tick(None))
    given, when_given = await _hold_mutate_release(hass, gate, live, key, "second")
    await task

    assert when_given[key] == "first"
    assert given == when_given                  # the snapshot never moved
    assert gate.merged[-1][key] == "first"
    assert tick_calls[name][-1][key] == "first"
    assert live[key] == "second"

    await tick(None)                            # the next tick is fresh
    assert tick_calls[name][-1][key] == "second"
    assert gate.seen[-1][0] is not given


# ── Lockdown setup ──────────────────────────────────────────────────────────

async def test_lockdown_in_flight_uses_its_snapshot(
        hass, lockdown_calls, before_lockdown, gate):
    from custom_components.nova import nova_config
    from custom_components.nova.const import DOMAIN
    seen = {}

    def _hook():
        runtime = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
        seen["live"] = runtime.runtime_config
        runtime.runtime_config["lockdown_on_alarm"] = "first"
        gate.armed = True          # the next merge is lockdown's

    before_lockdown.append(_hook)
    setup = hass.async_create_task(_setup(hass))
    await _wait_started(hass, gate)
    live = seen["live"]
    given, when_given = gate.seen[-1]
    assert given is not live
    live["lockdown_on_alarm"] = "second"
    gate.release.set()
    entry = await setup

    assert when_given["lockdown_on_alarm"] == "first"
    assert given == when_given
    assert [c["lockdown_on_alarm"] for c in lockdown_calls] == ["first"]

    # The next lockdown configuration read sees the new value.
    from custom_components.nova.runtime import runtime_config_snapshot
    nxt = await hass.async_add_executor_job(
        nova_config.effective_config_with_runtime, entry,
        runtime_config_snapshot(entry, strict=True))
    assert nxt["lockdown_on_alarm"] == "second"


# ── Conversation reasoning fallback ─────────────────────────────────────────

async def test_reasoning_fallback_in_flight_uses_its_snapshot(hass, fakes, gate):
    entry = await _setup_with_conversation(hass)
    live = entry.runtime_data.runtime_config
    live.update({"llm_provider": "ollama", "ollama_base_url": "http://first:11434"})

    gate.armed = True
    turn = hass.async_create_task(_converse(hass, entry, _text()))
    given, when_given = await _hold_mutate_release(
        hass, gate, live, "ollama_base_url", "http://second:11434")
    await turn

    assert when_given["ollama_base_url"] == "http://first:11434"
    assert given == when_given
    kw = fakes.agent_kwargs
    assert kw["config"]["ollama_base_url"] == "http://first:11434"

    await _converse(hass, entry, _text())       # the next turn is fresh
    assert fakes.agent_kwargs["config"]["ollama_base_url"] == "http://second:11434"
    assert gate.seen[-1][0] is not given


async def test_blocking_gate_really_runs_off_the_event_loop(hass, gate):
    """Guard for the tests above: the parked job is in a worker thread, so
    the event loop keeps running while it waits."""
    gate.armed = True
    from custom_components.nova import nova_config
    job = hass.async_add_executor_job(
        nova_config.effective_config_with_runtime, None, {"k": "v"})
    await _wait_started(hass, gate)
    await asyncio.sleep(0)               # the loop is free
    gate.release.set()
    assert (await job)["k"] == "v"
