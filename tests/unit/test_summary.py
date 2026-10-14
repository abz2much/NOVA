"""Fresh behaviour tests for summary.py: the conversation_summary service
body. History, storage, the system prompt builder, the announcer, the
language block and the provider boundary are all replaced with recorders."""
from __future__ import annotations

import sys
import types

import pytest


class _Reply:
    def __init__(self, text):
        self.text = text


@pytest.fixture
def env(load, monkeypatch):
    s = load("summary")
    lang = load("output_language")
    e = types.SimpleNamespace(mod=s, history=[], reads=[], stored=[], spoken=[],
                              chats=[], prompts=[], reply="  A quiet day overall.  ",
                              chat_exc=None)

    def get_recent(**kw):
        e.reads.append(kw)
        return list(e.history)

    def save(*args, **kw):
        e.stored.append(args)

    def build(hass, honorific, task):
        e.prompts.append((honorific, task))
        return "SYSTEM"

    async def announce(hass, text, tts, speakers, context=None):
        e.spoken.append((text, tts, list(speakers), context))

    async def execute_chat(hass, client, messages, **kw):
        e.chats.append((client, messages, kw))
        if e.chat_exc:
            raise e.chat_exc
        return _Reply(e.reply)

    activity = types.ModuleType("jc.providers.activity")
    activity.execute_chat = execute_chat
    monkeypatch.setattr(s, "get_recent_messages", get_recent)
    monkeypatch.setattr(s, "save_message", save)
    monkeypatch.setattr(s, "build_system_prompt", build)
    monkeypatch.setattr(s, "async_announce", announce)
    monkeypatch.setattr(lang, "with_language", lambda hass, system: system + "+LANG")
    monkeypatch.setitem(sys.modules, "jc.providers.activity", activity)
    return e


def _call(**data):
    return types.SimpleNamespace(data=data)


MSGS = [
    {"timestamp": "2026-02-03T07:45:12.123", "role": "user", "content": "morning"},
    {"timestamp": "2026-02-03T07:45:15", "role": "assistant", "content": "Good morning."},
    {"timestamp": "2026-02-03 09:00:00", "role": "system", "content": "note"},
]


# ── _period ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hours, text", [
    (-3, "hour"), (0, "hour"), (1, "hour"), (2, "2 hours"), (24, "24 hours"),
    (25, "25 hours"), (47, "47 hours"), (48, "2 days"), (71, "71 hours"), (168, "7 days")])
def test_period(load, hours, text):
    assert load("summary")._period(hours) == text


# ── quiet period ────────────────────────────────────────────────────────────

async def test_nothing_to_report_with_honorific(env, fake_hass):
    res = await env.mod.async_summarise(fake_hass, _call(), "client", "captain",
                                        "tts.voice", ["media_player.a"])
    text = "Nothing to report, captain. The last 24 hours appear to have been remarkably quiet."
    assert res == {"success": True, "summary": text, "message_count": 0}
    assert env.spoken == [(text, "tts.voice", ["media_player.a"], "summary")]
    assert env.chats == [] and env.stored == []


async def test_nothing_to_report_without_honorific(env, fake_hass):
    res = await env.mod.async_summarise(fake_hass, _call(hours=1, announce=False),
                                        None, "", None, [])
    assert res["summary"] == ("Nothing to report. The last hour appears to have been "
                              "remarkably quiet.")
    assert env.spoken == []


async def test_history_query_arguments(env, fake_hass):
    await env.mod.async_summarise(fake_hass, _call(hours="6", device_id="dev9"),
                                  None, "x", None, [])
    assert env.reads == [{"hours": 6, "device_id": "dev9", "limit": 300}]
    await env.mod.async_summarise(fake_hass, _call(), None, "x", None, [])
    assert env.reads[-1] == {"hours": 24, "device_id": None, "limit": 300}


async def test_bad_hours_raises(env, fake_hass):
    with pytest.raises(ValueError):
        await env.mod.async_summarise(fake_hass, _call(hours="lots"), None, "x", None, [])


async def test_history_is_read_in_the_executor(env, fake_hass, monkeypatch):
    jobs = []
    real = fake_hass.async_add_executor_job

    async def spy(func, *args):
        jobs.append((func, args))
        return await real(func, *args)
    monkeypatch.setattr(fake_hass, "async_add_executor_job", spy)
    env.history = MSGS
    await env.mod.async_summarise(fake_hass, _call(hours=3), None, "x", None, [])
    assert len(jobs) == 2
    assert jobs[1][0] is env.mod.save_message


