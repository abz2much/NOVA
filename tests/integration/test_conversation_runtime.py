"""Phase 3B.3 conversation.py on NovaRuntime, against a real Home Assistant
(PHACC).

Drives the real setup and real conversation turns to prove the agent:

* holds the runtime's own client, never builds a second provider, and
  cannot be constructed for a loaded entry without a runtime,
* reads runtime_config from NovaRuntime, not the hass.data bridge, so a
  missing, damaged or drifted bridge changes nothing,
* sees in-place runtime_config changes on the next turn (no stale copy),
* gives the reasoning fallback the persisted config overlaid with the
  current runtime values, keeping the None/"" overlay rule,
* routes the reply with the same live satellite_pairings in both routing
  sites,
* fails a turn for a loaded entry that lost its runtime before the local
  engine, the provider, any tool or TTS routing runs.

Everything outward is faked: the local engine, the agent loop, reply
routing, TTS and memory only record what they were given. No device is
touched: there are no lock, alarm or cover entities here.
"""
import itertools
import logging
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .test_runtime_data import (  # noqa: F401  (autouse fixtures)
    DOMAIN,
    _add_entry,
    _no_real_config,
    _restore_nova_config,
)

_counter = itertools.count()


def _text(stem: str = "Nova, write a short poem about the sea") -> str:
    """A unique addressed request, so the multi-wake dedup never folds two
    turns into one."""
    return f"{stem} number {next(_counter)}"


@pytest.fixture(autouse=True)
def _no_real_config_state(tmp_path, monkeypatch):
    """Setup's remaining fixed /config state files, pointed at tmp."""
    from custom_components.nova import cognitive_core, modes, reasoning_cache, routines
    monkeypatch.setattr(cognitive_core, "LOCKDOWN_STATE_PATH",
                        str(tmp_path / "lockdown_state.json"))
    monkeypatch.setattr(modes, "MODE_STATE_PATH", str(tmp_path / "mode_state.json"))
    monkeypatch.setattr(reasoning_cache, "CACHE_PATH", tmp_path / "reasoning_cache.json")
    monkeypatch.setattr(routines, "ROUTINE_FILE", str(tmp_path / "routines.yaml"))


@pytest.fixture
def agents(monkeypatch):
    """Record every NovaAgent the platform constructs."""
    from custom_components.nova.conversation import NovaAgent
    made: list = []
    real_init = NovaAgent.__init__

    def _init(self, hass, entry):
        real_init(self, hass, entry)
        made.append(self)

    monkeypatch.setattr(NovaAgent, "__init__", _init)
    return made


@pytest.fixture
def isolated_memory(monkeypatch, tmp_path):
    from custom_components.nova import conversation, database, knowledge, memory
    from custom_components.nova.diagnostics import service_health
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "conversations.db")
    monkeypatch.setattr(knowledge, "DB_PATH", str(tmp_path / "knowledge.db"))
    monkeypatch.setattr(conversation, "PERSONA_FILE", str(tmp_path / "persona.txt"))
    monkeypatch.setattr(memory, "store_memory", lambda *a, **k: None)
    monkeypatch.setattr(memory, "get_conversation_context", lambda *a, **k: "")
    monkeypatch.setattr(service_health, "record_usage", lambda *a, **k: None)


def _snapshot(pairings):
    """What the routing call saw at call time (the live dict may change)."""
    return dict(pairings) if isinstance(pairings, dict) else pairings


