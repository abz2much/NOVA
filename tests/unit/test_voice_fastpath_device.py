"""The local fast path knows which device a request came from (Phase 2B.1).

A spoken "unlock the front door" or "open the garage door" from a registered
voice satellite must never be actuated by local_engine.try_local: it has to
defer to the agent's guarded path, where policy.confirm_gate requires a
phone tap. conversation.py passes the request's device_id to every try_local
call (the fast path and all three salvage paths), try_local passes it to
every confirmation pre-check and to its own recursive follow-up call, and
policy.py stays the only place that decides.

Fakes only: no lock, cover, notification service or Home Assistant instance
is touched. Service calls are recorded by FakeHass, never executed.

Focused run:
    python -m pytest tests/unit/test_voice_fastpath_device.py -q
"""
import ast
import json
import pathlib
import sys
import types

import pytest

from fakes import FakeHass

SAT_DEVICE = "dev-sat-kitchen"
PHONE_DEVICE = "dev-phone-companion"   # a real device, but not a voice satellite
CONVERSATION = (pathlib.Path(__file__).resolve().parents[2] / "custom_components"
                / "nova" / "conversation.py")


class _Registry:
    def __init__(self, device_by_entity):
        self._d = device_by_entity

    def async_get(self, entity_id):
        did = self._d.get(entity_id)
        return types.SimpleNamespace(entity_id=entity_id, device_id=did) if did else None


@pytest.fixture
def home(load, monkeypatch):
    """A fake home: front door lock, garage cover, porch light, one kitchen
    voice satellite, and in-memory stand-ins for config, action log and
    database. Returns a namespace the tests drive."""
    config = {"voice_confirm_enabled": False}
    nc = types.ModuleType("jc.nova_config")
    nc.get = lambda key, default=None: config.get(key, default)
    monkeypatch.setitem(sys.modules, "jc.nova_config", nc)

    al = types.ModuleType("jc.action_log")
    al.new_request_id = lambda: "req"
    al.start = lambda *a, **k: 1
    al.start_many = lambda request_id, action, source, targets, **k: {
        t["key"]: i for i, t in enumerate(targets)}
    for name in ("mark_awaiting_approval", "set_approval", "set_execution"):
        setattr(al, name, lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "jc.action_log", al)
    db = types.ModuleType("jc.database")
    db.save_activity = lambda **k: None
    monkeypatch.setitem(sys.modules, "jc.database", db)

    er = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(er, "async_get",
                        lambda hass: _Registry({"assist_satellite.kitchen": SAT_DEVICE}))

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(load("entity_verify"), "_SLEEP", _no_sleep)
    agent = load("agent")
    monkeypatch.setattr(agent, "_VERIFY_SLEEP", _no_sleep)

    asked = []
    vc = load("voice_confirm")
    answers = {"phone": "rejected", "spoken": "rejected"}

    async def _phone(hass, question, timeout=None):
        asked.append("phone")
        return answers["phone"]

    async def _spoken(hass, satellite, question, timeout=None):
        asked.append("spoken")
        return answers["spoken"]

    monkeypatch.setattr(vc, "_confirm_via_notification", _phone)
    monkeypatch.setattr(vc, "_confirm_native", _spoken)
    monkeypatch.setattr(vc, "_confirm_gated", _spoken)
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda hass, eid: None)

    le = load("local_engine")
    monkeypatch.setattr(le, "_CTX", le._ConvCtx())

    hass = FakeHass()
    hass.states.set("lock.front_door", "locked", friendly_name="Front Door")
    hass.states.set("cover.garage_door", "closed", friendly_name="Garage Door",
                    device_class="garage")
    hass.states.set("light.porch", "off", friendly_name="Porch Light")
    hass.states.set("assist_satellite.kitchen", "idle")
    yield types.SimpleNamespace(hass=hass, le=le, agent=agent, config=config,
                                asked=asked, answers=answers)
    hass.close_pending()


def _calls(hass, domain=None):
    return [(d, s, data.get("entity_id")) for d, s, data in hass.service_calls
            if domain is None or d == domain]


# ── 1–3: spoken unlock / garage open defer instead of actuating ────────────

@pytest.mark.parametrize("text, blocked", [
    ("unlock the front door", ("lock", "unlock", "lock.front_door")),
    ("open the garage door", ("cover", "open_cover", "cover.garage_door")),
])
async def test_voice_satellite_opening_defers_to_the_guarded_path(home, text, blocked):
    result = await home.le.try_local(home.hass, text, "sir", device_id=SAT_DEVICE)
    assert result is None                 # deferred: the agent runs the gate
    assert blocked not in _calls(home.hass)
    assert home.hass.service_calls == []
    assert home.asked == []               # no second confirmation implementation here


@pytest.mark.parametrize("text", ["unlock all doors", "open all covers"])
async def test_voice_satellite_bulk_opening_defers_too(home, text):
    home.hass.states.set("cover.study_blind", "closed", friendly_name="Study Blind")
    assert await home.le.try_local(home.hass, text, "sir", device_id=SAT_DEVICE) is None
    assert home.hass.service_calls == []


# ── 4: on the guarded path, a rejected or absent phone tap runs nothing ─────

@pytest.mark.parametrize("answer", ["rejected", "expired", "error"])
@pytest.mark.parametrize("entity_id, action", [
    ("lock.front_door", "unlock"), ("cover.garage_door", "open")])
async def test_guarded_path_needs_a_phone_approval(home, answer, entity_id, action):
    home.answers["phone"] = answer
    out = json.loads(await home.agent._exec_control_device(
        home.hass, {"entity_id": entity_id, "action": action}, device_id=SAT_DEVICE))
    assert out["status"] == "awaiting_confirmation"
    assert home.asked == ["phone"]
    assert home.hass.service_calls == []


