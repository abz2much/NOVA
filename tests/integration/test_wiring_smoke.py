"""Integration smoke tests against a real Home Assistant instance (PHACC).

These are the wiring tests the hand-rolled fakes cannot prove: that the
integration actually sets up, registers its conversation agent, and that the
config flow validates input. They are intentionally few — the bulk of coverage
lives in tests/unit/ where it is fast and deterministic.

Skipped unless pytest-homeassistant-custom-component is installed (see the
directory conftest) — and PHACC must NOT be installed into the same venv as
the unit suite: it pulls in the real `homeassistant` package, which collides
with tests/unit/'s hand-rolled fakes and sys.modules stubs (confirmed live —
installing PHACC into the shared dev venv took 1634 passing unit tests to
1465 errors). Use a separate venv, e.g.:

    python3 -m venv .venv-integration
    .venv-integration/bin/pip install pytest-homeassistant-custom-component
    .venv-integration/bin/python -m pytest tests/integration/ -v
"""
import json
import os

import pytest
from unittest.mock import patch

from homeassistant.setup import async_setup_component  # noqa: E402

from .conftest import MockConfigEntry  # noqa: E402

DOMAIN = "nova"


def _has_runtime(entry) -> bool:
    from custom_components.nova.runtime import NovaRuntime
    return isinstance(getattr(entry, "runtime_data", None), NovaRuntime)


def _make_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"llm_provider": "ollama", "llm_base_url": "http://localhost:11434/v1",
              "model": "llama3", "schema_version": 7},
        options={},
    )


async def test_setup_entry_registers_integration(hass):
    """Setting up a config entry should leave the integration loaded, its
    runtime on entry.runtime_data and nothing of Nova's in hass.data."""
    # Nova registers a conversation agent, which needs the base
    # `homeassistant` component's exposed-entities tracking already set up.
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"llm_provider": "ollama", "llm_base_url": "http://localhost:11434/v1",
              "model": "llama3", "schema_version": 7},
        options={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _has_runtime(entry)
    assert DOMAIN not in hass.data


async def test_conversation_agent_is_registered(hass):
    """Nova should register as a conversation agent so it can be selected as
    the assist pipeline's conversation engine."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"llm_provider": "ollama", "llm_base_url": "http://localhost:11434/v1",
              "model": "llama3", "schema_version": 7},
        options={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Modern HA keys conversation agents by entity_id (e.g. "conversation.nova"),
    # not by integration domain — async_get_agent_info(hass, "nova") always
    # returns None for a ConversationEntity-based agent, since a bare id with
    # no "." is only resolved against the legacy non-entity agent registry.
    from homeassistant.components import conversation as conversation_component
    from homeassistant.helpers import entity_registry as er

    entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, entry.entry_id)
    assert entity_id is not None
    agent = conversation_component.async_get_agent_info(hass, entity_id)
    assert agent is not None


async def test_config_flow_accepts_local_llm(hass):
    """A local (Ollama) endpoint with no cloud key must be a valid config — the
    v6.7.0 'local-first install' contract. test_connection is mocked so this
    exercises the flow's own acceptance logic, not a real network call to a
    local Ollama server that won't exist in CI."""
    # Earlier setup tests legitimately persist a local-only runtime config in
    # PHACC's shared test config directory. Once _find_config learned to
    # recognise local endpoints (not only cloud keys), that state correctly
    # triggers the reinstall auto-import path and completes this flow during
    # async_init. This test is specifically for the *manual* form path, so
    # isolate that path instead of depending on suite order or deleting a
    # valid config file another test created.
    with patch("custom_components.nova.config_flow._find_config", return_value=None), \
            patch("custom_components.nova.llm_provider.test_connection", return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"llm_base_url": "http://localhost:11434/v1"},
        )
    assert result["type"] == "create_entry"
    assert result["data"]["llm_provider"] == "ollama"
    # Storage preserves the user's compatible legacy path. OllamaProvider
    # removes a trailing /v1 only when it builds the native /api/chat URL.
    assert result["data"]["ollama_base_url"] == "http://localhost:11434/v1"


async def test_config_flow_auto_imports_from_this_instances_config_dir(hass, tmp_path):
    """The auto-import step must read <this hass's config dir>/nova/config.json,
    not a hardcoded /config — PHACC's own hass fixture already proves this,
    since its config dir lives under the installed package
    (pytest_homeassistant_custom_component/testing_config), nowhere near
    /config. Confirms config_flow.py resolves the path itself, per-flow, via
    self.hass.config.path() rather than a module-level constant."""
    assert hass.config.config_dir != "/config"
    config_dir = os.path.join(hass.config.config_dir, "nova")
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "config.json")
    with open(config_path, "w") as f:
        json.dump({"api_key": "gsk_seeded_from_runtime_config",
                   "llm_provider": "groq", "model": "llama3"}, f)
    try:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"})
        assert result["type"] == "create_entry"
        assert result["data"]["api_key"] == "gsk_seeded_from_runtime_config"
    finally:
        os.remove(config_path)


async def test_unload_then_resetup_entry(hass):
    """Reload (unload → setup again on the same entry) is what HA does on
    every options-change and every HACS update — NovaResources' one-call,
    fail-safe teardown (__init__.py) exists specifically so this leaves Nova
    running cleanly again rather than accumulating duplicate listeners/timers
    across the two setups."""
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _has_runtime(entry)
    assert DOMAIN not in hass.data

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not _has_runtime(entry)
    assert DOMAIN not in hass.data

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _has_runtime(entry)
    assert DOMAIN not in hass.data


async def test_setup_failure_before_resource_acquisition_leaves_no_state(hass):
    """async_setup_entry's LLM-provider init is the first fallible step, and
    it deliberately runs BEFORE any listener/scheduler/resource is acquired
    (see __init__.py) so a provider failure can return False cleanly with
    nothing to unwind. This locks that contract in, and also proves a failed
    attempt doesn't wedge the entry — a corrected retry must still succeed."""
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)

    with patch("custom_components.nova.create_provider",
               side_effect=RuntimeError("boom")):
        result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert result is False
    assert not _has_runtime(entry)
    assert DOMAIN not in hass.data

    # A failed setup leaves the entry in SETUP_ERROR, not NOT_LOADED — HA
    # itself refuses a bare async_setup from that state, same as it would
    # after a real HACS update following a broken install. Reload is the
    # real retry path (used e.g. when the user fixes the config and hits
    # Reload), and must still succeed once the provider works again.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _has_runtime(entry)
    assert DOMAIN not in hass.data
