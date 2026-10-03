"""First run "Nova is ready" notification (welcome.py).

build_message is pure over a run_setup_health() result; async_maybe_show is
exercised against the fake hass with nova_config and setup_health stubbed, to
prove it only fires for a fresh install, only once, and never raises.
"""
from __future__ import annotations

import sys
import types

import pytest

if "aiohttp" not in sys.modules:
    _aiohttp = types.ModuleType("aiohttp")
    _aiohttp.ClientTimeout = lambda **kw: None
    _aiohttp.ClientSession = object
    sys.modules["aiohttp"] = _aiohttp


@pytest.fixture
def welcome(load):
    return load("welcome")


@pytest.fixture
def nova_config(load):
    return load("nova_config")


@pytest.fixture
def sh(load):
    return load("setup_health")


def _result(*statuses):
    return {"checks": [
        {"name": f"Check {i}", "key": f"c{i}", "status": st, "detail": f"detail {i}",
         **({"suggested_fix": f"fix {i}"} if st in ("warn", "down") else {})}
        for i, st in enumerate(statuses)]}


# ── build_message ────────────────────────────────────────────────────────────

def test_message_all_passed(welcome):
    title, msg = welcome.build_message(_result("ok", "ok", "off"))
    assert title == "Nova is ready"
    assert "All 2 checks passed." in msg
    assert "Needs attention" not in msg


def test_message_lists_only_problems_with_fix(welcome):
    _, msg = welcome.build_message(_result("ok", "warn", "down", "off"))
    assert "1 checks passed, 2 need attention." in msg
    assert "**Check 1**: detail 1 Fix: fix 1" in msg
    assert "**Check 2**: detail 2 Fix: fix 2" in msg
    assert "Check 0" not in msg and "Check 3" not in msg


def test_message_nothing_active(welcome):
    _, msg = welcome.build_message({"checks": []})
    assert msg.startswith("Setup finished.\n")
    assert "Setup Doctor" in msg


# ── async_maybe_show ─────────────────────────────────────────────────────────

def _patch(monkeypatch, nova_config, sh, values: dict, result=None):
    store = dict(values)
    monkeypatch.setattr(nova_config, "get", lambda k, d=None: store.get(k, d))

    def _set(k, v):
        store[k] = v
        return True
    monkeypatch.setattr(nova_config, "set", _set)

    async def _run(hass):
        return result or _result("ok")
    monkeypatch.setattr(sh, "run_setup_health", _run)
    return store


async def test_shows_once_for_fresh_install(welcome, nova_config, sh, fake_hass, monkeypatch):
    store = _patch(monkeypatch, nova_config, sh, {"welcome_pending": True})
    assert await welcome.async_maybe_show(fake_hass) is True
    assert store["welcome_shown"] is True
    (domain, service, data), = fake_hass.service_calls
    assert (domain, service) == ("persistent_notification", "create")
    assert data["notification_id"] == "nova_welcome"
    assert data["title"] == "Nova is ready"

    assert await welcome.async_maybe_show(fake_hass) is False
    assert len(fake_hass.service_calls) == 1


async def test_existing_install_never_sees_it(welcome, nova_config, sh, fake_hass, monkeypatch):
    _patch(monkeypatch, nova_config, sh, {})
    assert await welcome.async_maybe_show(fake_hass) is False
    assert fake_hass.service_calls == []


async def test_never_raises(welcome, nova_config, sh, fake_hass, monkeypatch):
    _patch(monkeypatch, nova_config, sh, {"welcome_pending": True})

    async def _boom(hass):
        raise RuntimeError("doctor failed")
    monkeypatch.setattr(sh, "run_setup_health", _boom)
    assert await welcome.async_maybe_show(fake_hass) is False


def test_bootstrap_runs_welcome_after_pipeline_repair(load):
    import inspect
    src = inspect.getsource(load("bootstrap").schedule_bootstrap)
    assert src.index("async_ensure_pipeline_agent") < src.index("welcome.async_maybe_show")


# ── async_say_hello ──────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, d):
        self._d = d

    def as_dict(self):
        return self._d


def _fake_conversation(monkeypatch, response=None, exc=None):
    calls = []

    async def async_converse(hass, text, conversation_id, context, agent_id=None, **kw):
        calls.append((text, conversation_id, context, agent_id))
        if exc:
            raise exc
        return types.SimpleNamespace(response=_Resp(response or {}))

    conv = types.ModuleType("homeassistant.components.conversation")
    conv.async_converse = async_converse
    comps = sys.modules.get("homeassistant.components") or types.ModuleType("homeassistant.components")
    monkeypatch.setitem(sys.modules, "homeassistant.components", comps)
    monkeypatch.setitem(sys.modules, "homeassistant.components.conversation", conv)
    monkeypatch.setattr(comps, "conversation", conv, raising=False)
    return calls


@pytest.fixture
def bootstrap(load):
    return load("bootstrap")


async def test_say_hello_returns_reply_and_sends_fixed_text(welcome, bootstrap, fake_hass, monkeypatch):
    monkeypatch.setattr(bootstrap, "_find_nova_agent", lambda h: "conversation.nova")
    calls = _fake_conversation(monkeypatch, {
        "response_type": "action_done", "speech": {"plain": {"speech": "Good day."}}})
    res = await welcome.async_say_hello(fake_hass, "ctx")
    assert res == {"ok": True, "reply": "Good day."}
    assert calls == [("Hello", None, "ctx", "conversation.nova")]


async def test_say_hello_reports_agent_error(welcome, bootstrap, fake_hass, monkeypatch):
    monkeypatch.setattr(bootstrap, "_find_nova_agent", lambda h: "conversation.nova")
    _fake_conversation(monkeypatch, {
        "response_type": "error", "speech": {"plain": {"speech": "LLM unreachable"}}})
    res = await welcome.async_say_hello(fake_hass, None)
    assert res == {"ok": False, "error": "LLM unreachable"}


async def test_say_hello_without_agent(welcome, bootstrap, fake_hass, monkeypatch):
    monkeypatch.setattr(bootstrap, "_find_nova_agent", lambda h: None)
    calls = _fake_conversation(monkeypatch)
    res = await welcome.async_say_hello(fake_hass, None)
    assert res["ok"] is False and "not found" in res["error"]
    assert calls == []


async def test_say_hello_never_raises(welcome, bootstrap, fake_hass, monkeypatch):
    monkeypatch.setattr(bootstrap, "_find_nova_agent", lambda h: "conversation.nova")
    _fake_conversation(monkeypatch, exc=RuntimeError("boom"))
    res = await welcome.async_say_hello(fake_hass, None)
    assert res["ok"] is False and "boom" in res["error"]


def test_onboarding_voice_step_uses_setup_doctor_pipeline_check():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "custom_components/nova/websocket.py").read_text()
    fn = src[src.index("def _get_onboarding_state("):src.index("def _get_cameras(")]
    assert "_check_assist_pipeline(hass)" in fn
    assert "assist_satellite" not in fn
    assert '"fresh": fresh' in fn
    assert "talk to it in chat" not in fn
