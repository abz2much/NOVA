"""Phase 3A typed runtime against a real Home Assistant (PHACC).

Drives the real async_setup_entry / async_unload_entry to prove:

* setup stores one NovaRuntime on entry.runtime_data, bound to this hass,
  and nothing of Nova's in hass.data (Phase 3C removed the bridge),
* each owner (provider client, sentinel, scheduler, resources) is built once,
* unload tears the runtime down and leaves no runtime_data behind, and
  repeating unload/release is safe,
* setup -> unload -> setup builds a fresh runtime from the changed config,
* a partial setup failure clears runtime_data, even when the platform unload
  inside the failure path reports failure,
* the proactive-audio objects belong to the entry's runtime (Phase 3B) and
  are dropped with it; tests/integration/test_proactive_audio_runtime.py
  covers them in detail.

No device is touched: there are no lock, alarm or cover entities here.
"""
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import DOMAIN, _make_entry

RUNTIME_OBJECTS = ("client", "sentinel", "reminder_watcher", "scheduler", "resources",
                   "automation_contexts", "automation_inventory", "runtime_config",
                   "intent_router", "state_ledger", "entity_locks", "alert_buffer")
PROACTIVE_FIELDS = ("intent_router", "state_ledger", "entity_locks", "alert_buffer")
LEGACY_AUDIO_KEYS = ("_intent_router", "_state_ledger", "_entity_locks", "_alert_buffer")


@pytest.fixture(autouse=True)
def _restore_nova_config(hass):
    """Setup and these tests write <PHACC testing config>/nova/config.json;
    put it back afterwards and drop nova_config's cached copy."""
    import os
    from custom_components.nova import nova_config
    path = hass.config.path("nova", "config.json")
    before = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
    yield
    if before is None:
        if os.path.exists(path):
            os.remove(path)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(before)
    nova_config._cache = {}
    nova_config._loaded = False


@pytest.fixture(autouse=True)
def _no_real_config(tmp_path, monkeypatch):
    """Nova's setup path still has a few fixed /config paths: the secrets
    file, the intrusion log and snapshot dirs, the persistent log and the
    state ledger. Point them all at this test's tmp dir so nothing here reads
    or writes the real /config. monkeypatch restores every value (and cache)
    afterwards."""
    from custom_components.nova import ha_secrets, intrusion, proactive_audio, websocket
    monkeypatch.setattr(ha_secrets, "SECRETS_PATH", tmp_path / "secrets.yaml")
    monkeypatch.setattr(ha_secrets, "_SECRETS_CACHE", None)
    monkeypatch.setattr(intrusion, "LOG_PATH", tmp_path / "intrusion_log.json")
    monkeypatch.setattr(intrusion, "SNAPSHOT_DIR", str(tmp_path / "intrusion"))
    monkeypatch.setattr(intrusion, "_LEGACY_SNAPSHOT_DIR", str(tmp_path / "www_intrusion"))
    monkeypatch.setattr(intrusion, "_log", [])
    monkeypatch.setattr(intrusion, "_log_loaded", False)
    monkeypatch.setattr(websocket, "_LOG_FILE", tmp_path / "nova.log")
    real_ledger = proactive_audio.StateLedger
    ledger_path = str(tmp_path / "state_ledger.jsonl")
    monkeypatch.setattr(proactive_audio, "StateLedger", lambda: real_ledger(ledger_path))
    yield
    # Log lines queued during the test must land in tmp, not after restore.
    websocket._LOG_QUEUE.join()


