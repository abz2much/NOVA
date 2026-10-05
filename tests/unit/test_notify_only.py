"""announce_notify_only: proactive announcements go to the phone, not the
speakers. Critical urgency keeps speaking exactly as before. Quiet hours,
sleep rules and every other routing rule are unchanged."""
from __future__ import annotations

import ast
import pathlib
import sys
import types

import pytest

from fakes import FakeHass
from test_announce_resilience import _Hass, _State, _set_nova_config, routing  # noqa: F401

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
ROOT = COMP.parents[1]


# ── the setting ─────────────────────────────────────────────────────────────

def _runtime_stub(monkeypatch, nc, rc):
    pkg = nc.__name__.rsplit(".", 1)[0]
    mod = types.ModuleType(f"{pkg}.runtime")
    mod.domain_runtime_config = lambda hass: rc
    monkeypatch.setitem(sys.modules, f"{pkg}.runtime", mod)


def test_setting_defaults_to_off(load, monkeypatch):
    nc = load("nova_config")
    _runtime_stub(monkeypatch, nc, {})
    monkeypatch.setattr(nc, "get", lambda k, d=None: d)
    assert nc.announce_notify_only(object()) is False


def test_setting_is_read_at_call_time_from_runtime_then_config(load, monkeypatch):
    nc = load("nova_config")
    rc: dict = {}
    _runtime_stub(monkeypatch, nc, rc)
    stored = {"announce_notify_only": False}
    monkeypatch.setattr(nc, "get", lambda k, d=None: stored.get(k, d))
    hass = object()
    assert nc.announce_notify_only(hass) is False
    stored["announce_notify_only"] = True          # config.json changes
    assert nc.announce_notify_only(hass) is True
    rc["announce_notify_only"] = False             # live panel value wins
    assert nc.announce_notify_only(hass) is False
    rc["announce_notify_only"] = True
    assert nc.announce_notify_only(hass) is True


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("on", True), ("1", True), ("false", False), ("", False), (0, False)])
def test_setting_coerces_stored_strings(load, monkeypatch, raw, expected):
    nc = load("nova_config")
    _runtime_stub(monkeypatch, nc, {"announce_notify_only": raw})
    assert nc.announce_notify_only(object()) is expected


def test_setting_is_panel_writable_and_surfaced():
    ws = (COMP / "websocket.py").read_text()
    tree = ast.parse(ws)
    keys = next({e.value for e in n.value.elts} for n in ast.walk(tree)
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Set)
                and any(getattr(t, "id", "") == "PANEL_WRITABLE_KEYS" for t in n.targets))
    assert "announce_notify_only" in keys
    assert '"announce_notify_only": bool(_runtime_opt(hass, entry, "announce_notify_only", False))' in ws


def test_setting_is_in_the_readme_table():
    assert "`announce_notify_only`" in (ROOT / "README.md").read_text()


# ── routing: every urgency, setting on and off ──────────────────────────────

SPEAKER = "media_player.kitchen"


@pytest.fixture
def routed(routing, monkeypatch):  # noqa: F811
    flag = {"on": False}
    monkeypatch.setattr(routing, "notify_only_enabled", lambda hass: flag["on"])
    monkeypatch.setattr(routing, "currently_occupied_areas", lambda h: ["living_room"])
    monkeypatch.setattr(routing, "anyone_home", lambda h: True)
    _set_nova_config(routing, monkeypatch, {"room_speakers": {"living_room": SPEAKER}})
    hass = _Hass({SPEAKER: _State(SPEAKER, "idle")})

    def route(urgency, **kw):
        return routing.observer_speak_target(
            hass, urgency=urgency, announcement_speakers=[SPEAKER], **kw)
    return route, flag


@pytest.mark.parametrize("urgency,expected", [
    ("low", ([SPEAKER], "local")),
    ("medium", ([SPEAKER], "local")),
    ("high", ([SPEAKER], "broadcast")),
    ("critical", ([SPEAKER], "broadcast")),
])
def test_setting_off_routes_as_before(routed, urgency, expected):
    route, flag = routed
    flag["on"] = False
    assert route(urgency) == expected


@pytest.mark.parametrize("urgency", ["low", "medium", "high"])
def test_setting_on_sends_non_critical_to_the_phone(routed, urgency):
    route, flag = routed
    flag["on"] = True
    assert route(urgency) == ([], "notify_only")


