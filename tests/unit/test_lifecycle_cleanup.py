"""Phase 0B lifecycle cleanup — the unit-level half.

The integration tests (tests/integration/test_lifecycle_cleanup.py) prove the
wiring against a real Home Assistant: what async_setup_entry registers and
async_unload_entry removes. This file proves the pieces with fakes:

* cognitive_core.stop() is idempotent (a second call never re-runs a remover),
* the lockdown alarm listener ensure_lockdown() registers is removed by stop(),
* bootstrap.schedule_bootstrap() returns a handle that cancels a pending
  start listener or a running task, and schedules nothing once shut down,
* every public service Nova registers is documented in services.yaml.

No device is touched: the bus, tasks and alarm sync are all fakes.

Focused run:
    python -m pytest tests/unit/test_lifecycle_cleanup.py -q
"""
import ast
import pathlib
import sys
import types

import pytest
import yaml

# Stub aiohttp + the HA aiohttp client helper before bootstrap is imported
# (same stubs as test_bootstrap.py; whichever file runs first installs them).
if "aiohttp" not in sys.modules:
    _aiohttp = types.ModuleType("aiohttp")
    _aiohttp.ClientTimeout = lambda **kw: None
    _aiohttp.ClientSession = object
    sys.modules["aiohttp"] = _aiohttp
if "homeassistant.helpers.aiohttp_client" not in sys.modules:
    _ac = types.ModuleType("homeassistant.helpers.aiohttp_client")
    _ac.async_get_clientsession = lambda hass: None
    sys.modules["homeassistant.helpers.aiohttp_client"] = _ac

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


# ── cognitive_core: lockdown listener and idempotent stop ───────────────────

class _CountingBus:
    def __init__(self):
        self.listening = 0
        self.removed = 0

    def async_listen(self, event_type, cb, *a, **k):
        self.listening += 1

        def _unsub():
            self.removed += 1
            self.listening -= 1
        return _unsub


@pytest.fixture
def core(cognitive_core, monkeypatch, tmp_path):
    monkeypatch.setattr(cognitive_core, "LOCKDOWN_STATE_PATH", str(tmp_path / "lockdown.json"))

    async def _no_sync(*a, **k):
        return None

    monkeypatch.setattr(cognitive_core, "_sync_lockdown_to_alarm", _no_sync)
    c = cognitive_core._CORE
    c.lockdown_mgr = None
    c.hass = None
    c.config = {}
    c.alarm_unsub = None
    c.unsub = None
    c.task = None
    yield cognitive_core
    c.alarm_unsub = None
    c.unsub = None
    c.task = None


async def test_ensure_lockdown_listener_is_removed_by_stop(core, fake_hass):
    bus = _CountingBus()
    fake_hass.bus = bus
    await core.ensure_lockdown(fake_hass, {})
    assert bus.listening == 1 and core._CORE.alarm_unsub is not None
    await core.stop()
    assert bus.listening == 0 and bus.removed == 1
    assert core._CORE.alarm_unsub is None


async def test_ensure_lockdown_is_idempotent(core, fake_hass):
    bus = _CountingBus()
    fake_hass.bus = bus
    await core.ensure_lockdown(fake_hass, {})
    await core.ensure_lockdown(fake_hass, {})
    assert bus.listening == 1


async def test_stop_twice_removes_each_listener_once(core):
    calls = {"unsub": 0, "alarm": 0}
    core._CORE.unsub = lambda: calls.__setitem__("unsub", calls["unsub"] + 1)
    core._CORE.alarm_unsub = lambda: calls.__setitem__("alarm", calls["alarm"] + 1)
    await core.stop()
    await core.stop()
    assert calls == {"unsub": 1, "alarm": 1}
    assert core._CORE.unsub is None and core._CORE.task is None


async def test_stop_without_start_is_safe(core):
    await core.stop()
    await core.stop()
    assert core._CORE.running is False


