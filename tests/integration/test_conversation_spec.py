"""The conversation agent, checked against its spec (7c).

Two halves. The module's plain helpers (the multi wake dedup, the offline
sentinel check, the persona file cache, the chat thread reset and the Home
Assistant keyword filter) are called directly. The turn itself runs through
the real conversation stack on a Chat tab thread, so neither the relevance
gate nor the dedup gets in the way, with the local engine and the agent
faked: a reply from the local engine, the offline answer, the agent's
connection sentinel, the agent raising, and an error anywhere else in the
turn. Run in the integration venv like the other files here.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from .test_chat_turn import _converse, _isolate_conversations_db, _setup_nova, _speech

CHAT = "nova_chat_u1"


@pytest.fixture
def conv():
    from custom_components.nova import conversation
    conversation._dedup_cache.clear()
    yield conversation
    conversation._dedup_cache.clear()


# ── Multi wake dedup ────────────────────────────────────────────────────────

def test_the_first_device_to_hear_a_request_claims_it(conv):
    assert conv._check_and_claim_dedup("Turn on the lights", "sat_a") == (False, None)
    assert conv._check_and_claim_dedup("turn on the lights!", "sat_b") == (True, None)


def test_a_duplicate_gets_the_winning_reply_once_it_is_recorded(conv):
    conv._check_and_claim_dedup("what time is it", "sat_a")
    conv._record_dedup_response("What time is it?", "It is noon.")
    assert conv._check_and_claim_dedup("what time is it", "sat_b") == (True, "It is noon.")


def test_the_same_device_asking_again_is_never_a_duplicate(conv):
    conv._check_and_claim_dedup("hello", "sat_a")
    assert conv._check_and_claim_dedup("hello", "sat_a") == (False, None)


def test_recording_a_reply_for_an_unclaimed_text_does_nothing(conv):
    conv._record_dedup_response("never asked", "reply")
    assert conv._dedup_cache == {}


def test_a_claim_lapses_after_the_window(conv, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(conv.time, "time", lambda: clock[0])
    conv._check_and_claim_dedup("lights", "sat_a")
    clock[0] += conv._DEDUP_WINDOW + 0.1
    assert conv._check_and_claim_dedup("lights", "sat_b") == (False, None)
    assert conv._dedup_cache["lights"][2] == "sat_b"


def test_stale_claims_are_swept_after_twice_the_window(conv, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(conv.time, "time", lambda: clock[0])
    conv._check_and_claim_dedup("old words", "sat_a")
    clock[0] += conv._DEDUP_WINDOW * 2 + 0.1
    conv._check_and_claim_dedup("new words", "sat_a")
    assert set(conv._dedup_cache) == {"new words"}


# ── Offline sentinel ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("", True),
    ("I'm having trouble connecting to my reasoning systems.", True),
    ("My reasoning systems are having connectivity issues.", True),
    ("My reasoning systems are fine.", False),
    ("The router has connectivity issues.", False),
    ("The heating is on.", False),
])
def test_only_the_agents_own_offline_sentence_counts_as_a_failure(conv, text, expected):
    assert conv._is_connectivity_failure(text) is expected


# ── Persona file ────────────────────────────────────────────────────────────

def test_a_missing_persona_file_reads_as_nothing(conv, monkeypatch, tmp_path):
    monkeypatch.setattr(conv, "PERSONA_FILE", str(tmp_path / "missing.txt"))
    assert conv._sync_load_persona() == (0.0, None)


def test_a_blank_persona_file_reads_as_no_text(conv, monkeypatch, tmp_path):
    path = tmp_path / "persona.txt"
    path.write_text("   \n")
    monkeypatch.setattr(conv, "PERSONA_FILE", str(path))
    mtime, text = conv._sync_load_persona()
    assert mtime > 0 and text is None


def test_the_persona_file_is_read_trimmed(conv, monkeypatch, tmp_path):
    path = tmp_path / "persona.txt"
    path.write_text("\n  Speak like a butler.  \n")
    monkeypatch.setattr(conv, "PERSONA_FILE", str(path))
    assert conv._sync_load_persona()[1] == "Speak like a butler."


class _Hass:
    def __init__(self):
        self.reads = 0

    async def async_add_executor_job(self, fn, *args):
        self.reads += 1
        return fn(*args)


async def test_the_persona_cache_is_rechecked_at_most_every_ttl(conv, monkeypatch, tmp_path):
    path = tmp_path / "persona.txt"
    path.write_text("First.")
    monkeypatch.setattr(conv, "PERSONA_FILE", str(path))
    monkeypatch.setattr(conv, "_persona_cache", {"mtime": 0.0, "text": None, "checked": 0.0})
    clock = [5000.0]
    monkeypatch.setattr(conv.time, "time", lambda: clock[0])
    hass = _Hass()
    await conv._ensure_persona_loaded(hass)
    assert conv._persona_cache["text"] == "First."
    path.write_text("Second.")
    import os
    os.utime(path, (1, 1))
    clock[0] += conv._PERSONA_MTIME_TTL - 1
    await conv._ensure_persona_loaded(hass)
    assert hass.reads == 1 and conv._persona_cache["text"] == "First."
    clock[0] += 2
    await conv._ensure_persona_loaded(hass)
    assert hass.reads == 2 and conv._persona_cache["text"] == "Second."


async def test_an_empty_cache_is_always_refilled(conv, monkeypatch, tmp_path):
    monkeypatch.setattr(conv, "PERSONA_FILE", str(tmp_path / "missing.txt"))
    monkeypatch.setattr(conv, "_persona_cache", {"mtime": 0.0, "text": None, "checked": 0.0})
    monkeypatch.setattr(conv.time, "time", lambda: 100.0)
    hass = _Hass()
    await conv._ensure_persona_loaded(hass)
    await conv._ensure_persona_loaded(hass)
    assert hass.reads == 2


# ── Small helpers ───────────────────────────────────────────────────────────

class _FakeAgent:
    def __init__(self):
        self._histories = {"nova_chat_u1": [1], "other": [2]}
        self._last_seen = {"nova_chat_u1": 1.0, "other": 2.0}


def test_new_chat_forgets_one_thread_in_every_live_agent(conv):
    agents = [_FakeAgent(), _FakeAgent()]
    for agent in agents:
        conv._AGENTS.add(agent)
    try:
        conv.clear_chat_thread("nova_chat_u1")
    finally:
        for agent in agents:
            conv._AGENTS.discard(agent)
    for agent in agents:
        assert agent._histories == {"other": [2]}
        assert agent._last_seen == {"other": 2.0}


def test_the_turn_user_comes_from_the_context(conv):
    assert conv._turn_user_id(SimpleNamespace(context=SimpleNamespace(user_id="u9"))) == "u9"
    assert conv._turn_user_id(SimpleNamespace(context=None)) is None
    assert conv._turn_user_id(SimpleNamespace()) is None


def test_only_accepted_keywords_reach_a_constructor(conv):
    def build(a, b=None):
        return a, b
    assert conv._ha_kwargs(build, a=1, b=2, c=3) == {"a": 1, "b": 2}


def test_an_unreadable_signature_passes_everything(conv):
    assert conv._ha_kwargs(object(), a=1) == {"a": 1}


@pytest.mark.parametrize("text, expected", [("yes", "yes"), ("no", "no"),
                                            ("turn on the lights", None), (None, None)])
def test_an_offer_answer_is_a_plain_yes_or_no(conv, text, expected):
    assert conv._offer_answer(text) == expected


def test_a_broken_phrase_store_never_matches(conv, monkeypatch):
    from custom_components.nova import nova_config

    def _boom(*a, **k):
        raise RuntimeError("store unreadable")
    monkeypatch.setattr(nova_config, "get", _boom)
    assert conv._matches_voice_phrase("I'm cold") is False


# ── The agent's prompt and history ──────────────────────────────────────────

async def _agent(hass, monkeypatch, tmp_path):
    from custom_components.nova import conversation
    made = []
    real_init = conversation.NovaAgent.__init__

    def _init(self, h, entry):
        real_init(self, h, entry)
        made.append(self)
    monkeypatch.setattr(conversation.NovaAgent, "__init__", _init)
    _isolate_conversations_db(monkeypatch, tmp_path)
    monkeypatch.setattr(conversation, "PERSONA_FILE", str(tmp_path / "persona.txt"))
    monkeypatch.setattr(conversation, "_persona_cache",
                        {"mtime": 0.0, "text": None, "checked": 0.0})
    await _setup_nova(hass)
    return made[0]


async def test_the_prompt_puts_the_directive_first_then_the_live_context(
        hass, monkeypatch, tmp_path):
    agent = await _agent(hass, monkeypatch, tmp_path)
    hass.states.async_set("weather.home", "sunny",
                          {"temperature": 18, "temperature_unit": "°C"})
    hass.states.async_set("weather.other", "rainy", {"temperature": 3})
    prompt = agent._persona()
    directive = agent._directive()
    assert prompt.startswith(directive.split("{honorific}")[0][:40])
    assert "\n\n---\n\n" in prompt
    context = prompt.split("## Current context (live data)\n", 1)[1]
    assert context.startswith("Current time: ")
    assert "temperatures in " in context
    assert "Weather: sunny, 18°C." in context
    assert "rainy" not in context


async def test_a_persona_file_replaces_the_built_in_character(hass, monkeypatch, tmp_path):
    agent = await _agent(hass, monkeypatch, tmp_path)
    from custom_components.nova import conversation
    (tmp_path / "persona.txt").write_text("You are a lighthouse keeper.")
    await conversation._ensure_persona_loaded(hass)
    prompt = agent._persona()
    assert "You are a lighthouse keeper." in prompt
    assert conversation.NOVA_PERSONA[:60] not in prompt


async def test_history_keeps_only_the_latest_window(hass, monkeypatch, tmp_path):
    agent = await _agent(hass, monkeypatch, tmp_path)
    from custom_components.nova import conversation
    agent._histories["c"] = list(range(conversation.MAX_HISTORY + 5))
    history = agent._history("c")
    assert history == list(range(5, conversation.MAX_HISTORY + 5))
    assert agent._history("c") is history
    assert agent._history("new") == [] and "new" in agent._histories


async def test_the_speaker_hint_needs_both_a_voice_and_an_identity(hass, monkeypatch, tmp_path):
    agent = await _agent(hass, monkeypatch, tmp_path)
    assert agent._speaker_hint(None, object()) == ""
    assert agent._speaker_hint(object(), None) == ""


# ── A turn ──────────────────────────────────────────────────────────────────

def _local(result=None):
    return patch("custom_components.nova.local_engine.try_local",
                 new=AsyncMock(return_value=result))


async def _turn(hass, monkeypatch, tmp_path, text="write me a poem about the sea"):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    return entity_id, text


async def test_a_local_reply_is_the_answer_and_the_agent_is_not_called(
        hass, monkeypatch, tmp_path):
    entity_id, text = await _turn(hass, monkeypatch, tmp_path)
    agent = AsyncMock(return_value="agent")
    with _local(SimpleNamespace(handled=True, text="Done.", repeat_of_id=None)), \
            patch("custom_components.nova.agent.run_agent", new=agent):
        result = await _converse(hass, entity_id, text, CHAT, user_id="u1")
    assert _speech(result) == "Done."
    agent.assert_not_called()


async def test_the_offline_answer_when_the_connection_is_down(hass, monkeypatch, tmp_path):
    entity_id, text = await _turn(hass, monkeypatch, tmp_path)
    from custom_components.nova import connectivity
    monkeypatch.setattr(connectivity, "allow_request", lambda: False)
    local = AsyncMock(return_value=None)
    with patch("custom_components.nova.local_engine.try_local", new=local):
        result = await _converse(hass, entity_id, text, CHAT, user_id="u1")
    assert _speech(result).startswith("I'm offline at the moment")
    assert "I can still control your devices" in _speech(result)
    assert [c.kwargs.get("force", False) for c in local.await_args_list] == [False, True]


async def test_the_agents_offline_sentence_is_a_failure_and_tries_the_local_engine(
        hass, monkeypatch, tmp_path):
    entity_id, text = await _turn(hass, monkeypatch, tmp_path)
    from custom_components.nova import connectivity
    failures = []
    monkeypatch.setattr(connectivity, "allow_request", lambda: True)
    monkeypatch.setattr(connectivity, "record_failure", lambda: failures.append(1))
    local = AsyncMock(side_effect=[None, SimpleNamespace(handled=True, text="Salvaged.")])
    sentinel = "I'm having trouble connecting to my reasoning systems."
    with patch("custom_components.nova.local_engine.try_local", new=local), \
            patch("custom_components.nova.agent.run_agent", new=AsyncMock(return_value=sentinel)):
        result = await _converse(hass, entity_id, text, CHAT, user_id="u1")
    assert _speech(result) == "Salvaged."
    assert failures == [1]


async def test_an_unsalvaged_offline_sentence_is_spoken_as_it_is(hass, monkeypatch, tmp_path):
    entity_id, text = await _turn(hass, monkeypatch, tmp_path)
    from custom_components.nova import connectivity
    monkeypatch.setattr(connectivity, "allow_request", lambda: True)
    monkeypatch.setattr(connectivity, "record_failure", lambda: None)
    sentinel = "I'm having trouble connecting to my reasoning systems."
    with _local(None), \
            patch("custom_components.nova.agent.run_agent", new=AsyncMock(return_value=sentinel)):
        result = await _converse(hass, entity_id, text, CHAT, user_id="u1")
    assert _speech(result) == sentinel


async def test_an_agent_that_raises_gets_the_lost_connection_reply(hass, monkeypatch, tmp_path):
    entity_id, text = await _turn(hass, monkeypatch, tmp_path)
    from custom_components.nova import connectivity
    monkeypatch.setattr(connectivity, "allow_request", lambda: True)
    monkeypatch.setattr(connectivity, "record_failure", lambda: None)
    with _local(None), patch("custom_components.nova.agent.run_agent",
                             new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await _converse(hass, entity_id, text, CHAT, user_id="u1")
    assert _speech(result).startswith("I've lost connection to my reasoning systems")
    assert _speech(result).endswith("report status while I reconnect.")


async def test_a_good_answer_records_a_success(hass, monkeypatch, tmp_path):
    entity_id, text = await _turn(hass, monkeypatch, tmp_path)
    from custom_components.nova import connectivity
    successes = []
    monkeypatch.setattr(connectivity, "allow_request", lambda: True)
    monkeypatch.setattr(connectivity, "record_success", lambda: successes.append(1))
    with _local(None), patch("custom_components.nova.agent.run_agent",
                             new=AsyncMock(return_value="A poem.")):
        result = await _converse(hass, entity_id, text, CHAT, user_id="u1")
    assert _speech(result) == "A poem." and successes == [1]


async def test_an_error_in_the_turn_gives_a_rotating_fallback_and_drops_the_question(
        hass, monkeypatch, tmp_path):
    from custom_components.nova import conversation, local_engine
    made = []
    real_init = conversation.NovaAgent.__init__

    def _init(self, h, entry):
        real_init(self, h, entry)
        made.append(self)
    monkeypatch.setattr(conversation.NovaAgent, "__init__", _init)
    entity_id, text = await _turn(hass, monkeypatch, tmp_path)

    def _boom(text):
        raise RuntimeError("scoring failed")
    monkeypatch.setattr(local_engine, "score_complexity", _boom)
    with _local(None):
        first = await _converse(hass, entity_id, text, CHAT, user_id="u1")
        second = await _converse(hass, entity_id, text + " again", CHAT, user_id="u1")
    assert _speech(first).startswith("Technical difficulties")
    assert _speech(second).startswith("I appear to be having connectivity issues")
    assert made[0]._histories[CHAT] == []


async def test_another_users_thread_is_refused(hass, monkeypatch, tmp_path):
    entity_id, text = await _turn(hass, monkeypatch, tmp_path)
    result = await _converse(hass, entity_id, text, CHAT, user_id="u2")
    assert _speech(result) == "That chat thread belongs to someone else."


async def test_both_halves_of_a_turn_join_the_history(hass, monkeypatch, tmp_path):
    from custom_components.nova import conversation
    made = []
    real_init = conversation.NovaAgent.__init__

    def _init(self, h, entry):
        real_init(self, h, entry)
        made.append(self)
    monkeypatch.setattr(conversation.NovaAgent, "__init__", _init)
    entity_id, text = await _turn(hass, monkeypatch, tmp_path)
    with _local(SimpleNamespace(handled=True, text="Done.", repeat_of_id=None)):
        await _converse(hass, entity_id, text, CHAT, user_id="u1")
    await asyncio.sleep(0)
    assert made[0]._histories[CHAT][-2:] == [
        {"role": "user", "content": text}, {"role": "assistant", "content": "Done."}]
