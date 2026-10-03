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
from unittest.mock import AsyncMock, patch

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


_GROQ_LIST = (["openai/gpt-oss-120b", "qwen/qwen3.6-27b"], [])
_OLLAMA_LIST = (["llama3.2", "llava"], [
    {"id": "llama3.2", "capabilities": ["completion", "tools"]},
    {"id": "llava", "capabilities": ["completion", "vision"]}])


def _flow_patches(lists, probes=None, saved=None):
    async def discover(self, provider, value):
        return lists.get(provider)

    async def probe_all(hass, jobs, creds):
        return {r: (probes or {}).get(r) for r in jobs}
    writer = AsyncMock(return_value=True)
    return writer, (
        # Nova's own setup builds AI clients before it copies settings into
        # config.json, and the key write is mocked here, so keep it out.
        patch("custom_components.nova.async_setup_entry", return_value=True),
        patch("custom_components.nova.config_flow._find_config", return_value=None),
        patch("custom_components.nova.config_flow._saved_keys", return_value=saved or {}),
        patch("custom_components.nova.config_flow.NovaConfigFlow._discover", discover),
        patch("custom_components.nova.setup_probe.probe_all", probe_all),
        patch("custom_components.nova.ha_secrets.async_set_provider_credential", writer),
    )


async def _run(hass, patches, keys, roles, models):
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert r["step_id"] == "user"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], keys)
        if r.get("step_id") != "roles":
            return r
        r = await hass.config_entries.flow.async_configure(r["flow_id"], roles)
        assert r["step_id"] == "models"
        placeholders = r["description_placeholders"]
        r = await hass.config_entries.flow.async_configure(r["flow_id"], models)
        await hass.async_block_till_done()
        r["_placeholders"] = placeholders
        return r


_TEXT = "openai/gpt-oss-120b"


async def test_first_run_one_groq_key(hass):
    writer, patches = _flow_patches({"groq": _GROQ_LIST})
    r = await _run(hass, patches, {"groq_api_key": "gsk"},
                   {"conversation": "groq", "classifier": "groq", "reasoning": "groq",
                    "camera_reasoning": "groq", "vision": "groq"},
                   {"conversation_model": _TEXT, "classifier_model": _TEXT,
                    "reasoning_model": _TEXT, "camera_reasoning_model": _TEXT,
                    "vision_model": "qwen/qwen3.6-27b"})
    assert r["type"] == "create_entry"
    assert r["_placeholders"] == {f"{x}_provider": "Groq" for x in (
        "conversation", "classifier", "reasoning", "camera_reasoning", "vision")}
    writer.assert_awaited_once()
    assert r["data"]["vision_model"] == "qwen/qwen3.6-27b"
    assert "api_key" not in r["data"]
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == DOMAIN
    assert entry.data["classifier_provider"] == "groq"
    assert entry.data["llm_provider"] == "groq"


async def test_first_run_ollama_only_vision_later(hass):
    writer, patches = _flow_patches({"ollama": _OLLAMA_LIST})
    r = await _run(hass, patches, {"ollama_base_url": "192.168.1.50"},
                   {r: "ollama" for r in ("conversation", "classifier", "reasoning",
                                           "camera_reasoning")} | {"vision": "not_now"},
                   {"conversation_model": "llama3.2", "classifier_model": "llama3.2",
                    "reasoning_model": "llama3.2", "camera_reasoning_model": "llama3.2"})
    assert r["type"] == "create_entry"
    assert r["data"]["ollama_base_url"] == "http://192.168.1.50:11434"
    assert "vision_provider" not in r["data"]
    writer.assert_not_awaited()


async def test_first_run_several_keys(hass):
    writer, patches = _flow_patches({"groq": _GROQ_LIST, "anthropic": (["claude-sonnet-5"], [])})
    r = await _run(hass, patches, {"groq_api_key": "g", "anthropic_api_key": "a"},
                   {"conversation": "anthropic", "classifier": "groq", "reasoning": "groq",
                    "camera_reasoning": "groq", "vision": "anthropic"},
                   {"conversation_model": "claude-sonnet-5", "classifier_model": _TEXT,
                    "reasoning_model": _TEXT, "camera_reasoning_model": _TEXT,
                    "vision_model": "claude-sonnet-5"})
    assert r["type"] == "create_entry"
    assert writer.await_count == 2


