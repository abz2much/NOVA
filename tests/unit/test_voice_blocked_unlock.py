"""Tests for the voice-blocked-opening gate (v7.87.0, backlog #4).

A spoken "unlock the front door" could be a deepfake of the owner's voice.
Locking/closing carries no such risk and stays frictionless by voice; but
unlock/open — when the request came from a voice satellite — must always be
confirmed via a PHONE tap, never a spoken confirmation (which would repeat
the exact weakness this exists to close), and this applies regardless of
whether the general voice_confirm_enabled toggle is on. A text/chat request
(no satellite device_id) is unaffected — it follows the pre-existing
opt-in confirmation behaviour unchanged.
"""
import json
import types

import pytest


@pytest.fixture
def pol(load):
    return load("policy")


@pytest.fixture
def vc(load):
    return load("voice_confirm")


@pytest.fixture
def agent(load):
    return load("agent")


def _registry_with(device_map: dict):
    def _get(eid):
        did = device_map.get(eid)
        return types.SimpleNamespace(device_id=did) if did else None
    return types.SimpleNamespace(async_get=_get)


class _Hass:
    def __init__(self, satellites=None):
        self._satellites = satellites or []
        self.calls = []
        self.service_calls = []

        class _States:
            def __init__(self, outer):
                self._outer = outer

            def async_all(self, domain):
                if domain == "assist_satellite":
                    return [types.SimpleNamespace(entity_id=e) for e in self._outer._satellites]
                return []

            def get(self, eid):
                return types.SimpleNamespace(entity_id=eid, state="locked", attributes={})

        class _Services:
            def __init__(self, outer):
                self._outer = outer

            async def async_call(self, domain, service, data=None, blocking=False, **kw):
                self._outer.service_calls.append((domain, service, dict(data or {})))

        self.states = _States(self)
        self.services = _Services(self)

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_task(self, coro, name=None):
        coro.close()  # don't actually run background verify-after-act


# ── voice_confirm.is_voice_satellite_device ──────────────────────────────────

def test_is_voice_satellite_device_matches_paired_device(vc, monkeypatch):
    import sys
    er_mod = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(er_mod, "async_get",
                        lambda h: _registry_with({"assist_satellite.kitchen": "dev-sat-1"}))
    hass = _Hass(satellites=["assist_satellite.kitchen"])
    assert vc.is_voice_satellite_device(hass, "dev-sat-1") is True
    assert vc.is_voice_satellite_device(hass, "dev-some-phone") is False
    assert vc.is_voice_satellite_device(hass, "") is False
    assert vc.is_voice_satellite_device(hass, None) is False


async def test_confirm_via_phone_only_never_touches_satellite_tiers(vc, monkeypatch):
    """Even with a satellite available, confirm_via_phone_only must go
    straight to the notification tier -- a spoken 'yes' would be exactly as
    spoofable as the spoken unlock request itself."""
    calls = []

    async def fake_notify(hass, question, timeout=None):
        calls.append(question)
        return "approved"

    async def _must_not_run(*a, **k):
        raise AssertionError("a voice confirmation tier must never run here")

    monkeypatch.setattr(vc, "_confirm_via_notification", fake_notify)
    monkeypatch.setattr(vc, "_confirm_native", _must_not_run)
    monkeypatch.setattr(vc, "_confirm_gated", _must_not_run)
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda hass, eid: "assist_satellite.kitchen")

    ok = await vc.confirm_via_phone_only(_Hass(), "Unlock front door?")
    assert ok is True
    assert calls == ["Unlock front door?"]


# ── policy.confirm_gate / requires_confirmation ──────────────────────────────

def test_voice_satellite_request_true_for_paired_device(pol, vc, monkeypatch):
    monkeypatch.setattr(vc, "is_voice_satellite_device", lambda hass, did, **k: did == "dev-sat-1")
    assert pol._voice_satellite_request(_Hass(), "dev-sat-1") is True
    assert pol._voice_satellite_request(_Hass(), "dev-other") is False
    assert pol._voice_satellite_request(_Hass(), "") is False


