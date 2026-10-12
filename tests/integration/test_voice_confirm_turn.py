"""PHACC-tier proof for a spoken "are you sure?" on the satellite you spoke to
(8.33.0), through the real conversation stack and two registered voice
satellites, A and B.

A request on A that needs a spoken confirmation asks nothing separately (no
announcement, no ask_question, no start_conversation during the turn), holds
one item for A, and keeps A's mic open for the answer. "Yes." from A runs it
once; anything from B, the Chat tab, or after the item expired runs nothing
and leaves A's item alone. The script services are fakes registered on the
test hass. Nothing touches a real device.
"""
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import Context

from .test_chat_turn import _isolate_conversations_db, _setup_nova, _speech
from .test_wiring_smoke import _make_entry  # noqa: F401  (same entry as _setup_nova)

PID = "vp_3f9c1a2b"
# A looser match (tier flexible): on a voice satellite Nova itself asks
# "House is cold?" and holds it (8.34.0). The exact words run at once.
LOOSE = "the house is a bit cold"


def _phrase(targets=("script.heating_boost",), allow_ai=True):
    return {"phrase_id": PID, "name": "House is cold", "phrases": ["the house is cold"],
            "match": "flexible", "allow_ai": allow_ai, "enabled": True,
            "reply": "Warming the house up.",
            "action": {"service": "script.turn_on", "data": {},
                       "target": {"entity_id": list(targets)}}}


def _settings(monkeypatch, *entries, **extra):
    from custom_components.nova import nova_config
    cache = nova_config._cache_dict()
    monkeypatch.setitem(cache, "voice_phrases", {"enabled": True, "entries": list(entries)})
    monkeypatch.setitem(cache, "voice_confirm_enabled", True)
    for k, v in extra.items():
        monkeypatch.setitem(cache, k, v)


def _satellite(hass, name):
    """A registered assist_satellite with its own device; returns the device id."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    entry = hass.config_entries.async_entries("nova")[0]
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test_sat", name)}, name=name)
    ent = er.async_get(hass).async_get_or_create(
        "assist_satellite", "test_sat", name, config_entry=None, device_id=dev.id)
    hass.states.async_set(ent.entity_id, "idle")
    return dev.id, ent.entity_id


def _recorders(hass):
    calls = {"script": [], "assist_satellite": [], "tts": []}

    def rec(bucket):
        async def handler(call):
            calls[bucket].append((call.service, dict(call.data)))
        return handler
    hass.services.async_register("script", "turn_on", rec("script"))
    for svc in ("start_conversation", "ask_question", "announce"):
        hass.services.async_register("assist_satellite", svc, rec("assist_satellite"))
    hass.services.async_register("tts", "speak", rec("tts"))
    return calls


class _Agent:
    """The agent, faked: for "it is nippy in here" (no phrase matches) it
    picks the phrase by meaning with Nova's real run_voice_phrase tool and
    replies with the tool's question. Anything else gets a plain answer."""

    def __init__(self):
        self.calls = 0
        self.local_calls = 0

    def patches(self):
        async def try_local(*a, **k):
            self.local_calls += 1
            return SimpleNamespace(handled=False, text="")

        async def run_agent(hass, *a, **k):
            import json
            from custom_components.nova.agent_runtime.capabilities.control import (
                _exec_run_voice_phrase)
            self.calls += 1
            ui = k.get("user_input")
            if "nippy" not in (getattr(ui, "text", "") or ""):
                return "Something else."
            res = json.loads(await _exec_run_voice_phrase(
                hass, {"phrase_id": PID}, device_id=getattr(ui, "device_id", None),
                user_id=getattr(getattr(ui, "context", None), "user_id", None)))
            return res.get("message") or "Something else."
        return (patch("custom_components.nova.local_engine.try_local", new=try_local),
                patch("custom_components.nova.agent.run_agent", new=run_agent))


