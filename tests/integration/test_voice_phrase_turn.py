"""PHACC-tier proof for voice phrases (8.32.0), through the real conversation
stack: a matched phrase runs before Nova's local commands and the agent, a
phrase that needs confirming goes to the agent with a hint, a phrase passes
the relevance gate, and with no phrases nothing changes. Run in the
integration venv like the other files here. Nothing touches a real device:
the script services are fakes registered on the test hass.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.core import Context

from .test_chat_turn import (
    AMBIENT_TEXT, _converse, _isolate_conversations_db, _setup_nova, _speech,
)


def _entry(phrases=("the house is cold",), target="script.heating_boost",
           reply="Warming the house up{addr}.", allow_ai=False):
    return {"phrase_id": "vp_3f9c1a2b", "name": "House is cold",
            "phrases": list(phrases), "match": "flexible", "allow_ai": allow_ai,
            "enabled": True, "reply": reply,
            "action": {"service": "script.turn_on", "data": {},
                       "target": {"entity_id": target}}}


def _phrases(monkeypatch, *entries, **settings):
    from custom_components.nova import nova_config
    cache = nova_config._cache_dict()
    monkeypatch.setitem(cache, "voice_phrases", {"enabled": True, "entries": list(entries)})
    for key, value in settings.items():
        monkeypatch.setitem(cache, key, value)


def _script_calls(hass):
    calls = []

    async def handler(call):
        calls.append(dict(call.data))
    hass.services.async_register("script", "turn_on", handler)
    return calls


class _Engine:
    """The local engine and the agent, faked so no provider is called."""

    def __init__(self, reply="From the agent."):
        self.local_calls = 0
        self.personas = []
        self.reply = reply

    def patches(self):
        async def try_local(*a, **k):
            self.local_calls += 1
            return SimpleNamespace(handled=False, text="")

        async def run_agent(*a, **k):
            self.personas.append(k.get("persona", ""))
            return self.reply
        return (patch("custom_components.nova.local_engine.try_local", new=try_local),
                patch("custom_components.nova.agent.run_agent", new=run_agent))


async def test_a_phrase_runs_before_the_local_engine_and_the_agent(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    calls = _script_calls(hass)
    _phrases(monkeypatch, _entry())
    eng = _Engine()
    local, agent = eng.patches()
    from custom_components.nova import cognitive_core
    logged = []
    monkeypatch.setattr(cognitive_core, "log_command",
                        lambda **kw: logged.append(kw["handled_by"]))
    with local, agent:
        result = await _converse(hass, entity_id, "Nova, the house is cold.", "voice-1")
        await hass.async_block_till_done()
    assert eng.local_calls == 0 and eng.personas == []
    assert calls == [{"entity_id": "script.heating_boost"}]
    assert _speech(result).startswith("Warming the house up")
    assert logged == ["local"]


async def test_a_near_miss_runs_nothing(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    calls = _script_calls(hass)
    _phrases(monkeypatch, _entry())
    eng = _Engine()
    local, agent = eng.patches()
    with local, agent:
        result = await _converse(hass, entity_id, "the house is not cold", "voice-1")
        await hass.async_block_till_done()
    assert calls == [] and eng.local_calls == 1
    assert _speech(result) == "From the agent."


async def test_a_phrase_that_needs_confirming_goes_to_the_agent(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    calls = _script_calls(hass)
    _phrases(monkeypatch, _entry(target=["script.heating_boost", "script.fan_off"]),
             voice_confirm_enabled=True)
    from custom_components.nova import connectivity, voice_phrase_runner
    inside, probes = {"on": False}, []
    real_allow, real_step = connectivity.allow_request, voice_phrase_runner.try_phrase

    def allow_request():
        probes.append(inside["on"])
        return real_allow()

    async def try_phrase(*a, **k):
        inside["on"] = True
        try:
            return await real_step(*a, **k)
        finally:
            inside["on"] = False
    monkeypatch.setattr(connectivity, "allow_request", allow_request)
    monkeypatch.setattr(voice_phrase_runner, "try_phrase", try_phrase)
    eng = _Engine("Shall I run House is cold?")
    local, agent = eng.patches()
    with local, agent:
        result = await _converse(hass, entity_id, "the house is cold", "voice-1")
        await hass.async_block_till_done()
    assert calls == []                                   # nothing ran
    assert eng.local_calls == 0                          # the local engine never saw it
    assert len(eng.personas) == 1                        # the agent did
    assert "BEGIN_VOICE_PHRASE_MATCH_" in eng.personas[0]
    assert "script.heating_boost, script.fan_off" in eng.personas[0]
    assert True not in probes                            # no breaker probe in the step
    assert _speech(result) == "Shall I run House is cold?"


async def test_a_refusal_on_the_second_target_runs_nothing(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    calls = _script_calls(hass)
    _phrases(monkeypatch, _entry(target=["script.heating_boost", "script.fan_off"]),
             voice_confirm_enabled=True,
             voice_confirm_entities=["!script.heating_boost"])
    eng = _Engine()
    local, agent = eng.patches()
    with local, agent:
        await _converse(hass, entity_id, "the house is cold", "voice-1")
        await hass.async_block_till_done()
    # Deferred, not dropped: the local engine never saw it, the agent did.
    assert calls == [] and eng.local_calls == 0 and len(eng.personas) == 1
    assert "script.heating_boost, script.fan_off" in eng.personas[0]


async def test_a_multi_target_phrase_is_one_call(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    calls = _script_calls(hass)
    _phrases(monkeypatch, _entry(target=["script.heating_boost", "script.fan_off"]))
    eng = _Engine()
    local, agent = eng.patches()
    with local, agent:
        await _converse(hass, entity_id, "the house is cold", "voice-1")
        await hass.async_block_till_done()
    assert calls == [{"entity_id": ["script.heating_boost", "script.fan_off"]}]


async def test_a_phrase_passes_the_relevance_gate(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    calls = _script_calls(hass)
    eng = _Engine()
    local, agent = eng.patches()
    with local, agent:
        dropped = await _converse(hass, entity_id, AMBIENT_TEXT, "voice-1")
        _phrases(monkeypatch, _entry(phrases=(AMBIENT_TEXT,), reply="Done."))
        kept = await _converse(hass, entity_id, AMBIENT_TEXT, "voice-2")
        await hass.async_block_till_done()
    assert _speech(dropped) == ""                 # ambient speech, as before
    assert _speech(kept) == "Done." and len(calls) == 1


async def test_a_chat_tab_turn_runs_a_phrase(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    calls = _script_calls(hass)
    _phrases(monkeypatch, _entry(reply="Done."))
    eng = _Engine()
    local, agent = eng.patches()
    with local, agent:
        result = await _converse(hass, entity_id, "the house is cold", "nova_chat_u1",
                                 user_id="u1")
        await hass.async_block_till_done()
    assert _speech(result) == "Done." and len(calls) == 1


async def test_with_no_phrases_nothing_changes(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    calls = _script_calls(hass)
    eng = _Engine()
    local, agent = eng.patches()
    with local, agent:
        result = await _converse(hass, entity_id, "the house is cold", "voice-1")
        await hass.async_block_till_done()
    assert calls == [] and eng.local_calls == 1
    assert len(eng.personas) == 1 and "VOICE_PHRASE" not in eng.personas[0]
    assert _speech(result) == "From the agent."


async def test_phrases_the_ai_may_pick_reach_its_prompt_fenced(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    _script_calls(hass)
    hidden = dict(_entry(phrases=("film night",)), phrase_id="vp_00000002",
                  name="Hidden one", allow_ai=False)
    _phrases(monkeypatch, _entry(allow_ai=True), hidden)
    eng = _Engine()
    local, agent = eng.patches()
    with local, agent:
        await _converse(hass, entity_id, "it is a bit nippy", "voice-1")
    (persona,) = eng.personas
    begin = persona.index("\nBEGIN_VOICE_PHRASES_")
    end = persona.index("\nEND_VOICE_PHRASES_")
    inside = persona[begin:end]
    assert '[1] House is cold (e.g. "the house is cold")' in inside
    assert "vp_" not in inside.split("\n", 2)[2]          # no id among the user's text
    assert "1 = vp_3f9c1a2b" in persona[:begin]
    assert "Hidden one" not in persona and "vp_00000002" not in persona
