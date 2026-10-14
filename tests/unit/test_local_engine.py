"""The local engine, from its behaviour spec (7c).

The engine answers simple requests with no model: device commands, state
questions, scenes, the goodnight sweep, and a few fixed replies. These tests
pin each rule with small stand ins for policy, the action log, the state
check and the background verifier. The earlier files
(test_local_engine_*.py, test_voice_fastpath_device.py,
test_local_fastpath_gate.py, test_local_offline.py) still cover the
regressions they were written for.
"""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass


@pytest.fixture
def le(load, monkeypatch):
    mod = load("local_engine")
    monkeypatch.setattr(mod, "_read_aliases", lambda: {})
    monkeypatch.setattr(mod, "_pick", lambda options: options[0])
    mod._CTX.last_ts = 0.0
    yield mod
    mod._CTX.last_ts = 0.0


class _Verdict:
    def __init__(self, allowed=True, code="", note="", approval="not_required"):
        self.allowed, self.code, self.note, self.approval = allowed, code, note, approval

    @property
    def already_secure(self):
        return self.code == "already_secure"


@pytest.fixture
def env(monkeypatch):
    """Stand ins for everything the engine reaches outside itself."""
    seen = types.SimpleNamespace(asked=[], rows=[], results=[], verify=[], unverified=[],
                                 answers={}, policy_error=None, classify="low",
                                 checks={}, brightness=None, exempt=set(),
                                 not_checkable=[])

    pol = types.ModuleType("jc.policy")

    class AuthorityRequest:
        def __init__(self, domain, service, entity_id="", device_id=""):
            self.args = (domain, service, entity_id, device_id)

    def authorize_now(hass, req):
        seen.asked.append(req.args)
        if seen.policy_error:
            raise seen.policy_error
        answer = seen.answers.get(req.args[2], seen.answers.get(req.args[:2], True))
        return answer if isinstance(answer, _Verdict) else _Verdict(answer)
    pol.AuthorityRequest = AuthorityRequest
    pol.authorize_now = authorize_now

    def classify(domain, service, entity_id):
        if seen.classify is None:
            raise RuntimeError("no table")
        return seen.classify, ""
    pol.classify = classify

    al = types.ModuleType("jc.action_log")
    al.new_request_id = lambda: "req"

    def start(request_id, action, source, **kw):
        seen.rows.append((action, source, kw))
        return f"row{len(seen.rows)}"

    def start_many(request_id, action, source, rows, **kw):
        seen.rows.append((action, source, rows))
        return {r["key"]: f"row-{r['key']}" for r in rows}
    al.start, al.start_many = start, start_many
    al.set_execution = lambda rid, result, reason_code=None: seen.results.append(
        (rid, result, reason_code))
    al.mark_not_checkable = lambda rid: seen.not_checkable.append(rid)

    ev = types.ModuleType("jc.entity_verify")
    ev.FAST_VERIFY_DOMAINS = {"light", "switch", "fan"}
    ev.check_state_once = lambda hass, eid, expected: seen.checks.get(eid) == expected
    ev.check_brightness_once = lambda hass, eid, pct: seen.brightness == pct

    async def wait_until(check):
        return check()

    async def record_unverified(hass, eid, action, source, detail=""):
        seen.unverified.append((eid, action, source, detail))
    ev.wait_until, ev.record_unverified = wait_until, record_unverified

    agent = types.ModuleType("jc.agent")

    async def verify_control(hass, eid, action, domain, service, data, source, action_id):
        seen.verify.append((eid, action, domain, service, data, source, action_id))
    agent._verify_control = verify_control

    cc = types.ModuleType("jc.cognitive_core")
    cc._lockdown_exempt_locks = lambda: seen.exempt

    for name, mod in (("policy", pol), ("action_log", al), ("entity_verify", ev),
                      ("agent", agent), ("cognitive_core", cc)):
        monkeypatch.setitem(sys.modules, f"jc.{name}", mod)
    return seen


@pytest.fixture
def hass():
    h = FakeHass()
    yield h
    for coro in h._tasks:
        coro.close()


async def _run(hass):
    for coro in list(hass._tasks):
        await coro
    hass._tasks.clear()


# ── text helpers ────────────────────────────────────────────────────────────

def test_normalize(le):
    assert le._normalize("  Turn ON   the Light!?  ") == "turn on the light"
    assert le._normalize("what's open.") == "what's open"


def test_contains_word(le):
    assert le._contains_word("is it unlocked", "lock") is False
    assert le._contains_word("lock the door", "lock") is True
    assert le._contains_word("please turn on now", "turn on") is True


@pytest.mark.parametrize("text,score", [
    ("turn on the lamp", 0),
    ("what time is it", 5),
    ("hello there", 5),
    ("why is the lamp on", 60),
    ("explain why the lock is open", 40),
    ("tell me about the house because it is late", 70),
])
def test_score_complexity(le, text, score):
    assert le.score_complexity(text) == score


def test_score_complexity_long_questions_and_bounds(le):
    long_q = "can you " + "really " * 14 + "check this for me today?"
    assert le.score_complexity(long_q) == 45
    assert le.score_complexity("write " + "x " * 30) == 75
    assert 0 <= le.score_complexity("turn on hello") <= 100


@pytest.mark.parametrize("a,b,expect", [
    ("kitchen", "kitchen", 100.0), ("", "x", 0.0), ("x", "", 0.0),
])
def test_fuzzy_score_edges(le, a, b, expect):
    assert le._fuzzy_score(a, b) == expect


def test_fuzzy_score_takes_the_best_signal(le):
    assert le._fuzzy_score("desk", "office desk lamp") == pytest.approx(100 / 3)
    assert le._fuzzy_score("office desk lamp", "desk") == pytest.approx(100 / 3)
    assert le._fuzzy_score("deskxx", "xxdesk") == pytest.approx(80.0)     # bigrams
    assert le._fuzzy_score("desk lamp", "lamp desk") == 100.0
    assert le._fuzzy_score("a", "b") == 0.0


def test_looks_like_entity_name(le):
    assert le._looks_like_entity_name("kitchen lights")
    for text in ("", "kitchen?", "one two three four five", "what is that",
                 "help", "your lights", "you there"):
        assert not le._looks_like_entity_name(text), text


def test_preserved_quirk_show_me_how_counts_as_a_name(le):
    # "show me how" is listed as an opener, but only first words are compared.
    assert le._looks_like_entity_name("show me how")