async def _say(hass, entity_id, text, device_id, conversation_id=None, user_id="u1"):
    from homeassistant.components import conversation as conv
    from custom_components.nova import conversation as nova_conv
    nova_conv._dedup_cache.clear()     # each test turn is its own utterance
    return await conv.async_converse(
        hass, text, conversation_id or f"voice-{device_id}", Context(user_id=user_id),
        agent_id=entity_id, device_id=device_id)


async def _setup(hass, monkeypatch, tmp_path, *entries, **extra):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    from custom_components.nova import pending_confirm
    pending_confirm.clear_all()
    a, a_sat = _satellite(hass, "Living Room")
    b, _b_sat = _satellite(hass, "Work Area")
    calls = _recorders(hass)
    _settings(monkeypatch, *(entries or (_phrase(),)), **extra)
    return entity_id, a, a_sat, b, calls, pending_confirm


async def test_a_held_request_asks_nothing_separately_and_waits_on_a(hass, tmp_path,
                                                                     monkeypatch):
    entity_id, a, _a_sat, _b, calls, pc = await _setup(hass, monkeypatch, tmp_path)
    agent = _Agent()
    local, run = agent.patches()
    with local, run:
        res = await _say(hass, entity_id, LOOSE, a)
        await hass.async_block_till_done()
    assert calls == {"script": [], "assist_satellite": [], "tts": []}
    assert _speech(res) == "House is cold?"
    item = pc.get(a)
    assert (item.kind, item.ref) == ("voice_phrase", PID)
    assert agent.local_calls == 0 and agent.calls == 0       # Nova asked, not the AI
    # The mic stays open on A for the answer, with continued conversation off.
    assert getattr(res, "continue_conversation", False) is True


async def test_yes_from_a_runs_it_once_and_logs_approved(hass, tmp_path, monkeypatch):
    entity_id, a, _a_sat, _b, calls, pc = await _setup(hass, monkeypatch, tmp_path)
    agent = _Agent()
    local, run = agent.patches()
    with local, run:
        await _say(hass, entity_id, LOOSE, a)
        res = await _say(hass, entity_id, "Yes.", a)
        await hass.async_block_till_done()
        again = await _say(hass, entity_id, "Yes.", a)
        await hass.async_block_till_done()
    assert calls["script"] == [("turn_on", {"entity_id": "script.heating_boost"})]
    assert _speech(res) == "Warming the house up."
    assert pc.get(a) is None
    assert calls["script"] == [("turn_on", {"entity_id": "script.heating_boost"})]   # once
    assert _speech(again) != "Warming the house up."
    from custom_components.nova import action_log
    rows = [t for r in action_log.page_requests()["requests"] for t in r["targets"]
            if t["action"] == "voice_phrase"]
    assert "approved" in {t["approval_result"] for t in rows}


async def test_a_phrase_with_several_targets_replays_all_of_them(hass, tmp_path, monkeypatch):
    entity_id, a, _a_sat, _b, calls, _pc = await _setup(
        hass, monkeypatch, tmp_path, _phrase(targets=("script.heating_boost", "script.fan_off")))
    agent = _Agent()
    local, run = agent.patches()
    with local, run:
        await _say(hass, entity_id, LOOSE, a)
        await _say(hass, entity_id, "yes please", a)
        await hass.async_block_till_done()
    assert calls["script"] == [
        ("turn_on", {"entity_id": ["script.heating_boost", "script.fan_off"]})]


async def test_yes_from_b_or_chat_runs_nothing_and_leaves_a_alone(hass, tmp_path, monkeypatch):
    entity_id, a, _a_sat, b, calls, pc = await _setup(hass, monkeypatch, tmp_path)
    agent = _Agent()
    local, run = agent.patches()
    with local, run:
        await _say(hass, entity_id, LOOSE, a)
        await _say(hass, entity_id, "Yes.", b)
        await _say(hass, entity_id, "Yes.", None, conversation_id="nova_chat_u1")
        await hass.async_block_till_done()
        assert calls["script"] == [] and pc.get(a) is not None
        await _say(hass, entity_id, "Yes.", a)
        await hass.async_block_till_done()
    assert calls["script"] == [("turn_on", {"entity_id": "script.heating_boost"})]


