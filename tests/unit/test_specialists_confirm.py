"""The Executive Assistant and Marketing specialists need a yes first (8.58.6).

They act in the world (Gmail, Calendar, posts), so the gate always asks:
a spoken yes from a voice satellite (held on the reply, replayed on the
next turn), a phone tap from Chat. Read only specialists are untouched.
"""
import json
import sys

import pytest

from test_authority import _VC

TOOLS = ["ask_executive_assistant", "ask_marketing_agent"]


@pytest.fixture
def pol(load, monkeypatch):
    p = load("policy")
    fake = _VC()
    fake.install(load("voice_confirm"), monkeypatch)
    fake.satellites.update({"satA", "satB"})
    return p, fake


@pytest.fixture
def pc(load):
    mod = load("pending_confirm")
    mod.clear_all()
    yield mod
    mod.clear_all()


@pytest.fixture
def spec(load):
    agent = load("agent")
    return sys.modules[agent._exec_ask_executive_assistant.__module__]


@pytest.fixture
def sent(spec, monkeypatch):
    seen = []

    async def fake(hass, path, message, timeout):
        seen.append((path, message))
        return json.dumps({"reply": "done"})
    monkeypatch.setattr(spec, "_ask_n8n_specialist", fake)
    return seen


def _run(spec, tool, hass, message, **kw):
    return getattr(spec, f"_exec_{tool}")(hass, {"message": message}, **kw)


@pytest.mark.parametrize("tool", TOOLS)
async def test_voice_is_held_for_a_spoken_yes(pol, pc, spec, sent, fake_hass, tool):
    res = json.loads(await _run(spec, tool, fake_hass, "email Sam the invoice",
                                device_id="satA", user_id="u1"))
    assert res["status"] == "awaiting_confirmation"
    assert res["ask_user"] == pc.ASK
    assert sent == []
    item = pc.get("satA")
    assert item.kind == pc.KIND_SPECIALIST and item.ref == tool
    assert item.data == "email Sam the invoice"


@pytest.mark.parametrize("tool", TOOLS)
async def test_a_spoken_yes_runs_it_once_with_the_same_words(pol, pc, spec, sent,
                                                              fake_hass, tool):
    await _run(spec, tool, fake_hass, "email Sam the invoice",
               device_id="satA", user_id="u1")
    out = await pc.answer(fake_hass, "satA", "yes", user_id="u1")
    assert out.success and "done" in out.text
    assert sent == [(spec.ACTING_SPECIALISTS[tool][1], "email Sam the invoice")]
    assert await pc.answer(fake_hass, "satA", "yes", user_id="u1") is None


async def test_a_no_or_other_words_run_nothing(pol, pc, spec, sent, fake_hass):
    await _run(spec, "ask_marketing_agent", fake_hass, "post it", device_id="satA")
    await pc.answer(fake_hass, "satA", "no")
    await _run(spec, "ask_marketing_agent", fake_hass, "post it", device_id="satA")
    assert await pc.answer(fake_hass, "satA", "what time is it") is None
    assert sent == []


async def test_a_yes_on_another_device_or_user_runs_nothing(pol, pc, spec, sent, fake_hass):
    await _run(spec, "ask_executive_assistant", fake_hass, "x", device_id="satA",
               user_id="u1")
    assert await pc.answer(fake_hass, "satB", "yes", user_id="u1") is None
    assert await pc.answer(fake_hass, "satA", "yes", user_id="u2") is None
    assert sent == []


async def test_an_old_held_request_does_not_run(pol, pc, spec, sent, fake_hass):
    pc.put("satA", pc.KIND_SPECIALIST, "ask_marketing_agent", "u1", now=0.0, data="x")
    assert pc.get("satA") is None
    assert await pc.answer(fake_hass, "satA", "yes", user_id="u1") is None


async def test_a_held_name_that_is_not_a_specialist_runs_nothing(pol, pc, spec, sent,
                                                                 fake_hass):
    pc.put("satA", pc.KIND_SPECIALIST, "ask_security_privacy_agent", "u1", data="x")
    out = await pc.answer(fake_hass, "satA", "yes", user_id="u1")
    assert not out.success and sent == []


@pytest.mark.parametrize("tool", TOOLS)
async def test_chat_needs_a_phone_tap(pol, pc, spec, sent, fake_hass, load, monkeypatch, tool):
    p, fake = pol
    vc = load("voice_confirm")
    asked = []

    async def phone(hass, question, timeout=None, services=None):
        asked.append(question)
        return "approved"
    monkeypatch.setattr(vc, "phone_services_for_user", lambda h, u: ["notify.mobile_app_x"])
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", phone)
    with p.chat_turn("nova_chat_u1", "u1"):
        res = json.loads(await _run(spec, tool, fake_hass, "send it", user_id="u1"))
    assert res == {"reply": "done"} and len(asked) == 1
    assert "Chat alone can't send, post or change things for you" in asked[0]


async def test_chat_without_a_phone_refuses(pol, spec, sent, fake_hass, load, monkeypatch):
    p, _ = pol
    monkeypatch.setattr(load("voice_confirm"), "phone_services_for_user", lambda h, u: [])
    with p.chat_turn("nova_chat_u1", "u1"):
        res = json.loads(await _run(spec, "ask_executive_assistant", fake_hass, "x",
                                    user_id="u1"))
    assert res["status"] == "awaiting_confirmation" and sent == []


async def test_a_declined_phone_tap_refuses(pol, spec, sent, fake_hass, load, monkeypatch):
    p, _ = pol
    vc = load("voice_confirm")

    async def phone(hass, question, timeout=None, services=None):
        return "rejected"
    monkeypatch.setattr(vc, "phone_services_for_user", lambda h, u: ["notify.x"])
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", phone)
    with p.chat_turn("nova_chat_u1", "u1"):
        res = json.loads(await _run(spec, "ask_marketing_agent", fake_hass, "x",
                                    user_id="u1"))
    assert res["status"] == "awaiting_confirmation" and sent == []


async def test_nova_never_does_it_on_its_own(pol, fake_hass):
    p, _ = pol
    d = await p.authorize(fake_hass, p.AuthorityRequest(
        "nova", "ask_marketing_agent", "", source=p.SOURCE_AUTOMATIC))
    assert not d.allowed and d.approval == "denied"


def test_rated_medium_and_always_asks(pol, fake_hass):
    p, _ = pol
    for tool in TOOLS:
        assert p.classify("nova", tool)[0] == "medium"
        assert p.requires_confirmation(fake_hass, "nova", tool)


def test_registry_says_they_mutate(load):
    agent = load("agent")
    registry = sys.modules[agent._exec_ask_executive_assistant.__module__
                           .replace("capabilities.specialists", "registry")]
    for tool in TOOLS:
        assert registry.TOOL_REGISTRY[tool].mutates is True
    for tool in ("ask_security_privacy_agent", "ask_homelab_infra_agent",
                 "ask_house_manager_agent"):
        assert registry.TOOL_REGISTRY[tool].mutates is False