async def test_reload_reregisters_lockdown_after_stop(core, fake_hass):
    bus = _CountingBus()
    fake_hass.bus = bus
    await core.ensure_lockdown(fake_hass, {})
    await core.stop()
    await core.ensure_lockdown(fake_hass, {})
    assert bus.listening == 1


# ── reload gets a fresh lockdown runtime (observer disabled) ────────────────

async def _unload_core(core):
    """What async_unload_entry does for the core with the observer off."""
    await core.stop()
    core.release_runtime()


async def test_reload_builds_lockdown_from_new_hass_and_config(core):
    """Observer disabled: setup A, unload, setup B with a different hass and
    changed lockdown config. The core fixture stubs the alarm sync, so no
    lockdown action can run against either fake."""
    import json
    from fakes import FakeHass

    hass_a, hass_b = FakeHass(), FakeHass()
    bus_a, bus_b = _CountingBus(), _CountingBus()
    hass_a.bus, hass_b.bus = bus_a, bus_b
    config_a = {"security_alarm_entity": "alarm_control_panel.first"}
    config_b = {"security_alarm_entity": "alarm_control_panel.second"}

    await core.ensure_lockdown(hass_a, config_a)
    mgr_a = core._CORE.lockdown_mgr
    assert mgr_a.hass is hass_a and mgr_a.config is config_a

    # Lockdown was engaged before unload; its persisted state must survive.
    with open(core.LOCKDOWN_STATE_PATH, "w") as fh:
        json.dump({"active": True, "reason": "test", "since": 1.0}, fh)

    await _unload_core(core)
    assert core._CORE.lockdown_mgr is None and core._CORE.hass is None
    assert core._CORE.config == {}
    assert bus_a.listening == 0

    await core.ensure_lockdown(hass_b, config_b)
    mgr_b = core._CORE.lockdown_mgr
    assert mgr_b is not mgr_a
    assert mgr_b.hass is hass_b and core._CORE.hass is hass_b
    assert mgr_b.config is config_b and core._CORE.config is config_b
    assert mgr_b.active is True          # restored from disk, not reset
    assert bus_a.listening == 0 and bus_b.listening == 1

    # Cleanup never actuated anything on either instance.
    assert hass_a.service_calls == [] and hass_b.service_calls == []

    # Repeated stop/release stays safe and leaves nothing registered.
    await _unload_core(core)
    await _unload_core(core)
    assert bus_b.listening == 0 and bus_b.removed == 1
    assert core._CORE.lockdown_mgr is None


async def test_stop_alone_keeps_loaded_lockdown_manager(core, fake_hass):
    """stop() also runs for nova.observer_stop while Nova stays loaded; it
    must not drop the lockdown manager there (only unload releases it)."""
    fake_hass.bus = _CountingBus()
    await core.ensure_lockdown(fake_hass, {})
    mgr = core._CORE.lockdown_mgr
    await core.stop()
    assert core._CORE.lockdown_mgr is mgr and core._CORE.hass is fake_hass


# ── bootstrap: owned start listener and task ────────────────────────────────

class _Task:
    def __init__(self, coro):
        coro.close()  # never actually run the bootstrap
        self.cancelled = False

    def done(self):
        return self.cancelled

    def cancel(self):
        self.cancelled = True


class _BootHass:
    def __init__(self, running):
        self.is_running = running
        self.tasks = []
        self.listeners = []
        self.removed = 0
        hass = self

        class _Bus:
            def async_listen_once(self, event_type, cb):
                hass.listeners.append(cb)

                def _unsub():
                    hass.removed += 1
                    hass.listeners.remove(cb)
                return _unsub

        self.bus = _Bus()

    def async_create_background_task(self, coro, name):
        t = _Task(coro)
        self.tasks.append((name, t))
        return t

    def fire_started(self):
        for cb in list(self.listeners):
            self.listeners.remove(cb)   # listen_once removes itself before calling
            cb(None)


