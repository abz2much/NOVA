"""The exact replies, log lines and audit calls of the mode and security
alert tools. The behaviour with the real policy gate and intrusion module is
in test_pin_safety_modes; here every dependency is a recorder."""
import json
import logging
import sys
import types

import pytest

from fakes import FakeHass


@pytest.fixture
def sm(load):
    return load("agent_runtime.capabilities.safety_modes")


def _stub(monkeypatch, name, **attrs):
    mod = types.ModuleType(f"jc.{name}")
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, f"jc.{name}", mod)
    return mod


@pytest.fixture
def env(monkeypatch, load):
    box = types.SimpleNamespace(log=[], audit=[], exec=[], approval=[], awaiting=[],
                                authorize_calls=[], decision=None, satellite=False,
                                entered=[], dismiss_calls=[], ack_calls=[],
                                mode_result={"ok": True, "mode": "movie"},
                                mode_calls=[], info={"description": "Dim and quiet"},
                                dismiss_error=None, ack_error=None)
    _stub(monkeypatch, "websocket", nova_log=lambda *a, **k: box.log.append(a))
    al = types.ModuleType("jc.action_log")
    al.new_request_id = lambda: "req-9"

    def start(request_id, action, source, **kw):
        box.audit.append((request_id, action, source, kw))
        return 31
    al.start = start
    al.set_execution = lambda aid, res, **kw: box.exec.append((aid, res, kw))
    al.set_approval = lambda aid, res, **kw: box.approval.append((aid, res, kw))
    al.mark_awaiting_approval = lambda aid, **kw: box.awaiting.append(aid)
    monkeypatch.setitem(sys.modules, "jc.action_log", al)

    vc = load("voice_confirm")
    monkeypatch.setattr(vc, "is_voice_satellite_device",
                        lambda hass, device_id: box.satellite and device_id == "sat")

    def set_mode(mode, reason):
        box.mode_calls.append((mode, reason))
        return box.mode_result
    _stub(monkeypatch, "modes", set_mode=set_mode, mode_info=lambda: box.info)

    async def apply_mode_entry(hass, mode, **kw):
        box.entered.append((mode, kw))
    _stub(monkeypatch, "mode_scene", apply_mode_entry=apply_mode_entry)

    def dismiss(reason=""):
        box.dismiss_calls.append(reason)
        if box.dismiss_error:
            raise box.dismiss_error
        return {"recorded": {"reason": reason}}

    def acknowledge(reason=""):
        box.ack_calls.append(reason)
        if box.ack_error:
            raise box.ack_error
        return {"acknowledged": True}
    _stub(monkeypatch, "intrusion", dismiss_intrusion=dismiss, acknowledge=acknowledge)

    core = types.SimpleNamespace(safety_mgr=types.SimpleNamespace(_investigation={"a": 1}))
    _stub(monkeypatch, "cognitive_core", _CORE=core)
    box.core = core

    class Request:
        def __init__(self, *args, **kw):
            self.args, self.kw = args, kw

    async def authorize(hass, request):
        box.authorize_calls.append(request)
        return box.decision
    pol = load("policy")
    monkeypatch.setattr(pol, "AuthorityRequest", Request)
    monkeypatch.setattr(pol, "authorize", authorize)
    box.decision = types.SimpleNamespace(allowed=True, note="", approval="approved")
    return box


# ── the audit source ─────────────────────────────────────────────────────────

def test_a_voice_satellite_is_a_voice_source_and_everything_else_is_chat(sm, env):
    env.satellite = True
    assert sm._source(None, "sat") == "voice"
    assert sm._source(None, "phone") == "chat"
    assert sm._source(None, None) == "chat"


def test_a_failing_satellite_lookup_means_chat(sm, monkeypatch, load):
    def boom(hass, device_id):
        raise RuntimeError("no registry")
    monkeypatch.setattr(load("voice_confirm"), "is_voice_satellite_device", boom)
    assert sm._source(None, "sat") == "chat"


# ── set_mode ─────────────────────────────────────────────────────────────────

async def test_set_mode_replies_and_logs(sm, env):
    out = await sm._exec_set_mode(FakeHass(), {"mode": "movie", "reason": "film night"},
                                  device_id="phone", user_id="u1")
    assert json.loads(out) == {
        "ok": True, "mode": "movie", "description": "Dim and quiet",
        "note": "safety (pipe-freeze, intrusion, lockdown) remains fully active in every mode"}
    assert list(json.loads(out)) == ["ok", "mode", "description", "note"]
    assert env.mode_calls == [("movie", "film night")]
    assert env.entered == [("movie", {"source": "chat", "requested_by_user_id": "u1"})]
    assert env.log == [("MODE", "mode → movie (film night)")]