class _Fakes:
    """Records what the turn handed to the outward-facing calls."""

    def __init__(self, monkeypatch):
        from custom_components.nova import (
            agent, audio_routing, connectivity, conversation, local_engine,
        )
        self.try_local = AsyncMock(return_value=None)
        self.run_agent = AsyncMock(return_value="Here is your poem.")
        self.reply_target_calls: list = []
        self.reply_targets_calls: list = []
        self.reply_target_result = None
        self.reply_targets_result: list = []
        self.announce = AsyncMock(return_value=None)
        monkeypatch.setattr(local_engine, "try_local", self.try_local)
        monkeypatch.setattr(connectivity, "allow_request", lambda: True)
        monkeypatch.setattr(agent, "run_agent", self.run_agent)

        def _reply_target(hass, device_id=None, satellite_pairings=None, **kw):
            self.reply_target_calls.append(_snapshot(satellite_pairings))
            return self.reply_target_result

        def _reply_targets(hass, device_id=None, satellite_pairings=None, **kw):
            self.reply_targets_calls.append(_snapshot(satellite_pairings))
            return list(self.reply_targets_result)

        monkeypatch.setattr(audio_routing, "reply_target", _reply_target)
        monkeypatch.setattr(conversation, "reply_targets", _reply_targets)
        monkeypatch.setattr(conversation, "async_announce", self.announce)
        monkeypatch.setattr(conversation, "resolve_tts_entity",
                            lambda hass, engine: "tts.fake_engine")

    @property
    def agent_kwargs(self) -> dict:
        assert self.run_agent.await_count >= 1
        return self.run_agent.await_args.kwargs


@pytest.fixture
def fakes(monkeypatch, isolated_memory):
    return _Fakes(monkeypatch)


@pytest.fixture
def effective_calls(monkeypatch):
    """Record the runtime_config handed to effective_config_with_runtime
    (a per-turn snapshot of the live dict) while still returning the real
    merge."""
    from custom_components.nova import nova_config
    real = nova_config.effective_config_with_runtime
    seen: list = []

    def _spy(entry=None, runtime_config=None):
        seen.append(runtime_config)
        return real(entry, runtime_config)

    monkeypatch.setattr(nova_config, "effective_config_with_runtime", _spy)
    return seen


@pytest.fixture
def providers_built(monkeypatch):
    """Fail loudly if anything builds a provider after setup."""
    from custom_components.nova import llm_provider
    built: list = []

    def _create(*a, **kw):
        built.append((a, kw))
        raise AssertionError("a second provider was built")

    return built, lambda: monkeypatch.setattr(llm_provider, "create_provider", _create)


async def _setup(hass, **options):
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    entry = await _add_entry(hass, **options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass, entry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, entry.entry_id)
    assert entity_id is not None
    return entity_id


async def _converse(hass, entry, text, device_id=None):
    from homeassistant.components import conversation as conversation_component
    return await conversation_component.async_converse(
        hass, text, None, Context(), agent_id=_entity_id(hass, entry),
        device_id=device_id,
    )


def _speech(result) -> str:
    return result.response.speech.get("plain", {}).get("speech", "")


def _drift_bridge(hass, entry, **values):
    bridge = hass.data[DOMAIN][entry.entry_id]
    bridge["client"] = object()
    bridge["runtime_config"] = dict(values)


def _drop_runtime(entry):
    object.__delattr__(entry, "runtime_data")
    assert entry.state is ConfigEntryState.LOADED


def _unavailable_logged(caplog) -> bool:
    return any("Nova runtime is not available" in (r.getMessage() + str(r.exc_info))
               for r in caplog.records)


# ── Provider client ownership ───────────────────────────────────────────────

async def test_agent_client_is_runtime_client(hass, agents):
    entry = await _setup(hass)
    assert len(agents) == 1
    agent = agents[0]
    assert agent._client is entry.runtime_data.client
    assert agent._client is hass.data[DOMAIN][entry.entry_id]["client"]


async def test_construction_does_not_build_a_provider(hass, providers_built):
    from custom_components.nova.conversation import NovaAgent
    built, arm = providers_built
    entry = await _setup(hass)
    arm()
    agent = NovaAgent(hass, entry)
    assert agent._client is entry.runtime_data.client
    assert built == []


async def test_missing_runtime_prevents_construction(hass, providers_built):
    from custom_components.nova import conversation
    from custom_components.nova.runtime import NovaRuntimeUnavailable
    built, arm = providers_built
    entry = await _setup(hass)
    arm()
    _drop_runtime(entry)
    with pytest.raises(NovaRuntimeUnavailable):
        conversation.NovaAgent(hass, entry)
    added: list = []
    with pytest.raises(NovaRuntimeUnavailable):
        await conversation.async_setup_entry(hass, entry, added.extend)
    assert added == []
    assert built == []