async def test_an_expired_item_runs_nothing(hass, tmp_path, monkeypatch):
    import time
    entity_id, a, _a_sat, _b, calls, pc = await _setup(hass, monkeypatch, tmp_path)
    agent = _Agent()
    local, run = agent.patches()
    pc.put(a, pc.KIND_PHRASE, PID, "u1", now=time.monotonic() - 61)
    with local, run:
        await _say(hass, entity_id, "Yes.", a)
        await hass.async_block_till_done()
    assert calls["script"] == []


async def test_no_clears_it_and_runs_nothing(hass, tmp_path, monkeypatch):
    entity_id, a, _a_sat, _b, calls, pc = await _setup(hass, monkeypatch, tmp_path)
    agent = _Agent()
    local, run = agent.patches()
    with local, run:
        await _say(hass, entity_id, LOOSE, a)
        res = await _say(hass, entity_id, "No.", a)
        await hass.async_block_till_done()
    assert _speech(res) == "OK, I won't." and calls["script"] == [] and pc.get(a) is None


async def test_an_unrelated_sentence_clears_it_and_is_handled_normally(hass, tmp_path,
                                                                       monkeypatch):
    entity_id, a, _a_sat, _b, calls, pc = await _setup(hass, monkeypatch, tmp_path)
    agent = _Agent()
    local, run = agent.patches()
    with local, run:
        await _say(hass, entity_id, LOOSE, a)
        before = agent.local_calls
        await _say(hass, entity_id, "what time is it", a)
        await hass.async_block_till_done()
    assert pc.get(a) is None and calls["script"] == []
    assert agent.local_calls == before + 1            # processed as a normal turn


async def test_yes_answers_the_held_request_not_the_proactive_offer(hass, tmp_path,
                                                                    monkeypatch):
    entity_id, a, _a_sat, _b, calls, _pc = await _setup(hass, monkeypatch, tmp_path)
    from custom_components.nova import cognitive_core
    accepted = []

    async def accept(offer_id=None):
        accepted.append(True)
        return {"ok": True}
    monkeypatch.setattr(cognitive_core, "pending_offer_for",
                        lambda hass, device_id, **kw: {"action": "lights_off", "offer_id": "o1"})
    monkeypatch.setattr(cognitive_core, "accept_pending_offer", accept)
    agent = _Agent()
    local, run = agent.patches()
    with local, run:
        await _say(hass, entity_id, LOOSE, a)
        res = await _say(hass, entity_id, "yes", a)
        await hass.async_block_till_done()
    assert calls["script"] == [("turn_on", {"entity_id": "script.heating_boost"})]
    assert accepted == [] and _speech(res) == "Warming the house up."


async def test_the_mic_reopens_on_a_without_handoff_when_the_reply_plays_elsewhere(
        hass, tmp_path, monkeypatch):
    # Continued conversation off, multi satellite handoff on, and the reply
    # routed to a separate speaker (the Cast branch, as in the bug report).
    entity_id, a, a_sat, _b, _calls, _pc = await _setup(
        hass, monkeypatch, tmp_path,
        continued_conversation_enabled=False, continued_conversation_multi_satellite=True)
    from custom_components.nova import continued_conversation, conversation as nova_conv
    scheduled = []
    monkeypatch.setattr(continued_conversation, "schedule_reopen",
                        lambda hass, sat, spk, text, follow=True:
                        scheduled.append((sat, spk, follow)) or True)
    monkeypatch.setattr(nova_conv.NovaAgent, "_satellite_pairings",
                        lambda self: {a_sat: "media_player.living"})
    monkeypatch.setattr(nova_conv, "resolve_tts_entity", lambda hass, eng: "tts.nova")

    async def announce(*a, **k):
        return None
    monkeypatch.setattr(nova_conv, "async_announce", announce)
    hass.states.async_set("media_player.living", "idle")
    agent = _Agent()
    local, run = agent.patches()
    with local, run:
        res = await _say(hass, entity_id, LOOSE, a)
        await hass.async_block_till_done()
    assert scheduled == [(a_sat, "media_player.living", False)]
    assert _speech(res) == ""                          # the Cast speaker has it


