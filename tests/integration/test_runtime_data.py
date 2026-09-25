"""Phase 3A typed runtime against a real Home Assistant (PHACC).

Drives the real async_setup_entry / async_unload_entry to prove:

* setup stores one NovaRuntime on entry.runtime_data, bound to this hass,
* the hass.data bridge holds the very same objects (identity, not copies),
* each owner (provider client, sentinel, scheduler, resources) is built once,
* unload tears the runtime down and leaves no runtime_data or bridge behind,
  and repeating unload/release is safe,
* setup -> unload -> setup builds a fresh runtime from the changed config,
* a partial setup failure clears runtime_data and the bridge, even when the
  platform unload inside the failure path reports failure,
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

BRIDGED = ("client", "sentinel", "reminder_watcher", "scheduler", "resources",
           "automation_contexts", "automation_inventory", "runtime_config",
           "llm_provider_name", "schema_version")
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


def _assert_released(hass, entry, runtime=None):
    from custom_components.nova.runtime import NovaRuntimeUnavailable, get_runtime
    assert not hasattr(entry, "runtime_data")
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
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


async def test_bridge_values_are_identical_objects(hass):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    bridge = hass.data[DOMAIN][entry.entry_id]

    for key in BRIDGED:
        assert bridge[key] is getattr(runtime, key), key
    # Bridge-only keys are unchanged.
    assert bridge["observer_running"] is False
    assert isinstance(bridge["camera_unsubs"], list)
    assert isinstance(bridge["recognition_unsubs"], list)
    assert bridge["proactive_audio_unsubs"]

    # A panel write the way websocket.py does it reaches the runtime.
    bridge.setdefault("runtime_config", {})["announcement_speakers"] = ["media_player.x"]
    assert runtime.runtime_config["announcement_speakers"] == ["media_player.x"]
    from custom_components.nova import nova_config
    assert nova_config.runtime_get(
        hass, entry, "announcement_speakers", None) == ["media_player.x"]


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


async def test_unload_releases_runtime_and_bridge(hass):
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
    clear_runtime(hass, entry)
    clear_runtime(hass, entry)
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
    assert hass.data[DOMAIN][entry.entry_id]["runtime_config"] is second.runtime_config
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
    assert hass.data[DOMAIN][entry.entry_id]["scheduler"] is second.scheduler
    assert first.scheduler.task_names() == []
    assert first.sentinel._active is False


async def test_partial_setup_failure_clears_runtime_and_bridge(hass):
    """sentinel.async_start() fails after the runtime and bridge exist."""
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
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_partial_setup_failure_clears_runtime_even_if_platform_unload_fails(hass):
    """Before Phase 3A a failed platform unload inside the failure path left
    the bridge behind. clear_runtime() now clears it regardless."""
    entry = await _add_entry(hass)
    with patch.object(hass.config_entries, "async_forward_entry_setups",
                      side_effect=RuntimeError("boom")), \
         patch.object(hass.config_entries, "async_unload_platforms",
                      return_value=False):
        result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert result is False
    assert not hasattr(entry, "runtime_data")
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_failed_observer_start_clears_runtime_and_bridge(hass):
    from custom_components.nova import observer

    async def _boom(hass_, config, entry=None):
        raise RuntimeError("observer failed to start")

    entry = await _add_entry(hass, observer_enabled=True)
    with patch.object(observer, "start", _boom):
        result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert result is False
    assert not hasattr(entry, "runtime_data")
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_proactive_audio_objects_are_entry_owned(hass):
    """Phase 3B: the four proactive-audio objects are the entry's NovaRuntime
    fields, never domain-level hass.data keys or bridge values. Unload drops
    them; a reload builds new ones."""
    from custom_components.nova import proactive_audio

    entry = await _setup(hass)
    runtime = entry.runtime_data
    router = proactive_audio._intent_router(hass, runtime)   # built lazily, on first use
    ledger, locks, buffer = runtime.state_ledger, runtime.entity_locks, runtime.alert_buffer
    assert runtime.intent_router is router
    assert router is not None and ledger is not None and locks is not None
    assert buffer.ready is True

    store = hass.data[DOMAIN]
    for key in LEGACY_AUDIO_KEYS:
        assert key not in store, key
    bridge = store[entry.entry_id]
    for obj in (router, ledger, locks, buffer):
        assert all(v is not obj for v in bridge.values())

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    for field in PROACTIVE_FIELDS:
        assert getattr(runtime, field) is None, field
    for key in LEGACY_AUDIO_KEYS:
        assert key not in hass.data.get(DOMAIN, {}), key

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fresh = entry.runtime_data
    assert fresh is not runtime
    assert fresh.state_ledger is not ledger
    assert fresh.alert_buffer is not buffer
    assert fresh.alert_buffer.ready is True
