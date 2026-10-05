"""Pin what the agent's security-alert and mode tools do today (8.7.19, tests only).

agent_runtime/capabilities/safety_modes.py holds the three tools that change
Nova's safety posture from a conversation: dismiss_intrusion (stands down the
intrusion response), acknowledge_alert (holds the no-response escalation) and
set_mode. These tests run the real policy gate and the real intrusion module;
only the confirmation transport (the phone or speaker question) and the
satellite lookup are faked. Tests named test_current_behaviour_* pin
behaviour that looks wrong; each says why in a comment.
"""
from __future__ import annotations

import json
import types

import pytest

from fakes import FakeHass

PHONE = "device-phone"
SATELLITE = "device-kitchen-satellite"


@pytest.fixture
def sm(load):
    return load("agent_runtime.capabilities.safety_modes")


@pytest.fixture
def intrusion(load):
    mod = load("intrusion")
    mod.clear_calloff()
    yield mod
    mod.clear_calloff()


@pytest.fixture
def audit(load, monkeypatch):
    """action_log writes recorded instead of written to SQLite."""
    al = load("action_log")
    rows = {"start": [], "execution": [], "approval": [], "awaiting": []}

    def start(request_id, action, source, **kw):
        rows["start"].append({"action": action, "source": source, **kw})
        return len(rows["start"])

    monkeypatch.setattr(al, "new_request_id", lambda: "req-1")
    monkeypatch.setattr(al, "start", start)
    monkeypatch.setattr(al, "set_execution",
                        lambda aid, res, **kw: rows["execution"].append((aid, res, kw.get("reason_code"))))
    monkeypatch.setattr(al, "set_approval",
                        lambda aid, res, **kw: rows["approval"].append((aid, res)))
    monkeypatch.setattr(al, "mark_awaiting_approval", lambda aid, **kw: rows["awaiting"].append(aid))
    return rows


@pytest.fixture
def confirm(load, monkeypatch):
    """The confirmation transport behind the real policy gate. `answer` is what
    the person says on the speaker; `phone` what they tap. `asked` records
    which channel was used, so a test can tell a spoken yes from a phone tap."""
    vc = load("voice_confirm")
    state = {"answer": "approved", "phone": "approved", "asked": []}

    async def confirm_typed(hass, question, *, entity_id="", timeout=0):
        state["asked"].append(("speaker", question))
        return state["answer"]

    async def phone_only(hass, question, *, timeout=0):
        state["asked"].append(("phone", question))
        return state["phone"]

    monkeypatch.setattr(vc, "confirm_typed", confirm_typed)
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", phone_only)
    monkeypatch.setattr(vc, "is_voice_satellite_device",
                        lambda hass, device_id, strict=False: device_id == SATELLITE)
    return state


@pytest.fixture
def core(load):
    """A live intrusion investigation on the cognitive core's SafetyManager."""
    cc = load("cognitive_core")
    saved = cc._CORE.safety_mgr
    cc._CORE.safety_mgr = types.SimpleNamespace(_investigation={"start": 1.0, "zones": {"hall"}})
    yield cc._CORE
    cc._CORE.safety_mgr = saved


@pytest.fixture
def modes(load, monkeypatch, tmp_path):
    m = load("modes")
    monkeypatch.setattr(m, "MODE_STATE_PATH", str(tmp_path / "mode_state.json"))
    monkeypatch.setattr(m, "_state", {"mode": "normal", "since": 0.0, "reason": ""})
    monkeypatch.setattr(m, "_loaded", True)
    return m


@pytest.fixture
def scenes(load, monkeypatch):
    ms = load("mode_scene")
    entered = []

    async def apply_mode_entry(hass, mode, **kw):
        entered.append((mode, kw))
    monkeypatch.setattr(ms, "apply_mode_entry", apply_mode_entry)
    return entered


# ── dismiss_intrusion: stands down Nova's security response ─────────────────

async def test_dismiss_with_no_requester_is_blocked_and_the_response_keeps_running(
        sm, intrusion, audit, confirm, core):
    res = json.loads(await sm._exec_dismiss_intrusion(FakeHass(), {"reason": "cat"}))
    assert res["status"] == "blocked"
    assert intrusion.is_called_off() is False
    assert core.safety_mgr._investigation is not None
    assert audit["execution"] == [(1, "blocked", "no_requester")]
    assert confirm["asked"] == []                     # nobody to ask


async def test_dismiss_confirmed_on_the_phone_stands_down(sm, intrusion, audit, confirm, core):
    res = json.loads(await sm._exec_dismiss_intrusion(
        FakeHass(), {"reason": "it was the cat"}, device_id=PHONE, user_id="u1"))
    assert res["ok"] is True and res["message"] == "Intrusion called off. Standing down."
    assert res["recorded"]["reason"] == "it was the cat"
    assert intrusion.is_called_off() is True
    assert core.safety_mgr._investigation is None
    assert audit["start"][0]["action"] == "dismiss_intrusion"
    assert audit["start"][0]["requested_by_user_id"] == "u1"
    assert audit["start"][0]["request_device_id"] == PHONE
    assert audit["approval"] == [(1, "approved")]
    assert audit["execution"] == [(1, "accepted", None)]
    # Always confirmed, even with voice confirmation switched off (the default).
    assert [c for c, _q in confirm["asked"]] == ["speaker"]