async def test_with_no_held_request_turns_are_unchanged(hass, tmp_path, monkeypatch):
    entity_id, a, _a_sat, _b, calls, pc = await _setup(
        hass, monkeypatch, tmp_path, continued_conversation_enabled=False)
    agent = _Agent()
    local, run = agent.patches()
    with local, run:
        res = await _say(hass, entity_id, "Yes.", a)
        await hass.async_block_till_done()
    assert calls["script"] == [] and pc.get(a) is None
    assert agent.local_calls == 1                      # the normal path
    assert not getattr(res, "continue_conversation", False)


async def test_the_exact_words_run_at_once(hass, tmp_path, monkeypatch):
    entity_id, a, _a_sat, _b, calls, pc = await _setup(hass, monkeypatch, tmp_path)
    agent = _Agent()
    local, run = agent.patches()
    with local, run:
        res = await _say(hass, entity_id, "Nova, the house is cold.", a)
        await hass.async_block_till_done()
    assert calls["script"] == [("turn_on", {"entity_id": "script.heating_boost"})]
    assert _speech(res) == "Warming the house up."
    assert agent.calls == 0 and agent.local_calls == 0 and pc.get(a) is None
    assert calls["assist_satellite"] == [] and calls["tts"] == []


async def test_the_ai_picking_by_meaning_still_waits_for_a_yes(hass, tmp_path, monkeypatch):
    entity_id, a, _a_sat, _b, calls, pc = await _setup(hass, monkeypatch, tmp_path)
    agent = _Agent()
    local, run = agent.patches()
    with local, run:
        res = await _say(hass, entity_id, "it is nippy in here", a)
        assert agent.calls == 1 and _speech(res).endswith("Shall I go ahead?")
        assert calls["script"] == [] and pc.get(a).ref == PID
        await _say(hass, entity_id, "Yes.", a)
        await hass.async_block_till_done()
    assert calls["script"] == [("turn_on", {"entity_id": "script.heating_boost"})]


# ── 8.34.0: the answer after a Nova driven reopen keeps its conversation ────

class _Asker:
    """The agent, faked: it asks a question, then records the history it is
    given on the next turn."""

    def __init__(self):
        self.seen = []
        self.personas = []

    def patches(self):
        async def try_local(*a, **k):
            return SimpleNamespace(handled=False, text="")

        async def run_agent(hass, *a, **k):
            msgs = [m.get("content") for m in (k.get("messages") or [])]
            self.seen.append(msgs)
            self.personas.append(k.get("persona", ""))
            return "Shall I raise both to 22 degrees?"
        return (patch("custom_components.nova.local_engine.try_local", new=try_local),
                patch("custom_components.nova.agent.run_agent", new=run_agent))


async def _cast_setup(hass, monkeypatch, a_sat):
    from custom_components.nova import continued_conversation, conversation as nova_conv
    continued_conversation._REOPENED.clear()
    monkeypatch.setattr(continued_conversation, "schedule_reopen",
                        lambda hass, sat, spk, text, follow=True: True)
    monkeypatch.setattr(nova_conv.NovaAgent, "_satellite_pairings",
                        lambda self: {a_sat: "media_player.living"})
    monkeypatch.setattr(nova_conv, "resolve_tts_entity", lambda hass, eng: "tts.nova")

    async def announce(*a, **k):
        return None
    monkeypatch.setattr(nova_conv, "async_announce", announce)
    hass.states.async_set("media_player.living", "idle")
    return continued_conversation