@pytest.fixture
def bootstrap(load):
    # A failed earlier import leaves a half-built module cached; start clean.
    mod = sys.modules.get("jc.bootstrap")
    if mod is not None and not hasattr(mod, "schedule_bootstrap"):
        del sys.modules["jc.bootstrap"]
    return load("bootstrap")


def test_bootstrap_running_hass_task_is_cancelled_on_shutdown(bootstrap):
    hass = _BootHass(running=True)
    handle = bootstrap.schedule_bootstrap(hass)
    assert [n for n, _ in hass.tasks] == ["nova_bootstrap"]
    task = hass.tasks[0][1]
    handle.shutdown()
    assert task.cancelled
    handle.shutdown()   # idempotent


def test_bootstrap_pending_start_listener_is_removed_on_shutdown(bootstrap):
    hass = _BootHass(running=False)
    handle = bootstrap.schedule_bootstrap(hass)
    assert len(hass.listeners) == 1 and hass.tasks == []
    handle.shutdown()
    assert hass.listeners == [] and hass.removed == 1
    hass.fire_started()
    assert hass.tasks == []


def test_bootstrap_start_after_shutdown_schedules_nothing(bootstrap):
    hass = _BootHass(running=False)
    handle = bootstrap.schedule_bootstrap(hass)
    cb = hass.listeners[0]
    handle.shutdown()
    cb(None)   # a start event already in flight when Nova unloaded
    assert hass.tasks == []


def test_bootstrap_started_then_shutdown_cancels_task_not_listener(bootstrap):
    hass = _BootHass(running=False)
    handle = bootstrap.schedule_bootstrap(hass)
    hass.fire_started()
    assert len(hass.tasks) == 1 and handle.unsub_start is None
    handle.shutdown()
    assert hass.tasks[0][1].cancelled
    assert hass.removed == 0   # listen_once already removed itself; no double remove


def test_bootstrap_handle_is_a_resources_closeable(bootstrap, load):
    resources = load("resources")
    hass = _BootHass(running=True)
    reg = resources.NovaResources()
    reg.add_closeable(bootstrap.schedule_bootstrap(hass))
    summary = reg.close_all()
    assert summary["closeables"] == 1 and summary["errors"] == 0
    assert hass.tasks[0][1].cancelled


# ── services.yaml parity ────────────────────────────────────────────────────

def _registered_services() -> set:
    """Service names passed to hass.services.async_register(DOMAIN, ...)."""
    names = set()
    consts = {}
    for path in (COMP / "__init__.py", COMP / "proactive_audio.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                consts[node.targets[0].id] = node.value.value
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "async_register" and len(node.args) >= 2
                    and ast.unparse(node.func.value).endswith("services")):
                arg = node.args[1]
                if isinstance(arg, ast.Constant):
                    names.add(arg.value)
                elif isinstance(arg, ast.Name):
                    names.add(consts[arg.id])
    return names


def _unloaded_services() -> set:
    """Service names in async_unload_entry's removal tuple."""
    tree = ast.parse((COMP / "__init__.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_unload_entry":
            for sub in ast.walk(node):
                if isinstance(sub, ast.For) and isinstance(sub.iter, ast.Tuple):
                    return {e.value for e in sub.iter.elts if isinstance(e, ast.Constant)}
    return set()


def test_services_yaml_documents_test_routing():
    doc = yaml.safe_load((COMP / "services.yaml").read_text())
    assert "test_routing" in doc
    assert "log" in doc["test_routing"]["description"].lower()


def test_every_registered_service_is_in_services_yaml():
    doc = yaml.safe_load((COMP / "services.yaml").read_text())
    registered = _registered_services()
    assert "test_routing" in registered and "speak" in registered
    assert registered == set(doc)


def test_every_init_service_is_removed_on_unload():
    # speak / process_intent are removed by proactive_audio's own unload.
    registered = _registered_services() - {"speak", "process_intent"}
    assert registered <= _unloaded_services()