def test_follow_up_context(le, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(le.time, "time", lambda: clock["now"])
    le._update_ctx(entity="light.a", domain="light", action="")
    assert le._CTX.last_entity == "light.a" and le._ctx_fresh()
    clock["now"] += 119
    assert le._ctx_fresh()
    clock["now"] += 1
    assert not le._ctx_fresh()


def test_pick_avoids_the_last_few(load, monkeypatch):
    mod = load("local_engine")
    monkeypatch.setattr(mod, "_recent_acks", [])
    monkeypatch.setattr(mod.random, "choice", lambda seq: seq[0])
    assert mod._pick([]) == ""
    picks = [mod._pick(["a", "b", "c", "d", "e", "f"]) for _ in range(6)]
    assert picks == ["a", "b", "c", "d", "e", "a"]
    assert mod._recent_acks == ["c", "d", "e", "a"]
    assert mod._pick(["c"]) == "c"      # nothing fresh: any option


# ── finding a device ────────────────────────────────────────────────────────

def test_find_entity_strips_suffixes_and_uses_aliases(le):
    hass = FakeHass()
    hass.states.set("light.porch", "on", friendly_name="Porch")
    assert le._find_entity(hass, "porch light please") == ("light.porch", "Porch")
    assert le._find_entity(hass, "lights") is None
    assert le._find_entity(hass, "front", aliases={"front": "light.porch"}) == (
        "light.porch", "Porch")
    # an alias to something that no longer exists falls through to matching
    assert le._find_entity(hass, "porch", aliases={"porch": "light.gone"}) == (
        "light.porch", "Porch")


def test_find_entity_tiers(le):
    hass = FakeHass()
    hass.states.set("light.hall_ceiling", "on", friendly_name="Hall Ceiling Light")
    hass.states.set("switch.garden_pump", "off", friendly_name="Pump")
    hass.states.set("sensor.x", "1", friendly_name="Totally Different")
    assert le._find_entity(hass, "hall ceiling")[0] == "light.hall_ceiling"   # substring
    assert le._find_entity(hass, "ceiling hall")[0] == "light.hall_ceiling"   # words
    assert le._find_entity(hass, "garden pump", "switch") == ("switch.garden_pump", "Pump")
    assert le._find_entity(hass, "zzzz qqqq") is None


def test_find_entity_stt_correction(le):
    hass = FakeHass()
    hass.states.set("light.nightstand", "on", friendly_name="Nightstand")
    assert le._find_entity(hass, "night stand") == ("light.nightstand", "Nightstand")


def test_find_entity_near_tie_is_ambiguous(le):
    hass = FakeHass()
    hass.states.set("light.a", "on", friendly_name="Desk Lamp Left")
    hass.states.set("light.b", "on", friendly_name="Desk Lamp Right")
    found = le._find_entity(hass, "desk lamp", "light")
    assert isinstance(found, le._AmbiguousEntity)
    assert [c["entity_id"] for c in found.candidates] == ["light.a", "light.b"]


def test_ambiguous_candidates_are_unique(le):
    amb = le._AmbiguousEntity([{"entity_id": "a"}, {"entity_id": "a", "x": 1},
                               {"entity_id": "b"}])
    assert amb.candidates == [{"entity_id": "a"}, {"entity_id": "b"}]


def test_clarification_text(le):
    two = [{"entity_id": "light.a", "friendly_name": "Lamp"},
           {"entity_id": "light.b", "friendly_name": "Lamp"}]
    assert le._build_local_clarification(two, ", sir") == (
        "I found more than one match, sir — did you mean Lamp (light.a) or Lamp (light.b)?")
    assert le._build_local_clarification([{"entity_id": "x.y"}, {"entity_id": "x.z"}], "") == (
        "I found more than one match — did you mean x.y or x.z?")
    assert le._build_local_clarification([{"entity_id": "x.y"}], "") == (
        "I found more than one possible match — could you be more specific?")


class _Entry:
    def __init__(self, entity_id, area_id=None, device_id=None):
        self.entity_id, self.domain = entity_id, entity_id.split(".")[0]
        self.area_id, self.device_id = area_id, device_id


@pytest.fixture
def registries(monkeypatch):
    """Areas Kitchen and Hall; light.k1 by its own area, light.k2 through its
    device, light.h in the hall."""
    import homeassistant.helpers as helpers
    areas = [types.SimpleNamespace(id="kitchen", name="Kitchen"),
             types.SimpleNamespace(id="hall", name="Hall")]
    entries = {e.entity_id: e for e in (
        _Entry("light.k1", "kitchen"), _Entry("light.k2", None, "dev2"),
        _Entry("light.h", "hall"), _Entry("switch.k", "kitchen"),
        _Entry("light.nodev", None, "missing"))}
    devices = {"dev2": types.SimpleNamespace(area_id="kitchen")}
    reg = types.SimpleNamespace(areas=areas, entries=entries)

    def module(obj):
        return types.SimpleNamespace(async_get=lambda hass: obj)
    monkeypatch.setattr(helpers, "area_registry", module(
        types.SimpleNamespace(async_list_areas=lambda: reg.areas)), raising=False)
    monkeypatch.setattr(helpers, "entity_registry", module(
        types.SimpleNamespace(entities=reg.entries)), raising=False)
    monkeypatch.setattr(helpers, "device_registry", module(
        types.SimpleNamespace(async_get=devices.get)), raising=False)
    return reg


def _home_lights(hass):
    hass.states.set("light.k1", "on", friendly_name="Pendant")
    hass.states.set("light.k2", "off", friendly_name="Spots")
    hass.states.set("light.h", "on", friendly_name="Hall")
    hass.states.set("switch.k", "on", friendly_name="Kettle")


def test_find_entity_area_fallback(le, registries):
    hass = FakeHass()
    hass.states.set("light.h", "on", friendly_name="Zzz")
    assert le._find_entity(hass, "hall", "light") == ("light.h", "Zzz")
    hass.states.set("light.k1", "on", friendly_name="Qqq")
    hass.states.set("light.k2", "on", friendly_name="Www")
    found = le._find_entity(hass, "kitchen", "light")
    assert isinstance(found, le._AmbiguousEntity)
    assert {c["entity_id"] for c in found.candidates} == {"light.k1", "light.k2"}
    registries.areas = [types.SimpleNamespace(id="none", name="Kitchen"),
                        types.SimpleNamespace(id="hall", name="Kitchen Hall")]
    assert le._find_entity(hass, "kitchen", "light") == ("light.h", "Zzz")
    assert le._find_entity(hass, "kitchen", None) is None    # no hint, no fallback


def test_find_entities_in_area(le, registries, monkeypatch):
    hass = FakeHass()
    _home_lights(hass)
    assert le._find_entities_in_area(hass, "KITCH", "light") == [
        ("light.k1", "Pendant"), ("light.k2", "Spots")]
    assert le._find_entities_in_area(hass, "attic", "light") == []
    ef = sys.modules["jc.entity_filter"]
    monkeypatch.setattr(ef, "is_excluded", lambda h, eid: eid == "light.k1")
    assert le._find_entities_in_area(hass, "kitchen", "light") == [("light.k2", "Spots")]


def test_find_entities_in_area_survives_a_broken_registry(le, monkeypatch):
    import homeassistant.helpers as helpers
    monkeypatch.setattr(helpers, "area_registry", None, raising=False)
    assert le._find_entities_in_area(FakeHass(), "kitchen", "light") == []


def test_find_scene_or_script(le):
    hass = FakeHass()
    hass.states.set("scene.movie_night", "x", friendly_name="Movie Night")
    hass.states.set("script.wind_down", "off", friendly_name="Bedtime")
    assert le._find_scene_or_script(hass, "movie night scene") == (
        "scene.movie_night", "Movie Night", "scene")
    assert le._find_scene_or_script(hass, "wind down routine") == (
        "script.wind_down", "Bedtime", "script")
    assert le._find_scene_or_script(hass, "nothing") is None


# ── the confirmation pre check ──────────────────────────────────────────────

@pytest.mark.parametrize("action,eid,svc", [
    ("turn_on", "switch.k", ("switch", "turn_on")),
    ("toggle", "fan.f", ("fan", "toggle")),
    ("open", "cover.g", ("cover", "open_cover")),
    ("media_next", "", ("media_player", "media_next_track")),
    ("unmute", "", ("media_player", "volume_mute")),
    ("set_temp_named", "", ("climate", "set_temperature")),
    ("volume_set", "", ("media_player", "volume_set")),
    ("query_temp", "", None),
])
def test_service_for(le, action, eid, svc):
    assert le._service_for(action, eid) == svc


def test_needs_confirmation_asks_policy_with_the_device(le, env):
    env.answers = {"lock.f": False}
    hass = FakeHass()
    assert le._needs_confirmation(hass, "unlock", "lock.f", "sat") is True
    assert env.asked[-1] == ("lock", "unlock", "lock.f", "sat")
    assert le._needs_confirmation(hass, "turn_on", "light.a") is False
    assert env.asked[-1] == ("light", "turn_on", "light.a", "")
    assert le._needs_confirmation(hass, "query_temp", "sensor.t") is False
    assert le._needs_confirmation_domain(hass, "scene", "turn_on", "scene.s", "d") is False


def test_a_broken_policy_check_defers_anything_above_low(le, env):
    env.policy_error = RuntimeError("down")
    hass = FakeHass()
    assert le._needs_confirmation(hass, "turn_on", "light.a") is False
    env.classify = "high"
    assert le._needs_confirmation(hass, "unlock", "lock.f") is True
    env.classify = None
    assert le._needs_confirmation(hass, "turn_on", "light.a") is True


def test_no_policy_module_defers(le, monkeypatch):
    monkeypatch.setitem(sys.modules, "jc.policy", None)
    monkeypatch.delattr(sys.modules["jc"], "policy", raising=False)
    assert le._policy_requires_confirmation(FakeHass(), "light", "turn_on") is True


# ── carrying out one action ─────────────────────────────────────────────────

@pytest.mark.parametrize("action,eid,args,call", [
    ("turn_on", "switch.k", {}, ("switch", "turn_on", {"entity_id": "switch.k"})),
    ("toggle", "light.a", {}, ("light", "toggle", {"entity_id": "light.a"})),
    ("close", "cover.g", {}, ("cover", "close_cover", {"entity_id": "cover.g"})),
    ("dim", "light.a", {"brightness_pct": 30},
     ("light", "turn_on", {"entity_id": "light.a", "brightness_pct": 30})),
    ("dim", "light.a", {}, ("light", "turn_on", {"entity_id": "light.a"})),
    ("brighten", "light.a", {},
     ("light", "turn_on", {"entity_id": "light.a", "brightness_pct": 100})),
    ("mute", "media_player.tv", {},
     ("media_player", "volume_mute", {"entity_id": "media_player.tv",
                                      "is_volume_muted": True})),
    ("unmute", "media_player.tv", {},
     ("media_player", "volume_mute", {"entity_id": "media_player.tv",
                                      "is_volume_muted": False})),
    ("media_next", "media_player.tv", {},
     ("media_player", "media_next_track", {"entity_id": "media_player.tv"})),
    ("set_temp", "climate.h", {"temperature": 21},
     ("climate", "set_temperature", {"entity_id": "climate.h", "temperature": 21.0})),
    ("volume_set", "media_player.tv", {"volume_level": 40},
     ("media_player", "volume_set", {"entity_id": "media_player.tv", "volume_level": 0.4})),
    ("volume_set", "media_player.tv", {},
     ("media_player", "volume_set", {"entity_id": "media_player.tv", "volume_level": 0.5})),
])
async def test_execute_action_calls(le, env, action, eid, args, call):
    hass = FakeHass()
    assert await le._execute_action(hass, action, eid, args) is True
    assert hass.service_calls == [call]


async def test_execute_action_refusals_and_failures(le, env):
    hass = FakeHass()
    env.answers = {"lock.f": False}
    assert await le._execute_action(hass, "unlock", "lock.f", {}, device_id="d") is False
    assert env.asked[-1] == ("lock", "unlock", "lock.f", "d")
    assert await le._execute_action(hass, "set_temp", "climate.h", {}) is False
    assert await le._execute_action(hass, "query_temp", "sensor.t", {}) is False
    assert hass.service_calls == []

    async def boom(*a, **k):
        raise RuntimeError("down")
    hass.services.async_call = boom
    assert await le._execute_action(hass, "turn_on", "light.a", {}) is False


async def test_verified_turn_on_fast_domain(le, env, hass):
    env.checks["light.a"] = "on"
    hass.states.set("light.a", "off")
    status = await le._execute_action_verified(hass, "turn_on", "light.a", {}, "A")
    assert status == "verified"
    assert env.rows == [("control_device", "voice", {
        "domain": "light", "entity_id": "light.a", "requested_state": "turn_on"})]
    assert env.results == [("row1", "accepted", None), ("row1", "verified", None)]
    assert hass._tasks == []


async def test_unconfirmed_turn_off_is_retried_in_the_background(le, env, hass):
    assert await le._execute_action_verified(hass, "turn_off", "switch.k", {}, "K") == (
        "unverified")
    await _run(hass)
    assert env.verify == [("switch.k", "turn_off", "switch", "turn_off",
                           {"entity_id": "switch.k"}, "local_engine", "row1")]
    assert await le._execute_action_verified(hass, "turn_on", "media_player.tv", {}, "TV") == (
        "accepted")


async def test_toggle_checks_the_opposite_state(le, env, hass):
    hass.states.set("light.a", "on")
    env.checks["light.a"] = "off"
    assert await le._execute_action_verified(hass, "toggle", "light.a", {}, "A") == "verified"
    env.checks.clear()
    assert await le._execute_action_verified(hass, "toggle", "light.a", {}, "A") == "unverified"
    assert env.unverified[-1][:3] == ("light.a", "toggle", "local_engine")
    assert env.results[-1][1:] == ("unverified", "not_retried_toggle_could_reverse")
    hass.states.set("light.a", "unavailable")
    assert await le._execute_action_verified(hass, "toggle", "light.a", {}, "A") == "accepted"
    assert await le._execute_action_verified(hass, "toggle", "cover.g", {}, "G") == "accepted"


async def test_dim_and_brighten_check_the_level(le, env, hass):
    env.brightness = 40
    assert await le._execute_action_verified(
        hass, "dim", "light.a", {"brightness_pct": 40}, "A") == "verified"
    assert env.rows[-1][2]["requested_state"] == "40"
    env.brightness = None
    assert await le._execute_action_verified(hass, "brighten", "light.a", {}, "A") == (
        "unverified")
    assert "requested 100%" in env.unverified[-1][3]
    assert env.results[-1][1:] == ("unverified", "brightness_not_confirmed")


async def test_locks_and_covers_go_to_the_background_check(le, env, hass):
    assert await le._execute_action_verified(hass, "close", "cover.g", {}, "G") == "accepted"
    await _run(hass)
    assert env.verify[-1][:4] == ("cover.g", "close", "cover", "close_cover")


async def test_other_actions_are_accepted_and_errors_are_errors(le, env, hass):
    assert await le._execute_action_verified(
        hass, "set_temp", "climate.h", {"temperature": 20}, "H") == "accepted"
    assert env.rows[-1][2]["requested_state"] == "20"
    assert await le._execute_action_verified(
        hass, "volume_set", "media_player.tv", {"volume_level": 30}, "TV") == "accepted"
    assert env.rows[-1][2]["requested_state"] == "30"
    env.answers = {"lock.f": False}
    assert await le._execute_action_verified(hass, "unlock", "lock.f", {}, "F") == "error"
    assert env.results[-1][1:] == ("failed", "service_call_failed")


# ── replies ─────────────────────────────────────────────────────────────────

def test_failure_reply(le):
    assert le._resp("turn_on", "Lamp", False, h="sir") == (
        "I wasn't able to turn on Lamp, sir — there may be a connectivity issue.")
    assert le._resp("set_temp", "Hall", False, h="") == (
        "I wasn't able to set temp Hall — there may be a connectivity issue.")


def test_unverified_reply(le):
    assert le._resp("dim", "Lamp", True, {"brightness_pct": 30}, "", "unverified") == (
        "I sent the command to set Lamp to 30%, but I can't confirm the brightness "
        "reached that level yet.")
    assert le._resp("brighten", "Lamp", True, {}, "sir", "unverified") == (
        "I sent the command to set Lamp to 100%, sir, but I can't confirm the "
        "brightness reached that level yet.")
    assert le._resp("dim", "Lamp", True, None, "", "unverified").startswith(
        "I sent the command to set Lamp to ?%")
    assert le._resp("turn_off", "Lamp", True, {}, "", "unverified") == (
        "I sent the command to turn off Lamp, but I can't confirm it worked yet.")


@pytest.mark.parametrize("action,args,text", [
    ("turn_on", {}, "I've sent the command to turn on Lamp, sir."),
    ("toggle", {}, "I've sent the toggle command to Lamp, sir."),
    ("lock", {}, "Locking Lamp now, sir."),
    ("unlock", {}, "Unlocking Lamp now, sir."),
    ("open", {}, "Opening Lamp now, sir."),
    ("close", {}, "Closing Lamp now, sir."),
    ("set_temp", {"temperature": 20}, "I've sent the command to set the temperature to 20°, sir."),
    ("set_temp_named", {}, "I've sent the command to set Lamp to ?°, sir."),
    ("media_pause", {}, "I've sent the pause command, sir."),
    ("media_play", {}, "I've sent the play command, sir."),
    ("media_next", {}, "I've sent the next-track command, sir."),
    ("volume_up", {}, "I've sent the volume-up command, sir."),
    ("volume_down", {}, "I've sent the volume-down command, sir."),
    ("volume_set", {"volume_level": 30}, "I've sent the command to set the volume to 30%, sir."),
    ("mute", {}, "I've sent the mute command, sir."),
    ("unmute", {}, "I've sent the unmute command, sir."),
    ("dim", {}, "I've sent the dim command to Lamp, sir."),
])
def test_accepted_reply(le, action, args, text):
    assert le._resp(action, "Lamp", True, args, "sir", "accepted") == text


@pytest.mark.parametrize("action,args,text", [
    ("turn_on", {}, "Done. Lamp is on."),
    ("turn_off", {}, "Done. Lamp is off."),
    ("toggle", {}, "Done. Lamp toggled."),
    ("dim", {"brightness_pct": 20}, "Done. Lamp at 20%."),
    ("brighten", {}, "Done. Lamp at full brightness."),
    ("volume_up", {}, "Done. volume_up applied to Lamp."),
])
def test_verified_reply(le, action, args, text):
    assert le._resp(action, "Lamp", True, args, "", "verified") == text


def test_verified_reply_varies_its_opener(load, monkeypatch):
    mod = load("local_engine")
    offered = []
    monkeypatch.setattr(mod, "_pick", lambda options: offered.append(options) or "At once")
    assert mod._resp("turn_on", "Lamp", True, h="sir") == "At once, sir. Lamp is on."
    assert offered == [["Done", "Right away", "As you wish", "Consider it done", "At once"]]


def test_query_replies(le, monkeypatch):
    import datetime as dt
    monkeypatch.setattr(sys.modules["homeassistant.util.dt"], "now",
                        lambda tz=None: dt.datetime(2026, 3, 4, 15, 5))
    hass = FakeHass()
    hass.states.set("sensor.t", "21", unit_of_measurement="°C")
    hass.states.set("light.a", "on")
    assert le._query_resp(hass, "query_time", "", "") == "It's currently 03:05 PM, sir."
    assert le._query_resp(hass, "greeting", "", "", "") == "Good afternoon."
    assert le._query_resp(hass, "thanks", "", "", "sir") == "Of course, sir."
    assert le._query_resp(hass, "query_temp", "sensor.t", "Den") == (
        "The temperature in Den is currently 21°C, sir.")
    assert le._query_resp(hass, "query_temp", "sensor.x", "Den", "") == (
        "I'm unable to read the temperature for Den at the moment.")
    assert le._query_resp(hass, "query_state", "light.a", "Lamp", "") == (
        "Lamp is currently on.")
    assert le._query_resp(hass, "query_state", "light.x", "Lamp", "") == (
        "I'm unable to determine the status of Lamp at the moment.")
    assert le._query_resp(hass, "nonsense", "", "") == ""


@pytest.mark.parametrize("hour,greeting", [(9, "Good morning"), (12, "Good afternoon"),
                                           (18, "Good evening")])
def test_greeting_by_hour(le, monkeypatch, hour, greeting):
    import datetime as dt
    monkeypatch.setattr(sys.modules["homeassistant.util.dt"], "now",
                        lambda tz=None: dt.datetime(2026, 3, 4, hour, 0))
    assert le._query_resp(FakeHass(), "greeting", "", "", "") == f"{greeting}."


def test_home_status(le, env):
    hass = FakeHass()
    env.exempt = {"lock.shed"}
    hass.states.set("light.a", "on")
    hass.states.set("lock.f", "unlocked")
    hass.states.set("lock.shed", "unlocked")
    hass.states.set("binary_sensor.d", "on", device_class="door")
    hass.states.set("person.a", "home")
    hass.states.set("person.b", "home")
    assert le._query_resp(hass, "status", "", "", "sir") == (
        "All systems nominal, sir. 1 light on. 1 lock unlocked. 1 door open. 2 persons home.")
    hass.states.set("lock.f", "locked")
    hass.states.set("binary_sensor.d", "off")
    hass.states.set("light.a", "off")
    assert le._home_status(hass, "") == (
        "All systems nominal. 0 lights on. All locks secured. 2 persons home.")


# ── Home Assistant's own status ─────────────────────────────────────────────

def test_core_state_name(le):
    assert le._core_state_name(None) == "running"
    assert le._core_state_name(types.SimpleNamespace(name="RUNNING")) == "running"
    assert le._core_state_name(" Stopping ") == "stopping"
    assert le._core_state_name(types.SimpleNamespace(name=None, value="Final_Write")) == (
        "final_write")


def test_platform_status(le, monkeypatch):
    import homeassistant.const as const
    monkeypatch.setattr(const, "__version__", "2026.9.1", raising=False)
    hass = FakeHass()
    hass.states.set("light.a", "on")
    hass.state = "running"
    assert le._platform_status(hass, ", sir") == (
        "Home Assistant 2026.9.1 is running and responding, sir. I'm connected to it "
        "and can see 1 entity.")
    hass.states.set("light.b", "on")
    assert le._platform_status(hass, "").endswith("can see 2 entities.")
    hass.state = types.SimpleNamespace(name="final_write")
    assert le._platform_status(hass, "") == (
        "Home Assistant 2026.9.1 is shutting down right now, so some devices may not "
        "respond until it finishes.")
    hass.state = "some_state"
    assert "is some state right now" in le._platform_status(hass, "")
    monkeypatch.setattr(const, "__version__", "dev", raising=False)
    hass.state = "running"
    hass.states.async_entity_ids_count = lambda: 0
    assert le._platform_status(hass, "") == (
        "Home Assistant is running, but I can't see any entities right now.")


def test_platform_status_counts_without_the_fast_counter(le, monkeypatch):
    hass = FakeHass()
    hass.states.set("light.a", "on")

    def broken():
        raise AttributeError
    hass.states.async_entity_ids_count = broken
    assert "can see 1 entity" in le._platform_status(hass, "")

    def all_broken(*a):
        raise RuntimeError
    hass.states.async_all = all_broken
    assert "can't see any entities" in le._platform_status(hass, "")


# ── contextual questions ────────────────────────────────────────────────────

def _people(hass):
    hass.states.set("person.ann", "home", friendly_name="Ann")
    hass.states.set("person.bo", "not_home", friendly_name="Bo")
    hass.states.set("person.cy", "Work", friendly_name="Cy")
    hass.states.set("person.di", "unknown", friendly_name="Di")
    hass.states.set("person.ed_jones", "home")


def test_who_and_how_many_are_home(le):
    hass = FakeHass()
    assert le._ctx_query(hass, "who_home", "") == "No one appears to be home at the moment."
    assert le._ctx_query(hass, "count_home", "sir") == (
        "No one appears to be home at the moment, sir.")
    hass.states.set("person.ann", "home", friendly_name="Ann")
    assert le._ctx_query(hass, "who_home", "") == "Ann is currently home."
    assert le._ctx_query(hass, "count_home", "") == "One person is home: Ann."
    _people(hass)
    assert le._ctx_query(hass, "who_home", "") == "Ann and person.ed_jones are currently home."
    assert le._ctx_query(hass, "count_home", "") == (
        "2 people are home: Ann and person.ed_jones.")


@pytest.mark.parametrize("name,text", [
    ("ann", "Ann is home."), ("bo", "Bo is away."), ("cy", "Cy is at Work."),
    ("di", "I can't tell where Di is right now."), ("ed_jones", "person.ed_jones is home."),
    ("ed  jones", "person.ed_jones is home."), ("zed", None), ("", None),
])
def test_is_a_person_home(le, name, text):
    hass = FakeHass()
    _people(hass)
    assert le._ctx_query(hass, "person_home", "", name) == text


def test_what_is_open(le, env):
    hass = FakeHass()
    env.exempt = {"lock.shed"}
    assert le._ctx_query(hass, "what_open", "sir") == "Everything is closed and secured, sir."
    hass.states.set("binary_sensor.w", "on", device_class="window", friendly_name="Window")
    hass.states.set("binary_sensor.m", "on", device_class="motion")
    hass.states.set("cover.g", "open", friendly_name="Garage")
    hass.states.set("lock.f", "unlocked", friendly_name="Front")
    hass.states.set("lock.shed", "unlocked")
    assert le._ctx_query(hass, "what_open", "") == (
        "Currently open or unlocked: Window, Garage, Front (unlocked).")


def test_energy(le):
    hass = FakeHass()
    assert le._ctx_query(hass, "energy", "") == "I don't have a total power sensor configured."
    hass.states.set("sensor.house_total_power", "450", device_class="power")
    assert le._ctx_query(hass, "energy", "sir") == "Current power consumption is 450 W, sir."


def test_indoor_temperature_then_weather(le):
    hass = FakeHass()
    assert le._ctx_query(hass, "indoor_temp", "") == (
        "I don't have weather data available at the moment.")
    hass.states.set("sensor.outdoor_temp", "9", device_class="temperature",
                    unit_of_measurement="°C")
    assert le._ctx_query(hass, "weather", "") == "The outdoor temperature is 9°C."
    hass.states.set("weather.home", "partly_cloudy", temperature=11, humidity=80)
    assert le._ctx_query(hass, "indoor_temp", "sir") == (
        "Currently partly cloudy outside, sir. Temperature is 11° with 80% humidity.")
    for i, name in enumerate(("D", "C", "B", "A")):
        hass.states.set(f"climate.{name.lower()}", "heat", friendly_name=name,
                        current_temperature=20 + i)
    hass.states.set("climate.none", "off")
    assert le._ctx_query(hass, "indoor_temp", "") == "Inside it's A 23°, B 22°, C 21°."


def test_area_devices(le, registries):
    hass = FakeHass()
    _home_lights(hass)
    assert le._ctx_query(hass, "area_devices", "", "attic") == (
        "I couldn't find an area matching 'attic'.")
    assert le._ctx_query(hass, "area_devices", "sir", "kitchen") == (
        "Kitchen has 3 devices: Pendant (on), Spots (off), Kettle (on).")
    registries.entries.clear()
    assert le._ctx_query(hass, "area_devices", "sir", "hall") == (
        "No devices found in Hall, sir.")


def test_area_devices_lists_ten_then_counts_the_rest(le, registries):
    hass = FakeHass()
    registries.entries.clear()
    for i in range(12):
        registries.entries[f"light.h{i}"] = _Entry(f"light.h{i}", "hall")
        hass.states.set(f"light.h{i}", "on", friendly_name=f"L{i}")
    text = le._ctx_query(hass, "area_devices", "", "hall")
    assert text.startswith("Hall has 12 devices: L0 (on), ") and text.endswith(" and 2 more.")
    assert le._ctx_query(hass, "area_devices", "", "") is None
    assert le._ctx_query(hass, "unknown_kind", "") is None


def test_area_devices_with_a_broken_registry(le, monkeypatch):
    import homeassistant.helpers as helpers
    monkeypatch.setattr(helpers, "area_registry", None, raising=False)
    assert le._ctx_query(FakeHass(), "area_devices", "", "hall") is None


# ── try_local: routing ──────────────────────────────────────────────────────

async def test_complex_requests_go_to_the_agent(le, env):
    assert await le.try_local(FakeHass(), "explain why the heating is on") is None


async def test_contextual_question_is_answered(le, env):
    hass = FakeHass()
    hass.states.set("person.ann", "home", friendly_name="Ann")
    out = await le.try_local(hass, "who is home", honorific="")
    assert (out.text, out.success, out.handled) == ("Ann is currently home.", True, True)


async def test_unanswered_contextual_question_carries_on(le, env):
    out = await le.try_local(FakeHass(), "is zed home")
    assert out is None


async def test_read_only_request_never_acts(le, env):
    hass = FakeHass()
    hass.states.set("light.a", "on", friendly_name="Lamp")
    assert await le.try_local(hass, "turn off the lamp but don't change anything") is None
    assert await le.try_local(hass, "just list the lamp") is None
    assert hass.service_calls == []


async def test_turn_off_everything(le, env):
    hass = FakeHass()
    hass.states.set("light.a", "on")
    hass.states.set("switch.b", "on")
    hass.states.set("fan.c", "on")
    hass.states.set("fan.d", "off")
    env.answers = {"switch.b": False}
    out = await le.try_local(hass, "turn off everything", honorific="sir")
    assert out.text == "Done, sir. 2 devices turned off." and out.success
    assert env.rows[0] == ("bulk_control", "chat", [
        {"key": "light.a", "domain": "light", "service": "turn_off", "entity_id": "light.a"},
        {"key": "switch.b", "domain": "switch", "service": "turn_off",
         "entity_id": "switch.b"},
        {"key": "fan.c", "domain": "fan", "service": "turn_off", "entity_id": "fan.c"}])
    assert ("row-switch.b", "failed", "service_call_failed") in env.results
    assert ("row-fan.c", "accepted", None) in env.results


async def test_turn_off_everything_one_device(le, env):
    hass = FakeHass()
    hass.states.set("light.a", "on")
    out = await le.try_local(hass, "turn out everything", honorific="")
    assert out.text == "Done. 1 device turned off."


async def test_bulk_by_domain_and_by_area(le, env, registries):
    hass = FakeHass()
    _home_lights(hass)
    out = await le.try_local(hass, "turn on all the lights", honorific="")
    assert out.text == "Done. 3 lights turn on." and out.success
    assert env.rows[0][2][0] == {"key": "light.k1", "domain": "light",
                                 "entity_id": "light.k1"}
    out = await le.try_local(hass, "turn off the lights in the kitchen", honorific="sir")
    assert out.text == "Done, sir. 2 lights in kitchen turn off."
    env.answers = {"light.h": False}
    hass.service_calls.clear()
    out = await le.try_local(hass, "turn on lights in hall", honorific="")
    assert (out.text, out.success) == ("Done. 0 lights in hall turn on.", False)
    assert ("row-light.h", "failed", "service_call_failed") in env.results


async def test_bulk_with_nothing_to_act_on_moves_on(le, env):
    hass = FakeHass()
    hass.states.set("light.lamp", "on", friendly_name="Lamp")
    out = await le.try_local(hass, "close the blinds")
    assert out is None or "blinds" not in out.text


async def test_protected_bulk_defers(le, env):
    env.answers = {("lock", "unlock"): False}
    hass = FakeHass()
    hass.states.set("lock.f", "locked")
    assert await le.try_local(hass, "unlock all the doors", device_id="sat") is None
    assert env.asked[0] == ("lock", "unlock", "", "sat")
    assert hass.service_calls == []


async def test_scene_activation(le, env):
    hass = FakeHass()
    hass.states.set("scene.movie", "x", friendly_name="Movie")
    out = await le.try_local(hass, "activate the movie scene", honorific="sir")
    assert out.text == "Activating Movie now, sir." and out.success
    assert hass.service_calls == [("scene", "turn_on", {"entity_id": "scene.movie"})]
    assert env.rows == [("scene_activation", "chat", {
        "domain": "scene", "service": "turn_on", "entity_id": "scene.movie"})]
    assert env.results == [("row1", "accepted", None)] and env.not_checkable == ["row1"]
    assert le._CTX.last_entity == "scene.movie" and le._CTX.last_domain == "scene"


async def test_script_activation_failure_and_protection(le, env):
    hass = FakeHass()
    hass.states.set("script.bedtime", "off", friendly_name="Bedtime")

    async def boom(*a, **k):
        raise RuntimeError("busy")
    hass.services.async_call = boom
    out = await le.try_local(hass, "run bedtime", honorific="")
    assert (out.text, out.success) == ("I wasn't able to activate Bedtime. busy", False)
    assert env.rows[0][0] == "script_run"
    assert env.results == [("row1", "failed", "service_call_failed")]
    env.answers = {"script.bedtime": False}
    assert await le.try_local(hass, "run bedtime") is None


async def test_goodnight_scene(le, env):
    hass = FakeHass()
    hass.states.set("scene.goodnight", "x", friendly_name="Goodnight")
    out = await le.try_local(hass, "goodnight nova", honorific="sir")
    assert out.text == "Goodnight, sir. I've triggered Goodnight. Rest well."
    assert env.not_checkable == ["row1"]
    env.answers = {"scene.goodnight": False}
    assert await le.try_local(hass, "good night") is None


async def test_goodnight_sweep(le, env):
    hass = FakeHass()
    env.exempt = {"lock.shed"}
    hass.states.set("light.a", "on")
    hass.states.set("light.b", "off")
    hass.states.set("lock.front", "unlocked")
    hass.states.set("lock.back", "unlocked")
    hass.states.set("lock.side", "unlocked")
    hass.states.set("lock.ok", "unlocked")
    hass.states.set("lock.shed", "unlocked")
    env.answers = {
        "lock.back": _Verdict(False, "door_open", "The back door is open.", "deferred"),
        "lock.side": _Verdict(False, "already_secure", "Already locked."),
        "lock.ok": _Verdict(False, "", "", "denied"),
    }
    out = await le.try_local(hass, "goodnight", honorific="sir", device_id="sat")
    assert out.text == (
        "Goodnight, sir. I've sent the command to turn off 1 light and lock 1 lock. "
        "The back door is open. Ask me to lock it if you still want it locked. Rest well.")
    assert hass.service_calls == [("light", "turn_off", {"entity_id": "light.a"}),
                                  ("lock", "lock", {"entity_id": "lock.front"})]
    assert ("lock", "lock", "lock.back", "sat") in env.asked
    assert ("row-lock.back", "blocked", "door_open") in env.results
    assert ("row-lock.ok", "blocked", "confirmation_not_approved") in env.results
    assert ("row-lock.side", "blocked", "already_secure") in env.results
    await _run(hass)
    assert [v[:2] for v in env.verify] == [("light.a", "turn_off"), ("lock.front", "lock")]
    assert env.verify[0][-1] == "row-light.a"


async def test_goodnight_sweep_with_a_note_that_is_not_deferred(le, env):
    hass = FakeHass()
    hass.states.set("lock.front", "unlocked")
    env.answers = {"lock.front": _Verdict(False, "device_unreadable", "I can't read it.",
                                          "error")}
    out = await le.try_local(hass, "goodnight", honorific="")
    assert out.text == ("Goodnight. I've sent the command to turn off 0 lights and lock "
                        "0 locks. I can't read it. Rest well.")
    assert [r[2][0]["entity_id"] for r in env.rows] == ["lock.front"]


async def test_goodnight_sweep_failures(le, env):
    hass = FakeHass()
    hass.states.set("light.a", "on")
    hass.states.set("lock.front", "unlocked")

    async def boom(*a, **k):
        raise RuntimeError("down")
    hass.services.async_call = boom
    out = await le.try_local(hass, "goodnight", honorific="")
    assert "turn off 0 lights and lock 0 locks" in out.text
    assert ("row-light.a", "failed", "service_call_failed") in env.results
    assert ("row-lock.front", "failed", "service_call_failed") in env.results


async def test_goodnight_scene_failure_falls_back_to_the_sweep(le, env):
    hass = FakeHass()
    hass.states.set("scene.goodnight", "x", friendly_name="Goodnight")
    calls = []

    async def scene_fails(domain, service, data, blocking=False):
        calls.append(domain)
        if domain == "scene":
            raise RuntimeError("busy")
    hass.services.async_call = scene_fails
    out = await le.try_local(hass, "goodnight", honorific="")
    assert out.text.startswith("Goodnight. I've sent the command")
    assert env.results[0] == ("row1", "failed", "service_call_failed")


# ── try_local: single devices ───────────────────────────────────────────────

async def test_fixed_replies(le, env, monkeypatch):
    import datetime as dt
    monkeypatch.setattr(sys.modules["homeassistant.util.dt"], "now",
                        lambda tz=None: dt.datetime(2026, 3, 4, 9, 30))
    hass = FakeHass()
    assert (await le.try_local(hass, "what time is it", honorific="")).text == (
        "It's currently 09:30 AM.")
    assert (await le.try_local(hass, "hello nova", honorific="")).text == "Good morning."
    assert (await le.try_local(hass, "thank you", honorific="")).text == "Of course."
    assert (await le.try_local(hass, "house status", honorific="")).text.startswith(
        "All systems nominal.")


async def test_switch_on_a_light(le, env):
    hass = FakeHass()
    hass.states.set("light.lamp", "off", friendly_name="Lamp")
    env.checks["light.lamp"] = "on"
    out = await le.try_local(hass, "turn on the lamp light", honorific="")
    assert (out.text, out.success) == ("Done. Lamp is on.", True)
    assert le._CTX.last_entity == "light.lamp" and le._CTX.last_action == "turn_on"


async def test_dim_and_set_temperature_args(le, env):
    hass = FakeHass()
    hass.states.set("light.lamp", "on", friendly_name="Lamp")
    hass.states.set("climate.hall", "heat", friendly_name="Hall")
    env.brightness = 30
    out = await le.try_local(hass, "dim the lamp to 30", honorific="")
    assert out.text == "Done. Lamp at 30%."
    out = await le.try_local(hass, "set the hall temperature to 21", honorific="")
    assert out.text == "I've sent the command to set Hall to 21°."
    assert hass.service_calls[-1] == ("climate", "set_temperature",
                                      {"entity_id": "climate.hall", "temperature": 21.0})


async def test_state_question_by_name(le, env):
    hass = FakeHass()
    hass.states.set("light.lamp", "on", friendly_name="Lamp")
    out = await le.try_local(hass, "is the lamp on", honorific="")
    assert out.text == "Lamp is currently on."


async def test_protected_single_action_defers(le, env):
    hass = FakeHass()
    hass.states.set("lock.front", "locked", friendly_name="Front")
    env.answers = {"lock.front": False}
    assert await le.try_local(hass, "unlock the front door", device_id="sat") is None
    assert env.asked[-1] == ("lock", "unlock", "lock.front", "sat")
    assert hass.service_calls == []


async def test_an_ambiguous_device_gets_a_question(le, env):
    hass = FakeHass()
    hass.states.set("light.a", "off", friendly_name="Lamp")
    hass.states.set("light.b", "off", friendly_name="Lamp")
    out = await le.try_local(hass, "turn on the lamp", honorific="")
    assert out.success is False and out.text.startswith("I found more than one match")
    assert hass.service_calls == []


async def test_an_unknown_device_goes_to_the_agent_or_says_so_offline(le, env):
    hass = FakeHass()
    assert await le.try_local(hass, "turn on the zorbulator") is None
    out = await le.try_local(hass, "turn on the zorbulator", honorific="sir", force=True)
    assert out.success is False
    assert out.text == ("I couldn't find a device matching 'zorbulator', sir, and I'm "
                        "offline so I can't do a deeper search right now. Try the exact "
                        "device name.")


async def test_repeat_with_nothing_to_repeat(le, env, load, monkeypatch):
    monkeypatch.setattr(load("spoken_history"), "get_last", lambda: None)
    out = await le.try_local(FakeHass(), "say that again")
    assert (out.text, out.success, out.repeat_of_id) == (
        "I don't have a recent spoken message to repeat.", False, None)


# ── try_local: the rest ─────────────────────────────────────────────────────

@pytest.fixture
def appliances(load, monkeypatch):
    mod = load("appliance_monitor")
    state = {"running": True, "sensors": {}}
    monkeypatch.setattr(mod, "is_running", lambda: state["running"])
    monkeypatch.setattr(mod, "status", lambda: {"sensors": state["sensors"]})
    return state


async def test_appliance_questions(le, env, appliances, monkeypatch):
    hass = FakeHass()
    ask = "let me know when the laundry is done"
    appliances["sensors"] = {"a": {"friendly_name": "Washer", "phase": "washing"}}
    assert (await le.try_local(hass, ask, honorific="")).text == (
        "Already on it. I'm monitoring Washer (washing). I'll announce when the cycle "
        "completes.")
    appliances["sensors"] = {}
    assert (await le.try_local(hass, ask, honorific="")).text.startswith(
        "I'm monitoring for appliance cycles, but I haven't found any power sensors")
    appliances["running"] = False
    assert (await le.try_local(hass, ask, honorific="sir")).text.startswith(
        "The appliance monitor isn't running at the moment, sir.")

    def boom():
        raise RuntimeError
    monkeypatch.setattr(sys.modules["jc.appliance_monitor"], "is_running", boom)
    assert (await le.try_local(hass, ask, honorific="")).text == (
        "I'll keep an eye on it. If I have a power sensor for that appliance, I'll "
        "announce when the cycle finishes.")


async def test_follow_up_strips_the_lead_in(le, env, monkeypatch):
    hass = FakeHass()
    hass.states.set("scene.goodnight", "x", friendly_name="Goodnight")
    le._update_ctx(entity="light.a")
    seen = []
    real = le.try_local

    async def spy(h, text, honorific="sir", force=False, device_id=None):
        seen.append((text, honorific, force, device_id))
        return await real(h, text, honorific, force=force, device_id=device_id)
    monkeypatch.setattr(le, "try_local", spy)
    out = await real(hass, "now goodnight", honorific="", force=True, device_id="sat")
    assert out.text == "Goodnight. I've triggered Goodnight. Rest well."
    assert seen == [("goodnight", "", True, "sat")]
    assert await real(hass, "also nothing here") is None


async def test_follow_up_only_while_fresh(le, env):
    hass = FakeHass()
    hass.states.set("light.lamp", "on", friendly_name="Lamp")
    le._CTX.last_ts = 0.0
    assert await le.try_local(hass, "also lamp sideways") is None


async def test_a_bare_device_name_reads_its_state(le, env):
    hass = FakeHass()
    hass.states.set("light.porch", "on", friendly_name="Porch")
    out = await le.try_local(hass, "porch", honorific="")
    assert out.text == "Porch is currently on."
    hass.states.set("light.porch2", "on", friendly_name="Porch")
    out = await le.try_local(hass, "porch", honorific="")
    assert out.success is False and "more than one" in out.text


async def test_nothing_matched(le, env):
    assert await le.try_local(FakeHass(), "what are your capabilities") is None
