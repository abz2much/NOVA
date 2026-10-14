"""Tests for voice confirmation (v6.67.0). The safety-critical property is the
fail-safe: a confirmation that isn't clearly affirmative must return False so the
protected action does NOT run. Also covers which actions are protected, mode
selection, and the yes/no sentence sets."""
import pytest


@pytest.fixture
def vc(load, monkeypatch):
    m = load("voice_confirm")
    return m


def _cfg_map(mapping):
    return lambda hass, k, d=None: mapping.get(k, d)


class _Hass:
    """Minimal hass; services.async_call is a recording no-op by default."""
    def __init__(self):
        self.calls = []
        self._states = {}
        outer = self
        class _Svc:
            async def async_call(self, dom, name, data, **kw):
                outer.calls.append((dom, name, data, kw))
                return outer._call_result
        class _States:
            def async_all(self, domain):
                return []
            def get(self, eid):
                return outer._states.get(eid)
        self.services = _Svc()
        self.states = _States()
        self._call_result = None


# ── enablement + protection ──────────────────────────────────────────────────

def test_disabled_by_default(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({}))
    assert vc.is_enabled(_Hass()) is False


def test_protected_requires_enabled(vc, monkeypatch):
    # even a lock/unlock isn't protected if the feature is off
    monkeypatch.setattr(vc, "_cfg", _cfg_map({}))
    assert vc.action_is_protected(_Hass(), "lock", "unlock", "lock.front") is False


def test_lock_unlock_is_protected_when_enabled(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({"voice_confirm_enabled": True}))
    assert vc.action_is_protected(_Hass(), "lock", "unlock", "lock.front") is True


def test_light_toggle_is_not_protected(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({"voice_confirm_enabled": True}))
    assert vc.action_is_protected(_Hass(), "light", "turn_on", "light.kitchen") is False


def test_garage_open_is_protected(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({"voice_confirm_enabled": True}))
    assert vc.action_is_protected(_Hass(), "cover", "open", "cover.garage") is True


def test_alarm_disarm_is_protected(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({"voice_confirm_enabled": True}))
    assert vc.action_is_protected(_Hass(), "alarm_control_panel", "alarm_disarm", "alarm_control_panel.home") is True


def test_switch_off_only_protected_for_security(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({"voice_confirm_enabled": True}))
    assert vc.action_is_protected(_Hass(), "switch", "turn_off", "switch.alarm_siren") is True
    assert vc.action_is_protected(_Hass(), "switch", "turn_off", "switch.desk_lamp") is False


def test_entity_override_exempts(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({
        "voice_confirm_enabled": True,
        "voice_confirm_entities": ["!lock.test_deadbolt"]}))
    # explicit exemption wins even for a normally-protected action
    assert vc.action_is_protected(_Hass(), "lock", "unlock", "lock.test_deadbolt") is False


def test_entity_override_includes(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({
        "voice_confirm_enabled": True,
        "voice_confirm_entities": ["switch.pool_pump"]}))
    # a normally-unprotected switch can be opted in
    assert vc.action_is_protected(_Hass(), "switch", "turn_off", "switch.pool_pump") is True


# ── mode selection ───────────────────────────────────────────────────────────

def test_mode_defaults_to_auto(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({}))
    assert vc._mode(_Hass()) == "auto"


def test_mode_bad_value_falls_back_auto(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({"voice_confirm_mode": "nonsense"}))
    assert vc._mode(_Hass()) == "auto"


def test_mode_explicit_gated(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({"voice_confirm_mode": "gated"}))
    assert vc._mode(_Hass()) == "gated"


# ── fail-safe confirm behavior ───────────────────────────────────────────────

async def test_confirm_no_satellite_returns_false(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({"voice_confirm_enabled": True}))
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda h, e: None)
    # no satellite → falls to phone notification → no notify.mobile_app_*
    # targets on the minimal fake hass → "error" → fail-safe False.
    assert await vc.confirm(_Hass(), "sure?", entity_id="lock.front") is False
    assert await vc.confirm_typed(_Hass(), "sure?", entity_id="lock.front") == "error"


async def test_confirm_native_affirmative_returns_true(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({
        "voice_confirm_enabled": True, "voice_confirm_mode": "native"}))
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda h, e: "assist_satellite.basement")
    async def _native_yes(h, s, q, t): return "approved"
    monkeypatch.setattr(vc, "_confirm_native", _native_yes)
    assert await vc.confirm(_Hass(), "sure?", entity_id="lock.front") is True
    assert await vc.confirm_typed(_Hass(), "sure?", entity_id="lock.front") == "approved"