def test_setting_on_critical_still_speaks_exactly_as_before(routed):
    route, flag = routed
    flag["on"] = False
    before = route("critical")
    flag["on"] = True
    assert route("critical") == before == ([SPEAKER], "broadcast")


def test_setting_on_critical_with_no_speakers_is_still_notify_only(routing, monkeypatch):  # noqa: F811
    monkeypatch.setattr(routing, "notify_only_enabled", lambda hass: True)
    assert routing.observer_speak_target(_Hass({}), urgency="critical") == ([], "notify_only")


def test_setting_on_keeps_sleep_and_quiet_rules(routed):
    route, flag = routed
    flag["on"] = True
    # sleeping: medium and low stay suppressed (no phone buzz at night)
    assert route("medium", is_sleeping=True) == ([], "suppressed")
    assert route("low", is_sleeping=True) == ([], "suppressed")
    assert route("high", is_sleeping=True) == ([], "notify_only")
    # critical overrides sleep, as before
    assert route("critical", is_sleeping=True) == ([SPEAKER], "broadcast")
    # nobody home: medium stays notify_only
    assert route("medium", authoritative_anyone_home=False) == ([], "notify_only")


def test_setting_on_unknown_urgency_stays_suppressed(routed):
    route, flag = routed
    flag["on"] = True
    assert route("weird") == ([], "suppressed")


# ── callers that already handle notify_only still send the notification ─────

async def test_host_health_notify_only_sends_the_phone_notification(load, monkeypatch):
    hh = load("host_health")
    hh.reset_state()
    gate = types.ModuleType("jc.output_gate")
    gate.can_announce = lambda **kw: (True, "ok")
    recorded = []
    gate.record_announcement = lambda **kw: recorded.append(kw)
    gate.habit_note = lambda **kw: kw["message"]
    monkeypatch.setitem(sys.modules, "jc.output_gate", gate)
    audio = types.ModuleType("jc.audio_routing")
    audio.observer_speak_target = lambda hass, **kw: ([], "notify_only")
    monkeypatch.setitem(sys.modules, "jc.audio_routing", audio)
    sleep = types.ModuleType("jc.sleep_detection")
    sleep.is_sleeping = lambda hass, **kw: (False, "")
    monkeypatch.setitem(sys.modules, "jc.sleep_detection", sleep)
    spoken, sent = [], []
    tts = types.ModuleType("jc.tts_helper")
    tts.resolve_tts_for_context = lambda *a, **k: "tts.x"

    async def _announce(*a, **k):
        spoken.append(a)
        return True
    tts.async_announce = _announce
    monkeypatch.setitem(sys.modules, "jc.tts_helper", tts)
    notify = types.ModuleType("jc.notify_targets")

    async def _send(hass, config, payload, **kw):
        sent.append(payload)
        return ["notify.phone"]
    notify.async_send_configured_notifications = _send
    monkeypatch.setitem(sys.modules, "jc.notify_targets", notify)

    await hh._dispatch_alert(FakeHass(), {}, "disk is nearly full", "host_health_problem")
    assert sent and sent[0]["message"] == "disk is nearly full"
    assert spoken == []
    hh.reset_state()


# ── cognitive_core: the alert itself reaches the phone ──────────────────────

@pytest.fixture
def cc_env(load, monkeypatch):
    cc = load("cognitive_core")
    nc = load("nova_config")
    hab = load("habituation")
    monkeypatch.setattr(hab, "is_quiet", lambda k: False)
    monkeypatch.setattr(hab, "record", lambda *a, **k: None)
    env = types.SimpleNamespace(pushes=[], prompts=[], spoken=[], mode="notify_only", on=True)

    async def _push(hass, config, message, action_type, snap=None, *,
                    request_id=None, extra_data=None):
        env.pushes.append((message, extra_data))
    monkeypatch.setattr(cc, "_push_notification", _push)
    aa = load("adaptive_awareness")

    async def _prompt(hass, config, message, decision_id):
        env.prompts.append(message)
        return True
    monkeypatch.setattr(aa, "async_send_rating_prompt", _prompt)
    monkeypatch.setattr(nc, "announce_notify_only", lambda hass: env.on)
    tts = types.ModuleType("jc.tts_helper")
    tts.resolve_tts_for_context = lambda *a, **k: "tts.x"

    async def _announce(hass, message, *a, **k):
        env.spoken.append(message)
    tts.async_announce = _announce
    ar = types.ModuleType("jc.audio_routing")
    ar.observer_speak_target = lambda *a, **k: (
        ([], env.mode) if env.mode == "notify_only" else (["media_player.x"], env.mode))
    monkeypatch.setitem(sys.modules, "jc.tts_helper", tts)
    monkeypatch.setitem(sys.modules, "jc.audio_routing", ar)
    sd = types.ModuleType("jc.sleep_detection")
    sd._in_quiet_hours = lambda *a: False
    monkeypatch.setitem(sys.modules, "jc.sleep_detection", sd)
    return cc, env