@pytest.mark.parametrize("damage", ["missing", "damaged", "drifted"])
async def test_bridge_state_does_not_affect_construction(hass, providers_built, damage):
    from custom_components.nova.conversation import NovaAgent
    built, arm = providers_built
    entry = await _setup(hass)
    arm()
    entry.runtime_data.runtime_config["model"] = "live-model"
    if damage == "missing":
        hass.data[DOMAIN].pop(entry.entry_id)
    elif damage == "damaged":
        hass.data[DOMAIN][entry.entry_id] = "not a dict"
    else:
        _drift_bridge(hass, entry, model="stale-model")
    agent = NovaAgent(hass, entry)
    assert agent._client is entry.runtime_data.client
    assert agent._model() == "live-model"
    assert built == []


# ── Configuration on real turns ─────────────────────────────────────────────

async def test_turn_uses_live_runtime_config_and_ignores_drifted_bridge(
        hass, fakes, effective_calls):
    entry = await _setup(hass)
    live = entry.runtime_data.runtime_config
    live.update({"model": "live-model", "llm_provider": "ollama",
                 "ollama_base_url": "http://live-a:11434"})
    _drift_bridge(hass, entry, model="stale-model", llm_provider="groq",
                  ollama_base_url="http://stale:11434")
    effective_calls.clear()   # setup's own calls

    result = await _converse(hass, entry, _text())
    assert _speech(result) == "Here is your poem."
    kw = fakes.agent_kwargs
    assert kw["model"] == "live-model"
    assert kw["provider_name"] == "ollama"
    assert kw["config"]["ollama_base_url"] == "http://live-a:11434"
    assert effective_calls[-1] == live
    assert effective_calls[-1] is not live       # a snapshot, not the live dict

    # In place, no reload: the next turn sees the new values.
    live["model"] = "live-model-2"
    live["ollama_base_url"] = "http://live-b:11434"
    await _converse(hass, entry, _text())
    kw = fakes.agent_kwargs
    assert kw["model"] == "live-model-2"
    assert kw["config"]["ollama_base_url"] == "http://live-b:11434"
    assert effective_calls[-1] == live
    assert effective_calls[-1] is not live
    assert effective_calls[0] is not effective_calls[1]
    assert len(effective_calls) == 2


@pytest.mark.parametrize("damage", ["missing", "damaged"])
async def test_turn_works_without_a_usable_bridge(hass, fakes, damage):
    entry = await _setup(hass)
    entry.runtime_data.runtime_config["model"] = "live-model"
    if damage == "missing":
        hass.data[DOMAIN].pop(entry.entry_id)
    else:
        hass.data[DOMAIN][entry.entry_id] = ["not", "a", "dict"]
    result = await _converse(hass, entry, _text())
    assert _speech(result) == "Here is your poem."
    assert fakes.agent_kwargs["model"] == "live-model"


async def test_reasoning_fallback_overlay_keeps_empty_value_rule(hass, fakes):
    """None/"" runtime values never clobber the persisted config: the model
    and endpoint come from the entry, a set runtime value wins."""
    entry = await _setup(hass)
    live = entry.runtime_data.runtime_config
    live.update({"model": "", "llm_base_url": None, "honorific": "captain"})
    await _converse(hass, entry, _text())
    kw = fakes.agent_kwargs
    assert kw["model"] == "llama3"                     # entry.data
    assert kw["config"]["model"] == "llama3"
    assert kw["config"]["llm_base_url"] == "http://localhost:11434/v1"
    assert kw["config"]["honorific"] == "captain"
    assert kw["provider_name"] == "ollama"