async def test_set_mode_without_a_reason_logs_just_the_mode(sm, env):
    await sm._exec_set_mode(FakeHass(), {"mode": "movie"})
    assert env.mode_calls == [("movie", "")]
    assert env.log == [("MODE", "mode → movie")]
    assert env.entered == [("movie", {"source": "chat", "requested_by_user_id": None})]


async def test_set_mode_from_a_satellite_is_a_voice_source(sm, env):
    env.satellite = True
    await sm._exec_set_mode(FakeHass(), {"mode": "movie"}, device_id="sat", user_id="")
    assert env.entered[0][1] == {"source": "voice", "requested_by_user_id": None}


async def test_set_mode_with_no_mode_asks_for_blank(sm, env):
    await sm._exec_set_mode(FakeHass(), {})
    assert env.mode_calls == [("", "")]


async def test_a_refused_mode_is_returned_as_it_is(sm, env):
    env.mode_result = {"ok": False, "error": "unknown mode", "available": ["normal"]}
    out = await sm._exec_set_mode(FakeHass(), {"mode": "disco"})
    assert json.loads(out) == env.mode_result
    assert env.entered == [] and env.log == []


async def test_the_scene_and_the_log_failing_do_not_change_the_reply(sm, env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("scene")
    _stub(monkeypatch, "mode_scene", apply_mode_entry=boom)
    _stub(monkeypatch, "websocket", nova_log=lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("log")))
    out = await sm._exec_set_mode(FakeHass(), {"mode": "movie"})
    assert json.loads(out)["ok"] is True


async def test_the_mode_description_is_blank_when_there_is_none(sm, env):
    env.info = {}
    out = await sm._exec_set_mode(FakeHass(), {"mode": "movie"})
    assert json.loads(out)["description"] == ""


async def test_a_failing_mode_switch_is_a_safe_error(sm, env):
    def boom(mode, reason):
        raise RuntimeError("disk")
    sys.modules["jc.modes"].set_mode = boom
    out = await sm._exec_set_mode(FakeHass(), {"mode": "movie"})
    assert json.loads(out) == {"error": "RuntimeError (details are in the Home Assistant log)"}


# ── acknowledge_alert ────────────────────────────────────────────────────────

async def test_acknowledge_audits_then_replies(sm, env):
    out = await sm._exec_acknowledge_alert(FakeHass(), {"reason": "on my way"},
                                           device_id="phone", user_id="u1")
    assert json.loads(out) == {
        "ok": True, "acknowledged": True,
        "message": "Acknowledged — holding the automatic alert. "
                   "I'll still escalate if I see a person on camera."}
    assert list(json.loads(out)) == ["ok", "acknowledged", "message"]
    assert env.audit == [("req-9", "acknowledge_alert", "chat", {
        "requested_by_user_id": "u1", "request_device_id": "phone",
        "domain": "nova", "service": "acknowledge"})]
    assert env.exec == [(31, "accepted", {})]
    assert env.ack_calls == ["on my way"]


async def test_acknowledge_without_identity_audits_none(sm, env):
    await sm._exec_acknowledge_alert(FakeHass(), {})
    assert env.audit[0][3]["requested_by_user_id"] is None
    assert env.audit[0][3]["request_device_id"] is None
    assert env.ack_calls == [""]


async def test_a_failed_acknowledge_is_audited_and_safe(sm, env):
    env.ack_error = RuntimeError("secret")
    out = await sm._exec_acknowledge_alert(FakeHass(), {})
    assert json.loads(out) == {"error": "RuntimeError (details are in the Home Assistant log)"}
    assert env.exec == [(31, "failed", {"reason_code": "acknowledge_failed"})]


# ── dismiss_intrusion ────────────────────────────────────────────────────────

async def test_dismiss_with_no_requester_is_blocked_before_anyone_is_asked(sm, env):
    out = await sm._exec_dismiss_intrusion(FakeHass(), {"reason": "cat"})
    assert json.loads(out) == {
        "status": "blocked",
        "message": "I can't call off a security alert without knowing who is asking. "
                   "Please ask from the Nova app or a voice satellite."}
    assert env.exec == [(31, "blocked", {"reason_code": "no_requester"})]
    assert env.awaiting == [] and env.authorize_calls == [] and env.dismiss_calls == []