async def test_first_run_bad_key_stays_on_screen_one(hass):
    _writer, patches = _flow_patches({"openai": None})
    # Screen 1's fallback key test is setup_probe.probe_model (Task 5).
    with patch("custom_components.nova.setup_probe.probe_model",
               return_value="authentication_failed"):
        r = await _run(hass, patches, {"openai_api_key": "wrong"}, {}, {})
    assert r["step_id"] == "user"
    assert r["errors"] == {"openai_api_key": "invalid_auth"}


async def test_new_choices_beat_a_leftover_config_json_after_real_setup(hass, tmp_path):
    """An earlier cloud only install left config.json behind (its keys were
    moved to secrets.yaml, so it does not trigger the import). After the
    screens and Nova's real setup, nothing mocked, the new choices win.

    PHACC's config folder is shared between runs and may already hold keys
    and a config.json from other tests, so this test uses its own empty
    secrets file and puts any existing config.json back afterwards."""
    import json as _json
    import os as _os
    from custom_components.nova import ha_secrets as _hs
    from custom_components.nova import nova_config, setup_health

    assert await async_setup_component(hass, "homeassistant", {})
    _os.makedirs(hass.config.path("nova"), exist_ok=True)
    path = hass.config.path("nova", "config.json")
    before = open(path, "rb").read() if _os.path.exists(path) else None
    with open(path, "w") as f:
        _json.dump({"llm_provider": "anthropic", "model": "old-model",
                    "classifier_provider": "anthropic", "welcome_shown": True}, f)

    async def discover(self, provider, value):
        return {"ollama": _OLLAMA_LIST}.get(provider)

    async def probe_all(hass_, jobs, creds):
        return {r: None for r in jobs}
    _hs._reset_secrets_cache()
    try:
        with patch.object(_hs, "SECRETS_PATH", tmp_path / "secrets.yaml"), \
                patch("custom_components.nova.config_flow._saved_keys", return_value={}), \
                patch("custom_components.nova.config_flow.NovaConfigFlow._discover", discover), \
                patch("custom_components.nova.setup_probe.probe_all", probe_all):
            r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
            assert r["step_id"] == "user"          # the leftover file did not trigger an import
            r = await hass.config_entries.flow.async_configure(
                r["flow_id"], {"ollama_base_url": "http://localhost:11434"})
            r = await hass.config_entries.flow.async_configure(r["flow_id"], {
                "conversation": "ollama", "classifier": "ollama", "reasoning": "ollama",
                "camera_reasoning": "ollama", "vision": "not_now"})
            r = await hass.config_entries.flow.async_configure(r["flow_id"], {
                "conversation_model": "llama3.2", "classifier_model": "llama3.2",
                "reasoning_model": "llama3.2", "camera_reasoning_model": "llama3.2"})
            assert r["type"] == "create_entry"
            await hass.async_block_till_done()

            assert nova_config.get("llm_provider") == "ollama"
            assert nova_config.get("model") == "llama3.2"
            assert nova_config.get("classifier_provider") == "ollama"
            assert nova_config.get("vision_provider") is None
            entry = hass.config_entries.async_entries(DOMAIN)[0]
            ai = await hass.async_add_executor_job(setup_health._check_ai_roles, hass, entry)
            # Every chosen role works; only vision, set up later, is flagged.
            assert ai["status"] == "warn"
            assert "Vision (groq)" in ai["detail"] and "Conversation" not in ai["detail"]
    finally:
        _hs._reset_secrets_cache()
        if before is None:
            _os.remove(path)
        else:
            with open(path, "wb") as f:
                f.write(before)


async def test_first_run_already_set_up(hass):
    # A real Nova entry carries the unique id the flow checks.
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN,
                    data={"llm_provider": "groq", "schema_version": 7}).add_to_hass(hass)
    with patch("custom_components.nova.config_flow._find_config", return_value=None):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] == "abort" and r["reason"] == "already_configured"


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
        # An imported (existing) install never gets the first run welcome.
        assert "welcome_pending" not in result["data"]
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


async def test_nova_stores_live_under_this_instances_config_dir(hass):
    """PHACC's config dir is not /config: Nova must resolve every store
    against hass.config.path(), never the literal /config."""
    from custom_components.nova import goals, nova_config, paths, spoken_history

    assert hass.config.config_dir != "/config"
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    root = hass.config.path()
    assert paths.config_dir() == root
    assert str(nova_config._config_path()) == hass.config.path("nova", "config.json")
    assert goals._db_path() == hass.config.path("nova", "patterns.db")
    assert spoken_history._resolve(None) == hass.config.path("nova", "conversations.db")
    assert os.path.isfile(hass.config.path("nova", "config.json"))