# ── with history ────────────────────────────────────────────────────────────

async def test_full_summary_flow(env, fake_hass):
    env.history = MSGS
    res = await env.mod.async_summarise(fake_hass, _call(hours=12), "the-client",
                                        "captain", "tts.voice", ["media_player.a", "media_player.b"])
    assert res == {"success": True, "summary": "A quiet day overall.",
                   "message_count": 3, "period_hours": 12}
    client, messages, kw = env.chats[0]
    assert client == "the-client"
    assert messages[0] == {"role": "system", "content": "SYSTEM+LANG"}
    assert messages[1] == {"role": "user", "content": (
        "Transcript:\n\n"
        "[2026-02-03 07:45] You: morning\n"
        "[2026-02-03 07:45] Nova: Good morning.\n"
        "[2026-02-03 09:00] Nova: note")}
    assert kw == {"role": "summary", "data_category": "text",
                  "max_tokens": 300, "temperature": 0.7}
    assert env.stored == [("assistant", "[Summary — 12 hours] A quiet day overall.", "summary")]
    assert env.spoken == [("A quiet day overall.", "tts.voice",
                           ["media_player.a", "media_player.b"], "summary")]


async def test_task_wording(env, fake_hass):
    env.history = MSGS
    await env.mod.async_summarise(fake_hass, _call(hours=48), None, "captain", None, [])
    honorific, task = env.prompts[0]
    assert honorific == "captain"
    assert "past 2 days" in task and "to captain" in task
    assert "3–5 sentences" in task
    await env.mod.async_summarise(fake_hass, _call(), None, "", None, [])
    assert "to the household" in env.prompts[1][1]
    assert "past 24 hours" in env.prompts[1][1]


async def test_store_and_announce_switches(env, fake_hass):
    env.history = MSGS
    await env.mod.async_summarise(fake_hass, _call(store=False, announce=False),
                                  None, "x", None, [])
    assert env.stored == [] and env.spoken == []
    await env.mod.async_summarise(fake_hass, _call(store=0, announce=1), None, "x", None, [])
    assert env.stored == [] and len(env.spoken) == 1


async def test_store_happens_before_announce(env, fake_hass, monkeypatch):
    order = []
    env.history = MSGS
    monkeypatch.setattr(env.mod, "save_message", lambda *a: order.append("store"))

    async def announce(*a, **k):
        order.append("speak")
    monkeypatch.setattr(env.mod, "async_announce", announce)
    await env.mod.async_summarise(fake_hass, _call(), None, "x", None, [])
    assert order == ["store", "speak"]


async def test_model_failure(env, fake_hass):
    env.history = MSGS
    env.chat_exc = RuntimeError("provider unreachable")
    res = await env.mod.async_summarise(fake_hass, _call(), None, "x", "tts.voice", [])
    assert res == {"success": False, "error": "provider unreachable"}
    assert env.stored == [] and env.spoken == []


async def test_reply_without_text_is_a_failure(env, fake_hass, monkeypatch):
    env.history = MSGS

    async def odd(hass, client, messages, **kw):
        return {"text": "dict, not an object"}
    sys.modules["jc.providers.activity"].execute_chat = odd
    res = await env.mod.async_summarise(fake_hass, _call(), None, "x", None, [])
    assert res["success"] is False and "text" in res["error"]


async def test_language_block_receives_the_built_prompt(env, fake_hass, load, monkeypatch):
    seen = []
    monkeypatch.setattr(load("output_language"), "with_language",
                        lambda hass, system: seen.append((hass, system)) or "FINAL")
    env.history = MSGS
    await env.mod.async_summarise(fake_hass, _call(), None, "x", None, [])
    assert seen == [(fake_hass, "SYSTEM")]
    assert env.chats[0][1][0]["content"] == "FINAL"


def test_legacy_constant(load):
    assert load("summary").SUMMARY_MODEL == "openai/gpt-oss-120b"


# ── 8.49.22 regressions ──────────────────────────────────────────────────────

async def test_a_quiet_hour_reads_as_one_hour(env, fake_hass):
    """Bug 8: "The last hour appears", not "appear"."""
    res = await env.mod.async_summarise(fake_hass, _call(hours=1, announce=False),
                                        None, "", None, [])
    assert "The last hour appears to have been remarkably quiet." in res["summary"]


def test_a_part_day_span_is_not_rounded_down_to_days(load):
    """Bug 9: 47 hours is "47 hours", never "1 day"."""
    period = load("summary")._period
    assert period(47) == "47 hours" and period(25) == "25 hours"
    assert period(48) == "2 days"