async def test_dismiss_asks_the_policy_gate_with_this_request(sm, env):
    await sm._exec_dismiss_intrusion(FakeHass(), {"reason": "cat"},
                                     device_id="phone", user_id="u1")
    (request,) = env.authorize_calls
    assert request.args == ("nova", "dismiss_intrusion", "")
    assert request.kw == {"device_id": "phone", "user_id": "u1", "label": "call off",
                          "target_name": "the intrusion alert"}
    assert env.awaiting == [31]
    assert env.audit[0][3] == {"requested_by_user_id": "u1", "request_device_id": "phone",
                               "domain": "nova", "service": "dismiss_intrusion"}


async def test_an_approved_dismiss_stands_down_and_is_audited(sm, env, caplog):
    out = await sm._exec_dismiss_intrusion(FakeHass(), {"reason": "it was the cat"},
                                           user_id="u1")
    assert json.loads(out) == {"ok": True, "recorded": {"reason": "it was the cat"},
                               "message": "Intrusion called off. Standing down."}
    assert env.core.safety_mgr._investigation is None
    assert env.log == [("SAFETY", "Intrusion called off by user (false alarm): it was the cat")]
    assert env.approval == [(31, "approved", {"approval_required": True})]
    assert env.exec == [(31, "accepted", {})]
    assert env.authorize_calls[0].kw["device_id"] == ""


async def test_a_dismiss_with_no_reason_logs_without_one(sm, env):
    await sm._exec_dismiss_intrusion(FakeHass(), {}, device_id="phone")
    assert env.log == [("SAFETY", "Intrusion called off by user (false alarm)")]
    assert env.dismiss_calls == [""]


async def test_a_gate_that_needs_no_approval_is_recorded_as_not_required(sm, env):
    env.decision = types.SimpleNamespace(allowed=True, note="", approval="not_required")
    await sm._exec_dismiss_intrusion(FakeHass(), {}, user_id="u1")
    assert env.approval == [(31, "not_required", {"approval_required": False})]


async def test_a_dismiss_not_approved_says_so_and_changes_nothing(sm, env):
    env.decision = types.SimpleNamespace(allowed=False, note="Tap the phone.",
                                         approval="rejected")
    out = await sm._exec_dismiss_intrusion(FakeHass(), {}, user_id="u1")
    assert json.loads(out) == {"status": "awaiting_confirmation", "message": "Tap the phone."}
    assert env.exec == [(31, "blocked", {"reason_code": "confirmation_not_approved"})]
    assert env.dismiss_calls == [] and env.core.safety_mgr._investigation == {"a": 1}


async def test_a_dismiss_not_approved_with_no_note_uses_the_default_words(sm, env):
    env.decision = types.SimpleNamespace(allowed=False, note="", approval="expired")
    out = await sm._exec_dismiss_intrusion(FakeHass(), {}, user_id="u1")
    assert json.loads(out)["message"] == \
        "Confirmation is required before I call off the alert."


async def test_a_dismiss_that_fails_is_audited_and_keeps_the_investigation(sm, env):
    env.dismiss_error = RuntimeError("secret")
    out = await sm._exec_dismiss_intrusion(FakeHass(), {}, user_id="u1")
    assert json.loads(out) == {"error": "RuntimeError (details are in the Home Assistant log)"}
    assert env.exec == [(31, "failed", {"reason_code": "dismiss_failed"})]
    assert env.core.safety_mgr._investigation == {"a": 1}


async def test_a_missing_core_or_manager_does_not_stop_the_dismiss(sm, env, monkeypatch):
    _stub(monkeypatch, "cognitive_core", _CORE=None)
    out = await sm._exec_dismiss_intrusion(FakeHass(), {}, user_id="u1")
    assert json.loads(out)["ok"] is True
    _stub(monkeypatch, "cognitive_core", _CORE=types.SimpleNamespace(safety_mgr=None))
    out = await sm._exec_dismiss_intrusion(FakeHass(), {}, user_id="u1")
    assert json.loads(out)["ok"] is True


async def test_a_failing_log_does_not_stop_the_dismiss(sm, env, monkeypatch):
    _stub(monkeypatch, "websocket", nova_log=lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("log")))
    out = await sm._exec_dismiss_intrusion(FakeHass(), {}, user_id="u1")
    assert json.loads(out)["ok"] is True and env.exec == [(31, "accepted", {})]


def test_the_agent_logger_keeps_the_name_it_always_had(sm):
    assert sm._LOGGER.name == "jc.agent"