def _alert(urgency, **kw):
    a = {"type": "anticipation_overdue", "urgency": urgency, "message": "Porch is quiet.",
         "pattern_key": f"overdue:{urgency}", "decision_id": 7}
    a.update(kw)
    return a


@pytest.mark.parametrize("urgency", ["low", "medium"])
async def test_cognitive_alert_is_pushed_instead_of_spoken_when_on(cc_env, fake_hass, urgency):
    cc, env = cc_env
    await cc._emit_action(fake_hass, {}, _alert(urgency), sleeping=False)
    assert env.spoken == []
    assert [m for m, _ in env.pushes] == ["Porch is quiet."]
    assert env.prompts == []          # the push already carries the rating buttons


async def test_cognitive_alert_old_behaviour_when_off(cc_env, fake_hass):
    cc, env = cc_env
    env.on = False
    await cc._emit_action(fake_hass, {}, _alert("medium"), sleeping=False)
    assert env.spoken == [] and env.pushes == []      # notify_only: no push for medium, as before


async def test_cognitive_critical_still_speaks_when_on(cc_env, fake_hass):
    cc, env = cc_env
    env.mode = "broadcast"
    await cc._emit_action(fake_hass, {}, _alert("critical"), sleeping=False)
    assert env.spoken == ["Porch is quiet."]
    assert len(env.pushes) == 1                       # critical was always pushed too


# ── sentinel: its own speech path ───────────────────────────────────────────

@pytest.fixture
def sentinel_env(load, fake_hass, monkeypatch):
    ev = types.ModuleType("homeassistant.helpers.event")
    ev.async_track_state_change_event = lambda *a, **k: (lambda: None)
    ev.async_track_time_interval = lambda *a, **k: (lambda: None)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)
    sd = types.ModuleType("jc.sleep_detection")
    sd.is_sleeping = lambda hass, **kw: (False, None)
    monkeypatch.setitem(sys.modules, "jc.sleep_detection", sd)
    s_mod = load("sentinel")
    nc = load("nova_config")
    env = types.SimpleNamespace(spoken=[], on=True, notify="notify.mobile_app_test")
    monkeypatch.setattr(nc, "announce_notify_only", lambda hass: env.on)

    async def _announce(hass, text, tts, speakers, context="", action_request_id=None):
        env.spoken.append(text)
    monkeypatch.setattr(s_mod, "async_announce", _announce)
    monkeypatch.setattr(s_mod, "save_sentinel_event", lambda *a, **k: None)
    monkeypatch.setattr(s_mod, "save_message", lambda *a, **k: None)
    monkeypatch.setattr(
        s_mod.nova_config, "runtime_get",
        lambda hass, entry, key, default=None: env.notify if key == "notify_service" else default)
    fake_hass.services.register("notify", "mobile_app_test")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door",
                         friendly_name="Front Door")
    sentinel = s_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir", rules=[], entry=None)
    rule = {"id": "door_left_open",
            "message": "{honorific}, {friendly_name} has been open for {minutes} minutes."}
    return sentinel, rule, env


async def test_sentinel_sends_the_phone_notification_and_stays_quiet_when_on(sentinel_env):
    sentinel, rule, env = sentinel_env
    await sentinel._announce_rule("binary_sensor.front_door", rule, 10)
    assert env.spoken == []
    pushed = [c for c in sentinel.hass.service_calls if c[0] == "notify"]
    assert len(pushed) == 1 and "has been open" in pushed[0][2]["message"]


async def test_sentinel_speaks_when_off(sentinel_env):
    sentinel, rule, env = sentinel_env
    env.on = False
    await sentinel._announce_rule("binary_sensor.front_door", rule, 10)
    assert len(env.spoken) == 1
    assert len([c for c in sentinel.hass.service_calls if c[0] == "notify"]) == 1


async def test_sentinel_speaks_after_all_if_no_notification_service_is_set(sentinel_env):
    sentinel, rule, env = sentinel_env
    env.notify = ""
    await sentinel._announce_rule("binary_sensor.front_door", rule, 10)
    assert len(env.spoken) == 1                       # never silently dropped