async def test_confirm_native_denial_returns_false(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({
        "voice_confirm_enabled": True, "voice_confirm_mode": "native"}))
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda h, e: "assist_satellite.basement")
    async def _native_no(h, s, q, t): return "rejected"
    monkeypatch.setattr(vc, "_confirm_native", _native_no)
    assert await vc.confirm(_Hass(), "sure?", entity_id="lock.front") is False
    assert await vc.confirm_typed(_Hass(), "sure?", entity_id="lock.front") == "rejected"


async def test_confirm_gated_never_auto_approves(vc, monkeypatch):
    # gated path voices the prompt + reopens mic but cannot itself approve →
    # always False (the follow-up turn completes the action). Fail-safe.
    monkeypatch.setattr(vc, "_cfg", _cfg_map({
        "voice_confirm_enabled": True, "voice_confirm_mode": "gated"}))
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda h, e: "assist_satellite.basement")
    monkeypatch.setattr(vc, "_speaker_for_satellite", lambda h, s: ("tts.piper", ["media_player.nest"]))
    async def _noop_announce(*a, **k): return None
    async def _noop_wait(*a, **k): return None
    async def _noop_start(*a, **k): return True
    # patch the tts_helper import target + waits
    import sys, types
    th = types.ModuleType("jc.tts_helper")
    th.async_announce = _noop_announce
    sys.modules["jc.tts_helper"] = th
    monkeypatch.setattr(vc, "_wait_for_playback", _noop_wait)
    monkeypatch.setattr(vc, "_start_listening", _noop_start)
    try:
        assert await vc.confirm(_Hass(), "sure?", entity_id="lock.front") is False
        assert await vc.confirm_typed(_Hass(), "sure?", entity_id="lock.front") == "deferred"
    finally:
        sys.modules.pop("jc.tts_helper", None)


async def test_confirm_exception_is_failsafe_false(vc, monkeypatch):
    monkeypatch.setattr(vc, "_cfg", _cfg_map({"voice_confirm_enabled": True}))
    def _boom(h, e): raise RuntimeError("x")
    monkeypatch.setattr(vc, "_satellite_for_entity", _boom)
    assert await vc.confirm(_Hass(), "sure?", entity_id="lock.front") is False
    assert await vc.confirm_typed(_Hass(), "sure?", entity_id="lock.front") == "error"


# ── typed layer: the three-way split _confirm_via_notification actually
# computes internally, previously discarded before this feature and never
# directly tested at all ─────────────────────────────────────────────────────

class _HassWithNotify(_Hass):
    """Adds services.async_services() so _confirm_via_notification can find
    a notify.mobile_app_* target, and a controllable bus for firing the
    confirm/deny action event."""
    def __init__(self, notify_names=("mobile_app_alex",)):
        super().__init__()
        self._notify_names = notify_names
        self._bus_listener = None
        outer = self
        class _Svc(type(self.services)):
            def async_services(self):
                return {"notify": {n: {} for n in outer._notify_names}}
        self.services = _Svc()
        self.services.calls = self.calls
        self.services._call_result = None
        class _Bus:
            def async_listen(_self, event_type, cb):
                outer._bus_listener = cb
                return lambda: None
        self.bus = _Bus()

    def fire(self, action):
        self._bus_listener(type("Ev", (), {"data": {"action": action}})())


async def test_confirm_via_notification_explicit_confirm_is_approved(vc):
    hass = _HassWithNotify()

    import asyncio
    async def _tap_soon():
        await asyncio.sleep(0)
        # The action name is generated per-call; recover it from the sent payload.
        domain, name, data, kw = hass.calls[0]
        hass.fire(data["data"]["actions"][0]["action"])

    task = asyncio.ensure_future(_tap_soon())
    result = await vc._confirm_via_notification(hass, "Unlock the door?", timeout=2)
    await task
    assert result == "approved"


async def test_confirm_via_notification_explicit_deny_is_rejected(vc):
    hass = _HassWithNotify()

    import asyncio
    async def _tap_soon():
        await asyncio.sleep(0)
        domain, name, data, kw = hass.calls[0]
        hass.fire(data["data"]["actions"][1]["action"])

    task = asyncio.ensure_future(_tap_soon())
    result = await vc._confirm_via_notification(hass, "Unlock the door?", timeout=2)
    await task
    assert result == "rejected"