@pytest.mark.parametrize("answer", ["rejected", "expired", "deferred", "error"])
async def test_dismiss_not_confirmed_leaves_the_response_running(
        sm, intrusion, audit, confirm, core, answer):
    confirm["answer"] = answer
    res = json.loads(await sm._exec_dismiss_intrusion(FakeHass(), {}, user_id="u1"))
    assert res["status"] == "awaiting_confirmation"
    assert intrusion.is_called_off() is False
    assert core.safety_mgr._investigation is not None
    assert audit["approval"] == [(1, answer)]
    assert audit["execution"] == [(1, "blocked", "confirmation_not_approved")]


async def test_dismiss_by_voice_needs_a_phone_tap_and_a_spoken_yes_is_not_enough(
        sm, intrusion, audit, confirm, core):
    confirm["answer"] = "approved"        # a spoken yes would be given...
    confirm["phone"] = "expired"          # ...but nobody taps the phone
    res = json.loads(await sm._exec_dismiss_intrusion(FakeHass(), {}, device_id=SATELLITE))
    assert res["status"] == "awaiting_confirmation"
    assert "voice alone can't authorize" in res["message"]
    assert [c for c, _q in confirm["asked"]] == ["phone"]
    assert intrusion.is_called_off() is False and core.safety_mgr._investigation is not None
    assert audit["start"][0]["source"] == "voice"

    confirm["phone"] = "approved"
    res = json.loads(await sm._exec_dismiss_intrusion(FakeHass(), {}, device_id=SATELLITE))
    assert res["ok"] is True and intrusion.is_called_off() is True


async def test_a_dismiss_that_errors_is_audited_as_failed_and_keeps_the_investigation(
        sm, intrusion, audit, confirm, core, monkeypatch):
    def boom(reason=""):
        raise RuntimeError("disk full")
    monkeypatch.setattr(intrusion, "dismiss_intrusion", boom)
    res = json.loads(await sm._exec_dismiss_intrusion(FakeHass(), {}, user_id="u1"))
    assert res == {"error": "disk full"}
    assert core.safety_mgr._investigation is not None
    assert audit["execution"] == [(1, "failed", "dismiss_failed")]


# ── acknowledge_alert: holds the no-response escalation, never stands down ──

async def test_acknowledge_holds_escalation_without_calling_off(sm, intrusion, audit, confirm, core):
    res = json.loads(await sm._exec_acknowledge_alert(
        FakeHass(), {"reason": "looking now"}, device_id=SATELLITE, user_id="u2"))
    assert res["ok"] is True and "still escalate" in res["message"]
    assert intrusion.is_acknowledged() is True
    assert intrusion.is_called_off() is False
    assert core.safety_mgr._investigation is not None     # the investigation keeps running
    assert confirm["asked"] == []                         # not confirmation gated
    assert audit["start"][0] | {} == {
        "action": "acknowledge_alert", "source": "voice", "requested_by_user_id": "u2",
        "request_device_id": SATELLITE, "domain": "nova", "service": "acknowledge"}
    assert audit["execution"] == [(1, "accepted", None)]


async def test_a_failed_acknowledge_is_audited_as_failed(sm, intrusion, audit, confirm, monkeypatch):
    monkeypatch.setattr(intrusion, "acknowledge",
                        lambda reason="": (_ for _ in ()).throw(RuntimeError("x")))
    res = json.loads(await sm._exec_acknowledge_alert(FakeHass(), {}))
    assert res == {"error": "x"} and intrusion.is_acknowledged() is False
    assert audit["start"][0]["source"] == "chat"
    assert audit["execution"] == [(1, "failed", "acknowledge_failed")]


# ── set_mode ────────────────────────────────────────────────────────────────

async def test_set_mode_switches_and_applies_the_mode_scene(sm, modes, scenes, confirm):
    res = json.loads(await sm._exec_set_mode(
        FakeHass(), {"mode": "Party", "reason": "friends over"}, device_id=SATELLITE,
        user_id="u3"))
    assert res["ok"] is True and res["mode"] == "party"
    assert "remains fully active" in res["note"]
    assert modes.active_mode() == "party"
    assert scenes == [("party", {"source": "voice", "requested_by_user_id": "u3"})]


async def test_an_unknown_mode_changes_nothing(sm, modes, scenes):
    res = json.loads(await sm._exec_set_mode(FakeHass(), {"mode": "disco"}))
    assert res["ok"] is False and "normal" in res["available"]
    assert modes.active_mode() == "normal" and scenes == []


async def test_a_failing_mode_scene_does_not_undo_the_mode(sm, modes, load, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("scene missing")
    monkeypatch.setattr(load("mode_scene"), "apply_mode_entry", boom)
    res = json.loads(await sm._exec_set_mode(FakeHass(), {"mode": "away"}))
    assert res["ok"] is True and modes.active_mode() == "away"


async def test_current_behaviour_set_mode_is_neither_confirmed_nor_audited(
        sm, modes, scenes, confirm, audit):
    # Looks wrong (minor): away and party change what Nova announces and
    # whether it acts on its own, for the whole house, from any conversation,
    # and leave no row in the Action Audit Log. acknowledge_alert, which changes
    # less, is audited.
    await sm._exec_set_mode(FakeHass(), {"mode": "away"}, user_id=None)
    assert modes.active_mode() == "away"
    assert confirm["asked"] == [] and audit["start"] == []