async def test_reasoning_credentials_and_endpoint_match_effective_config(hass, fakes):
    """The provider credential and endpoint are resolved from the same merged
    config the old in-loop overlay produced."""
    from custom_components.nova import nova_config
    from custom_components.nova.llm_provider import (
        resolve_provider_credential, resolve_provider_endpoint,
    )
    entry = await _setup(hass)
    live = entry.runtime_data.runtime_config
    live.update({"llm_provider": "ollama", "ollama_base_url": "http://rt:11434",
                 "empty": ""})
    await _converse(hass, entry, _text())
    kw = fakes.agent_kwargs
    base = await hass.async_add_executor_job(nova_config.effective_config, entry)
    old = {**base, **{k: v for k, v in live.items() if v not in (None, "")}}
    assert kw["config"] == old
    assert kw["base_url"] == resolve_provider_endpoint(old, "ollama")
    assert kw["api_key"] == resolve_provider_credential(old, "ollama")


# ── Satellite pairings routing ──────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ({"sat1": "media_player.kitchen"}, {"sat1": "media_player.kitchen"}),
    ('{"sat1": "media_player.lounge"}', {"sat1": "media_player.lounge"}),
    ("{not json", None),
    ("[1, 2]", None),
    ({}, None),
    ("", None),
])
async def test_both_routing_sites_read_live_pairings(hass, fakes, raw, expected):
    entry = await _setup(hass)
    _drift_bridge(hass, entry, satellite_pairings={"sat1": "media_player.stale"})
    entry.runtime_data.runtime_config["satellite_pairings"] = raw
    await _converse(hass, entry, _text(), device_id="sat1")
    # Final routing resolves nothing, so it falls back to _speakers().
    assert fakes.reply_target_calls == [expected]
    assert fakes.reply_targets_calls == [expected]
    fakes.announce.assert_not_called()


async def test_pairings_change_in_place_between_turns(hass, fakes):
    entry = await _setup(hass)
    live = entry.runtime_data.runtime_config
    live["satellite_pairings"] = {"sat1": "media_player.a"}
    await _converse(hass, entry, _text(), device_id="sat1")
    live["satellite_pairings"]["sat1"] = "media_player.b"
    await _converse(hass, entry, _text(), device_id="sat1")
    live["satellite_pairings"] = '{"sat1": "media_player.c"}'
    await _converse(hass, entry, _text(), device_id="sat1")
    assert fakes.reply_target_calls == [
        {"sat1": "media_player.a"}, {"sat1": "media_player.b"},
        {"sat1": "media_player.c"}]
    assert fakes.reply_targets_calls == fakes.reply_target_calls


async def test_paired_speaker_gets_the_reply(hass, fakes):
    entry = await _setup(hass)
    entry.runtime_data.runtime_config["satellite_pairings"] = {
        "sat1": "media_player.kitchen"}
    hass.states.async_set("media_player.kitchen", "idle")
    fakes.reply_target_result = "media_player.kitchen"
    await _converse(hass, entry, _text(), device_id="sat1")
    await hass.async_block_till_done()
    assert fakes.reply_target_calls == [{"sat1": "media_player.kitchen"}]
    fakes.announce.assert_awaited_once()
    args = fakes.announce.await_args.args
    assert args[2:] == ("tts.fake_engine", ["media_player.kitchen"])


# ── Loaded entry that lost its runtime ──────────────────────────────────────

async def test_lost_runtime_fails_turn_before_any_work(hass, fakes, caplog,
                                                       providers_built):
    built, arm = providers_built
    entry = await _setup(hass)
    arm()
    entry.runtime_data.runtime_config["satellite_pairings"] = {"sat1": "media_player.k"}
    hass.states.async_set("media_player.k", "idle")
    fakes.reply_target_result = "media_player.k"
    _drop_runtime(entry)
    caplog.set_level(logging.ERROR)
    result = await _converse(hass, entry, _text(), device_id="sat1")
    await hass.async_block_till_done()
    assert _speech(result) == "I ran into an internal error handling that request."
    assert _unavailable_logged(caplog)
    fakes.try_local.assert_not_called()
    fakes.run_agent.assert_not_called()
    fakes.announce.assert_not_called()
    assert fakes.reply_target_calls == []
    assert fakes.reply_targets_calls == []
    assert built == []