async def test_the_answer_on_a_reopened_satellite_keeps_its_history(hass, tmp_path,
                                                                     monkeypatch):
    entity_id, a, a_sat, b, _calls, _pc = await _setup(
        hass, monkeypatch, tmp_path, continued_conversation_enabled=True)
    cc = await _cast_setup(hass, monkeypatch, a_sat)
    asker = _Asker()
    local, run = asker.patches()
    with local, run:
        await _say(hass, entity_id, "it's freezing", a, conversation_id="conv-1")
        assert cc._REOPENED.get(a, ("",))[0] == "conv-1"
        # HA's start_conversation gives the follow up a brand new id.
        await _say(hass, entity_id, "Yes.", a, conversation_id="conv-2")
    assert "it's freezing" in asker.seen[1]
    assert "Shall I raise both to 22 degrees?" in asker.seen[1]
    # conv-1 was used once; this reply asked again, so its own id is next.
    assert cc._REOPENED[a][0] == "conv-2"


async def test_after_sixty_seconds_or_from_another_device_there_is_no_history(
        hass, tmp_path, monkeypatch):
    entity_id, a, a_sat, b, _calls, _pc = await _setup(
        hass, monkeypatch, tmp_path, continued_conversation_enabled=True)
    cc = await _cast_setup(hass, monkeypatch, a_sat)
    asker = _Asker()
    local, run = asker.patches()
    with local, run:
        await _say(hass, entity_id, "it's freezing", a, conversation_id="conv-1")
        await _say(hass, entity_id, "Yes.", b, conversation_id="conv-b")
        assert "it's freezing" not in asker.seen[1]
        assert cc._REOPENED.get(a, ("",))[0] == "conv-1"       # B left it alone
        import time
        cc._REOPENED[a] = ("conv-1", time.monotonic() - 1)    # expired
        await _say(hass, entity_id, "Yes.", a, conversation_id="conv-3")
    assert "it's freezing" not in asker.seen[2]


async def test_voice_turns_are_told_to_keep_it_short(hass, tmp_path, monkeypatch):
    entity_id, a, _a_sat, _b, _calls, _pc = await _setup(hass, monkeypatch, tmp_path)
    from custom_components.nova.conversation import VOICE_REPLY_RULE
    asker = _Asker()
    local, run = asker.patches()
    with local, run:
        await _say(hass, entity_id, "it's freezing", a)
        await _say(hass, entity_id, "it's freezing", None, conversation_id="nova_chat_u1")
        await _say(hass, entity_id, "it's freezing", None, conversation_id="typed-1")
    assert VOICE_REPLY_RULE in asker.personas[0]
    assert VOICE_REPLY_RULE not in asker.personas[1]       # Chat
    assert VOICE_REPLY_RULE not in asker.personas[2]       # not a satellite


async def test_the_carried_history_is_not_replaced_by_the_cross_session_reseed(
        hass, tmp_path, monkeypatch):
    # With memory threading on, a never seen conversation id is reseeded from
    # older sessions, which REPLACES its history. The carried over turn must
    # survive that.
    entity_id, a, a_sat, _b, _calls, _pc = await _setup(
        hass, monkeypatch, tmp_path, continued_conversation_enabled=True,
        memory_threading_enabled=True)
    await _cast_setup(hass, monkeypatch, a_sat)
    from custom_components.nova import memory_thread

    async def older(hass, hours, limit, device_id=None, subject=None):
        return [{"role": "user", "content": "an older session", "ts": 1.0}]
    monkeypatch.setattr(memory_thread, "load_recent", older)
    asker = _Asker()
    local, run = asker.patches()
    with local, run:
        await _say(hass, entity_id, "it's freezing", a, conversation_id="conv-1")
        await _say(hass, entity_id, "Yes.", a, conversation_id="conv-2")
    assert "it's freezing" in asker.seen[1]