async def test_confirm_gate_blocks_voice_unlock_until_phone_confirmed(pol, vc, monkeypatch):
    monkeypatch.setattr(vc, "is_voice_satellite_device", lambda hass, did, **k: True)

    async def deny(hass, question, timeout=None):
        return "rejected"
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", deny)

    ok, note, approval_result = await pol.confirm_gate(_Hass(), "lock", "unlock", "lock.front_door",
                                      "unlock", device_id="dev-sat-1")
    assert ok is False
    assert "voice" in note.lower()
    assert approval_result == "rejected"


async def test_confirm_gate_allows_voice_unlock_once_phone_confirmed(pol, vc, monkeypatch):
    monkeypatch.setattr(vc, "is_voice_satellite_device", lambda hass, did, **k: True)

    async def approve(hass, question, timeout=None):
        return "approved"
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", approve)

    ok, note, approval_result = await pol.confirm_gate(_Hass(), "lock", "unlock", "lock.front_door",
                                      "unlock", device_id="dev-sat-1")
    assert ok is True
    assert note == ""
    assert approval_result == "approved"


async def test_confirm_gate_uses_phone_only_not_spoken_confirm(pol, vc, monkeypatch):
    """The load-bearing assertion: voice_confirm.confirm() (which can use a
    spoken channel) must never be the function consulted for a voice-sourced
    unlock -- only confirm_via_phone_only."""
    monkeypatch.setattr(vc, "is_voice_satellite_device", lambda hass, did, **k: True)

    async def _must_not_run(*a, **k):
        raise AssertionError("voice_confirm.confirm (a voice-capable channel) must not run here")
    monkeypatch.setattr(vc, "confirm", _must_not_run)
    monkeypatch.setattr(vc, "confirm_typed", _must_not_run)

    called = []
    async def approve(hass, question, timeout=None):
        called.append(question)
        return "approved"
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", approve)

    ok, _, approval_result = await pol.confirm_gate(_Hass(), "cover", "open_cover", "cover.garage",
                                   "open", device_id="dev-sat-1")
    assert ok is True
    assert approval_result == "approved"
    assert len(called) == 1


async def test_confirm_gate_lock_is_never_blocked_by_voice_rule(pol, vc, monkeypatch):
    """Locking is safe by design -- the voice rule must not add friction to it,
    even from a voice satellite, even if it were (hypothetically) misconfigured
    as protected elsewhere."""
    monkeypatch.setattr(vc, "is_voice_satellite_device", lambda hass, did, **k: True)

    async def _must_not_run(*a, **k):
        raise AssertionError("confirm_via_phone_only must not run for a lock (safe direction)")
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", _must_not_run)
    monkeypatch.setattr(vc, "action_is_protected", lambda hass, d, s, e="": False)

    ok, note, approval_result = await pol.confirm_gate(_Hass(), "lock", "lock", "lock.front_door",
                                      "lock", device_id="dev-sat-1")
    assert ok is True and note == ""
    assert approval_result == "not_required"


async def test_confirm_gate_text_request_unaffected_by_voice_rule(pol, vc, monkeypatch):
    """No device_id (text/chat, or a non-satellite device) -- falls through to
    the pre-existing opt-in behaviour untouched. With the toggle off (today's
    default), an unlock proceeds exactly as it did before this change."""
    monkeypatch.setattr(vc, "is_voice_satellite_device", lambda hass, did, **k: False)
    monkeypatch.setattr(vc, "action_is_protected", lambda hass, d, s, e="": False)

    async def _must_not_run(*a, **k):
        raise AssertionError("phone confirmation must not be forced for a text request")
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", _must_not_run)

    ok, note, approval_result = await pol.confirm_gate(_Hass(), "lock", "unlock", "lock.front_door",
                                      "unlock", device_id="")
    assert ok is True and note == ""
    assert approval_result == "not_required"


def test_requires_confirmation_true_for_voice_unlock_regardless_of_toggle(pol, vc, monkeypatch):
    """bulk_control's skip-and-report path relies on this being True even when
    voice_confirm_enabled is off -- the general opt-in must not gate this."""
    monkeypatch.setattr(vc, "is_voice_satellite_device", lambda hass, did, **k: True)
    monkeypatch.setattr(vc, "action_is_protected", lambda hass, d, s, e="": False)  # opt-in OFF
    assert pol.requires_confirmation(_Hass(), "lock", "unlock", "lock.front_door",
                                     device_id="dev-sat-1") is True