# ── 5–6: salvage (force=True) cannot bypass the rule ───────────────────────

@pytest.mark.parametrize("text", ["unlock the front door", "open the garage door"])
async def test_offline_salvage_does_not_actuate_a_voice_opening(home, text):
    result = await home.le.try_local(home.hass, text, "sir", force=True,
                                     device_id=SAT_DEVICE)
    assert result is None                 # conversation.py then says it's offline
    assert home.hass.service_calls == []


def _try_local_calls():
    tree = ast.parse(CONVERSATION.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "try_local"]


def test_every_salvage_call_in_conversation_passes_the_device():
    """The offline, connectivity-failure and exception salvage calls are the
    three force=True calls; each must pass the request's device_id, as must
    the normal fast path call."""
    calls = _try_local_calls()
    salvage = [c for c in calls if any(k.arg == "force" for k in c.keywords)]
    assert len(calls) == 4 and len(salvage) == 3
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert getattr(kw.get("device_id"), "id", None) == "device_id", (
            f"conversation.py line {call.lineno} calls try_local without the device")


# ── 7: the recursive follow-up keeps the device ────────────────────────────

async def test_follow_up_recursion_preserves_the_device(home, monkeypatch):
    le = home.le
    le._update_ctx(entity="light.porch", domain="light", action="turn_on")
    real = le.try_local
    seen = []

    async def spy(hass, text, honorific="sir", force=False, device_id=None):
        seen.append((text, device_id))
        return await real(hass, text, honorific, force=force, device_id=device_id)

    monkeypatch.setattr(le, "try_local", spy)
    await real(home.hass, "also frobnicate the widget", "sir", device_id=SAT_DEVICE)
    assert seen == [("frobnicate the widget", SAT_DEVICE)]


# ── 8–9: safe voice actions are unchanged ──────────────────────────────────

async def test_safe_voice_light_still_runs_locally(home):
    result = await home.le.try_local(home.hass, "turn on the porch light", "sir",
                                     device_id=SAT_DEVICE)
    assert result is not None and result.handled
    assert _calls(home.hass) == [("light", "turn_on", "light.porch")]
    assert home.asked == []


async def test_voice_lock_runs_without_confirmation(home):
    result = await home.le.try_local(home.hass, "lock the front door", "sir",
                                     device_id=SAT_DEVICE)
    assert result is not None and result.handled
    assert _calls(home.hass) == [("lock", "lock", "lock.front_door")]
    assert home.asked == []


# ── 10–11: typed/chat and non-satellite devices are unchanged ──────────────

@pytest.mark.parametrize("device_id", [None, "", PHONE_DEVICE])
async def test_non_satellite_unlock_keeps_its_existing_behaviour(home, device_id):
    """No device (typed chat) or a device that isn't a voice satellite: the
    satellite-only rule doesn't apply, so with voice confirmation off the
    fast path unlocks exactly as before this change."""
    result = await home.le.try_local(home.hass, "unlock the front door", "sir",
                                     device_id=device_id)
    assert result is not None and result.handled
    assert _calls(home.hass) == [("lock", "unlock", "lock.front_door")]


async def test_typed_unlock_still_defers_when_voice_confirmation_is_on(home):
    home.config["voice_confirm_enabled"] = True
    assert await home.le.try_local(home.hass, "unlock the front door", "sir") is None
    assert home.hass.service_calls == []


async def test_callers_that_omit_the_device_still_work(home):
    result = await home.le.try_local(home.hass, "turn on the porch light", "sir")
    assert result is not None and result.handled


# ── 12: a failing authorization check fails closed for unlock/open ─────────

@pytest.mark.parametrize("text", ["unlock the front door", "open the garage door",
                                  "unlock all doors"])
@pytest.mark.parametrize("device_id", [SAT_DEVICE, None])
async def test_authorization_error_defers_instead_of_actuating(home, monkeypatch, load,
                                                                text, device_id):
    policy = load("policy")

    def boom(*a, **k):
        raise RuntimeError("policy check unavailable")

    monkeypatch.setattr(policy, "requires_confirmation", boom)
    assert await home.le.try_local(home.hass, text, "sir", device_id=device_id) is None
    assert home.hass.service_calls == []


async def test_authorization_error_leaves_safe_actions_running(home, monkeypatch, load):
    policy = load("policy")

    def boom(*a, **k):
        raise RuntimeError("policy check unavailable")

    monkeypatch.setattr(policy, "requires_confirmation", boom)
    result = await home.le.try_local(home.hass, "lock the front door", "sir",
                                     device_id=SAT_DEVICE)
    assert result is not None and result.handled
    assert _calls(home.hass) == [("lock", "lock", "lock.front_door")]


def test_needs_confirmation_passes_the_device_only_when_there_is_one(load, monkeypatch):
    le, policy = load("local_engine"), load("policy")
    seen = []

    def spy(hass, domain, service, entity_id="", **kw):
        seen.append((domain, service, kw))
        return False

    monkeypatch.setattr(policy, "requires_confirmation", spy)
    le._needs_confirmation(None, "unlock", "lock.front_door", SAT_DEVICE)
    le._needs_confirmation(None, "unlock", "lock.front_door")
    le._needs_confirmation_domain(None, "scene", "turn_on", "scene.movie", SAT_DEVICE)
    assert seen == [("lock", "unlock", {"device_id": SAT_DEVICE}),
                    ("lock", "unlock", {}),
                    ("scene", "turn_on", {"device_id": SAT_DEVICE})]