async def _add_entry(hass, **options):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    if options:
        entry = type(entry)(domain=entry.domain, data=dict(entry.data), options=options)
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, **options):
    entry = await _add_entry(hass, **options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def assert_no_nova_state_in_hass_data(hass, runtime=None):
    """Nova keeps nothing in hass.data: no domain bucket, and none of the
    runtime's objects anywhere in its top two levels."""
    from custom_components.nova.runtime import NovaRuntime
    assert DOMAIN not in hass.data
    owned = [] if runtime is None else [
        getattr(runtime, name) for name in RUNTIME_OBJECTS
        if getattr(runtime, name) is not None]
    for value in hass.data.values():
        nested = list(value.values()) if isinstance(value, dict) else []
        for item in [value, *nested]:
            assert not isinstance(item, NovaRuntime)
            assert all(item is not obj for obj in owned)


def _assert_released(hass, entry, runtime=None):
    from custom_components.nova.runtime import NovaRuntimeUnavailable, get_runtime
    assert not hasattr(entry, "runtime_data")
    assert_no_nova_state_in_hass_data(hass, runtime)
    with pytest.raises(NovaRuntimeUnavailable):
        get_runtime(entry)
    if runtime is not None:
        # The objects the runtime held are torn down, not just forgotten.
        assert runtime.scheduler.task_names() == []
        assert runtime.resources._unsubs == []
        assert runtime.resources._closeables == []
        assert runtime.sentinel._active is False


async def test_setup_creates_runtime_with_live_objects(hass):
    from custom_components.nova.automation_inventory import (
        AutomationContextTracker, AutomationInventory)
    from custom_components.nova.llm_provider import LLMProvider
    from custom_components.nova.migrations import CURRENT_SCHEMA_VERSION
    from custom_components.nova.reminders import ReminderWatcher
    from custom_components.nova.resources import NovaResources
    from custom_components.nova.runtime import NovaRuntime, get_runtime
    from custom_components.nova.scheduler import NovaScheduler
    from custom_components.nova.sentinel import NovaSentinel

    entry = await _setup(hass)
    runtime = entry.runtime_data

    assert isinstance(runtime, NovaRuntime)
    assert get_runtime(entry) is runtime
    assert isinstance(runtime.client, LLMProvider)
    assert runtime.llm_provider_name == "ollama"
    assert runtime.client.model == "llama3"
    assert isinstance(runtime.sentinel, NovaSentinel)
    assert isinstance(runtime.reminder_watcher, ReminderWatcher)
    assert isinstance(runtime.scheduler, NovaScheduler)
    assert isinstance(runtime.resources, NovaResources)
    assert isinstance(runtime.automation_contexts, AutomationContextTracker)
    assert isinstance(runtime.automation_inventory, AutomationInventory)
    assert runtime.automation_contexts.inventory is runtime.automation_inventory
    assert isinstance(runtime.runtime_config, dict)
    assert runtime.schema_version == CURRENT_SCHEMA_VERSION

    # Bound to this hass and live.
    assert runtime.sentinel.hass is hass
    assert runtime.reminder_watcher.hass is hass
    assert runtime.scheduler._hass is hass
    assert runtime.sentinel._active is True
    assert "health" in runtime.scheduler.task_names()
    # The scheduler and inventory are disposed through resources.
    assert runtime.scheduler in runtime.resources._closeables
    assert runtime.automation_inventory in runtime.resources._closeables


async def test_setup_creates_no_hass_data_bridge(hass):
    """Setup stores runtime state only on entry.runtime_data."""
    entry = await _setup(hass)
    runtime = entry.runtime_data
    assert_no_nova_state_in_hass_data(hass, runtime)

    # A panel write lands in the runtime's own dict and nowhere else.
    runtime.runtime_config["announcement_speakers"] = ["media_player.x"]
    from custom_components.nova import nova_config
    assert nova_config.runtime_get(
        hass, entry, "announcement_speakers", None) == ["media_player.x"]
    assert_no_nova_state_in_hass_data(hass, runtime)


async def test_runtime_mutations_never_write_hass_data(hass):
    """Observer state, the automation inventory and the proactive-audio
    objects all change on the runtime alone."""
    from custom_components.nova import proactive_audio
    from custom_components.nova.runtime import set_observer_running

    entry = await _setup(hass)
    runtime = entry.runtime_data
    set_observer_running(entry, True)
    assert runtime.observer_running is True
    set_observer_running(entry, False)
    proactive_audio._intent_router(hass, runtime)
    assert runtime.automation_inventory is not None
    assert runtime.alert_buffer is not None and runtime.intent_router is not None
    assert_no_nova_state_in_hass_data(hass, runtime)


async def test_each_owner_is_built_once_per_setup(hass):
    import custom_components.nova as nova
    from custom_components.nova import llm_provider
    from custom_components.nova.resources import NovaResources
    from custom_components.nova.scheduler import NovaScheduler
    from custom_components.nova.sentinel import NovaSentinel

    built: dict[str, list] = {"scheduler": [], "resources": [], "sentinel": [], "client": []}

    def _counting(cls, key):
        real = cls.__init__

        def _init(self, *a, **kw):
            real(self, *a, **kw)
            built[key].append(self)
        return _init

    real_create = llm_provider.create_provider

    def _create(*a, **kw):
        client = real_create(*a, **kw)
        built["client"].append(client)
        return client

    entry = await _add_entry(hass)
    with patch.object(NovaScheduler, "__init__", _counting(NovaScheduler, "scheduler")), \
         patch.object(NovaResources, "__init__", _counting(NovaResources, "resources")), \
         patch.object(NovaSentinel, "__init__", _counting(NovaSentinel, "sentinel")), \
         patch.object(nova, "create_provider", _create):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    runtime = entry.runtime_data
    assert built["scheduler"] == [runtime.scheduler]
    assert built["resources"] == [runtime.resources]
    assert built["sentinel"] == [runtime.sentinel]
    assert built["client"] == [runtime.client]


async def test_unload_releases_runtime(hass):
    entry = await _setup(hass)
    runtime = entry.runtime_data

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    _assert_released(hass, entry, runtime)


async def test_repeated_unload_and_release_are_safe(hass):
    from custom_components.nova import async_unload_entry
    from custom_components.nova.runtime import clear_runtime

    entry = await _setup(hass)
    runtime = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    await async_unload_entry(hass, entry)
    clear_runtime(entry)
    clear_runtime(entry)
    _assert_released(hass, entry, runtime)


async def test_setup_unload_setup_builds_fresh_runtime_from_changed_config(hass):
    from custom_components.nova import nova_config

    entry = await _setup(hass)
    first = entry.runtime_data
    assert first.client.model == "llama3"
    assert "briefing_morning_time" not in first.runtime_config

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    _assert_released(hass, entry, first)

    # Changed the way the panel changes it: config.json is Nova's source of
    # truth and wins over the entry (nova_config.py).
    await hass.async_add_executor_job(nova_config.set, "model", "llama3.1")
    await hass.async_add_executor_job(
        nova_config.set, "briefing_morning_time", "06:45")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    second = entry.runtime_data
    assert second is not first
    for key in ("client", "sentinel", "reminder_watcher", "scheduler", "resources",
                "automation_contexts", "automation_inventory", "runtime_config"):
        assert getattr(second, key) is not getattr(first, key), key
    assert second.client.model == "llama3.1"
    assert second.runtime_config["briefing_morning_time"] == "06:45"
    assert second.sentinel.hass is hass and second.scheduler._hass is hass
    assert_no_nova_state_in_hass_data(hass, second)
    # The released runtime stays torn down.
    assert first.scheduler.task_names() == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    _assert_released(hass, entry, second)


async def test_reload_replaces_runtime(hass):
    entry = await _setup(hass)
    first = entry.runtime_data
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    second = entry.runtime_data
    assert second is not first
    for key in ("client", "sentinel", "reminder_watcher", "scheduler", "resources",
                "automation_contexts", "automation_inventory", "runtime_config"):
        assert getattr(second, key) is not getattr(first, key), key
    assert_no_nova_state_in_hass_data(hass, second)
    assert first.scheduler.task_names() == []
    assert first.sentinel._active is False


async def test_partial_setup_failure_clears_runtime(hass):
    """sentinel.async_start() fails after the runtime exists."""
    entry = await _add_entry(hass)
    seen = []
    from custom_components.nova.sentinel import NovaSentinel

    async def _boom(self):
        seen.append(entry.runtime_data)
        raise RuntimeError("boom")

    with patch.object(NovaSentinel, "async_start", _boom):
        result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert result is False
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(seen) == 1   # the runtime existed when the failure happened
    runtime = seen[0]
    assert runtime.scheduler.task_names() == []
    assert runtime.resources._unsubs == [] and runtime.resources._closeables == []
    assert not hasattr(entry, "runtime_data")
    assert DOMAIN not in hass.data


async def test_partial_setup_failure_clears_runtime_even_if_platform_unload_fails(hass):
    """A failed platform unload inside the failure path must not leave
    runtime_data behind: clear_runtime() clears it regardless."""
    entry = await _add_entry(hass)
    with patch.object(hass.config_entries, "async_forward_entry_setups",
                      side_effect=RuntimeError("boom")), \
         patch.object(hass.config_entries, "async_unload_platforms",
                      return_value=False):
        result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert result is False
    assert not hasattr(entry, "runtime_data")
    assert DOMAIN not in hass.data


async def test_failed_observer_start_clears_runtime(hass):
    from custom_components.nova import observer

    async def _boom(hass_, config, entry=None):
        raise RuntimeError("observer failed to start")

    entry = await _add_entry(hass, observer_enabled=True)
    with patch.object(observer, "start", _boom):
        result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert result is False
    assert not hasattr(entry, "runtime_data")
    assert DOMAIN not in hass.data


async def test_proactive_audio_objects_are_entry_owned(hass):
    """Phase 3B: the four proactive-audio objects are the entry's NovaRuntime
    fields, never hass.data keys or values. Unload drops
    them; a reload builds new ones."""
    from custom_components.nova import proactive_audio

    entry = await _setup(hass)
    runtime = entry.runtime_data
    router = proactive_audio._intent_router(hass, runtime)   # built lazily, on first use
    ledger, locks, buffer = runtime.state_ledger, runtime.entity_locks, runtime.alert_buffer
    assert runtime.intent_router is router
    assert router is not None and ledger is not None and locks is not None
    assert buffer.ready is True

    assert_no_nova_state_in_hass_data(hass, runtime)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    for field in PROACTIVE_FIELDS:
        assert getattr(runtime, field) is None, field
    assert DOMAIN not in hass.data

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fresh = entry.runtime_data
    assert fresh is not runtime
    assert fresh.state_ledger is not ledger
    assert fresh.alert_buffer is not buffer
    assert fresh.alert_buffer.ready is True


async def test_provider_clients_close_exactly_once_on_unload_and_reload(hass):
    """Phase 6: the runtime's ProviderManager owns the primary client;
    unload and reload close it exactly once, and a reload builds a new one."""
    from custom_components import nova
    from custom_components.nova import llm_provider

    real_create = llm_provider.create_provider
    closes: dict[int, int] = {}
    built = []

    def _create(*a, **kw):
        client = real_create(*a, **kw)
        real_close = client.close

        def _counted_close():
            closes[id(client)] = closes.get(id(client), 0) + 1
            return real_close()

        client.close = _counted_close
        built.append(client)
        return client

    with patch.object(nova, "create_provider", _create):
        entry = await _setup(hass)
        first = entry.runtime_data.client
        manager = entry.runtime_data.providers
        assert manager.primary is first

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        second = entry.runtime_data.client
        assert second is not first
        assert closes.get(id(first)) == 1
        assert manager.closed

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert closes.get(id(first)) == 1
    assert closes.get(id(second)) == 1
    assert len(built) == 2