async def test_confirm_via_notification_timeout_is_expired(vc):
    hass = _HassWithNotify()
    result = await vc._confirm_via_notification(hass, "Unlock the door?", timeout=0.01)
    assert result == "expired"


async def test_confirm_via_notification_no_targets_is_error(vc):
    hass = _HassWithNotify(notify_names=())
    result = await vc._confirm_via_notification(hass, "Unlock the door?", timeout=1)
    assert result == "error"


async def test_confirm_via_phone_only_typed_matches_bool_wrapper(vc, monkeypatch):
    async def _approved(hass, q, timeout=None):
        return "approved"
    monkeypatch.setattr(vc, "_confirm_via_notification", _approved)
    assert await vc.confirm_via_phone_only_typed(_Hass(), "sure?") == "approved"
    assert await vc.confirm_via_phone_only(_Hass(), "sure?") is True

    async def _rejected(hass, q, timeout=None):
        return "rejected"
    monkeypatch.setattr(vc, "_confirm_via_notification", _rejected)
    assert await vc.confirm_via_phone_only_typed(_Hass(), "sure?") == "rejected"
    assert await vc.confirm_via_phone_only(_Hass(), "sure?") is False


# ── sentence sets ────────────────────────────────────────────────────────────

def test_yes_no_sentence_sets_are_disjoint(vc):
    assert not (set(vc._YES) & set(vc._NO))       # no overlap
    assert "yes" in vc._YES and "no" in vc._NO
    assert "unlock it" in vc._YES and "cancel" in vc._NO


# ── 8.49.35 fixes ────────────────────────────────────────────────────────────

async def test_native_confirm_expires_when_the_satellite_never_answers(vc):
    """Fixed: the native path ignored the answer time limit, so a satellite
    that never answered held the protected action open forever."""
    import asyncio
    hass = _Hass()

    async def _never(*a, **k):
        await asyncio.sleep(3600)
    hass.services.async_call = _never
    result = await asyncio.wait_for(
        vc._confirm_native(hass, "assist_satellite.hall", "Unlock?", 0.05), 2)
    assert result == "expired"


async def test_native_confirm_still_reads_a_quick_answer(vc):
    hass = _Hass()
    hass._call_result = {"assist_satellite.hall": {"id": "confirm"}}
    assert await vc._confirm_native(hass, "assist_satellite.hall", "Unlock?", 5) == "approved"
    hass._call_result = {"id": "deny"}
    assert await vc._confirm_native(hass, "assist_satellite.hall", "Unlock?", 5) == "rejected"
    hass._call_result = {"something": "else"}
    assert await vc._confirm_native(hass, "assist_satellite.hall", "Unlock?", 5) is None


async def test_phone_tap_during_the_send_is_not_missed(vc):
    """Fixed: Nova started listening for the tap only after sending, so a
    tap that came back while the push was still going out was lost."""
    hass = _HassWithNotify()
    send = type(hass.services).async_call

    async def _send_and_tap(self, dom, name, data, **kw):
        await send(self, dom, name, data, **kw)
        if hass._bus_listener is not None:
            hass.fire(data["data"]["actions"][0]["action"])
    type(hass.services).async_call = _send_and_tap
    assert await vc._confirm_via_notification(hass, "Unlock the door?", timeout=0.05) == "approved"


async def test_followup_without_a_speaker_reports_failure_and_leaves_the_mic(vc, monkeypatch):
    """Fixed: with no speaker the question was skipped but the mic still
    opened and the call said ok."""
    monkeypatch.setattr(vc, "_cfg", _cfg_map({"voice_confirm_mode": "gated"}))
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda h, e: "assist_satellite.hall")
    monkeypatch.setattr(vc, "_speaker_for_satellite", lambda h, s: ("tts.piper", []))
    opened = []

    async def _start(h, s):
        opened.append(s)
        return True
    monkeypatch.setattr(vc, "_start_listening", _start)
    result = await vc.ask_followup(_Hass(), "Which one?")
    assert result["ok"] is False and result["satellite"] == "assist_satellite.hall"
    assert opened == []


async def test_announce_test_note_names_no_particular_speaker(vc):
    """Fixed: the note named one brand of speaker."""
    result = await vc.announce_test(_Hass(), "assist_satellite.hall")
    assert result["ok"] is True
    assert "Nest" not in result["note"] and "your speaker" in result["note"]