def test_requires_confirmation_lock_stays_false_when_unprotected(pol, vc, monkeypatch):
    monkeypatch.setattr(vc, "is_voice_satellite_device", lambda hass, did, **k: True)
    monkeypatch.setattr(vc, "action_is_protected", lambda hass, d, s, e="": False)
    assert pol.requires_confirmation(_Hass(), "lock", "lock", "lock.front_door",
                                     device_id="dev-sat-1") is False


# ── agent._exec_control_device wiring ─────────────────────────────────────────

async def test_control_device_voice_unlock_needs_phone_confirmation(agent, pol, monkeypatch):
    async def fake_gate(hass, domain, service, entity_id="", action_label="", device_id="",
                        target_name=""):
        assert device_id == "dev-sat-1"
        return False, "voice alone can't authorize this", "rejected"
    monkeypatch.setattr(pol, "confirm_gate", fake_gate)

    hass = _Hass()
    out = json.loads(await agent._exec_control_device(
        hass, {"entity_id": "lock.front_door", "action": "unlock"}, device_id="dev-sat-1"))
    assert out["status"] == "awaiting_confirmation"
    assert hass.service_calls == []


async def test_control_device_voice_unlock_proceeds_once_confirmed(agent, pol, monkeypatch):
    async def fake_gate(hass, domain, service, entity_id="", action_label="", device_id="",
                        target_name=""):
        return True, "", "approved"
    monkeypatch.setattr(pol, "confirm_gate", fake_gate)

    hass = _Hass()
    out = json.loads(await agent._exec_control_device(
        hass, {"entity_id": "lock.front_door", "action": "unlock"}, device_id="dev-sat-1"))
    assert out.get("success") is True
    assert hass.service_calls == [("lock", "unlock", {"entity_id": "lock.front_door"})]


# ── agent._execute_tool: device_id actually reaches the tool ────────────────

async def test_execute_tool_extracts_device_id_from_user_input(agent, pol, monkeypatch):
    seen = {}

    async def fake_gate(hass, domain, service, entity_id="", action_label="", device_id="",
                        target_name=""):
        seen["device_id"] = device_id
        return True, "", "not_required"
    monkeypatch.setattr(pol, "confirm_gate", fake_gate)

    user_input = types.SimpleNamespace(device_id="dev-sat-1")
    await agent._execute_tool(
        _Hass(), "control_device", {"entity_id": "lock.front_door", "action": "unlock"},
        user_input=user_input,
    )
    assert seen["device_id"] == "dev-sat-1"


async def test_execute_tool_omits_device_id_for_tools_that_dont_accept_it(agent, monkeypatch):
    """Most tools have signature (hass, args) -- passing device_id to them
    would be a TypeError. get_entity_state is a plain example."""
    hass = _Hass()
    # get_entity_state doesn't exist on the fake states dict by entity_id, but
    # the call must not raise a TypeError over an unexpected kwarg regardless
    # of the entity lookup result.
    result = await agent._execute_tool(
        hass, "get_entity_state", {"entity_id": "light.kitchen"},
        user_input=types.SimpleNamespace(device_id="dev-sat-1"),
    )
    assert isinstance(result, str)  # didn't raise; returned some JSON string


async def test_control_device_lock_action_not_gated_by_device_id(agent, pol, monkeypatch):
    """Sanity: passing a device_id at all must not itself add friction to a
    lock action -- only confirm_gate's own logic (tested above) decides
    that, and here it's mocked to allow through unconditionally."""
    async def fake_gate(hass, domain, service, entity_id="", action_label="", device_id="",
                        target_name=""):
        return True, "", "not_required"
    monkeypatch.setattr(pol, "confirm_gate", fake_gate)

    hass = _Hass()
    out = json.loads(await agent._exec_control_device(
        hass, {"entity_id": "lock.front_door", "action": "lock"}, device_id="dev-sat-1"))
    assert out.get("success") is True
