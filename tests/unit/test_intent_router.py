"""The local intent router, from its behaviour spec (7c).

Phrase matching and the yes check are pure. The router itself is driven
against FakeHass with small stand ins for policy, the action log, the
decision log and the alert path, so each rule is checked on its own. The
end to end voice paths stay covered by test_pin_intent_router,
test_stage_c_verify, test_linked_logs and test_authority.
"""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass


@pytest.fixture
def ir(load):
    return load("intent.intent_router")


# ── matching ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("phrase,intent", [
    ("secure the garage", "secure_area"),
    ("please close the garage door", "secure_area"),
    ("lock up for the night", "secure_area"),
    ("lock it", "secure_area"),
    ("turn off the lights", "lights_off"),
    ("shut off the kitchen light", "lights_off"),
    ("light off", "lights_off"),
    ("kill the lights", "lights_off"),
    ("switch on the light", "lights_on"),
    ("lights on", "lights_on"),
    ("turn it off", "context_off"),
    ("switch it off", "context_off"),
    ("turn that off", "context_off"),
    ("pause it", "context_off"),
    ("close it", "context_close"),
    ("shut it", "context_close"),
    ("arm the house", "arm_alarm"),
    ("arm alarm", "arm_alarm"),
    ("what's on tonight", None),
    ("", None),
])
def test_match_intent(ir, phrase, intent):
    found = ir.match_intent(phrase)
    if intent is None:
        assert found is None
    else:
        assert found == {"intent": intent, "raw": phrase}


def test_a_lights_phrase_is_not_taken_as_a_pronoun(ir):
    # "turn off the light" also contains "turn ... off"; the table order wins.
    assert ir.match_intent("Turn OFF the lights  ")["intent"] == "lights_off"


@pytest.mark.parametrize("phrase", [
    "is the garage secure", "secure the garage?", "why is the light on",
    "don't secure the garage", "do not turn off the lights", "dont kill the lights",
    "never lock up", "cancel, lights off", "stop, close it", "not now, lights on",
])
def test_questions_and_negations_never_match(ir, phrase):
    assert ir.match_intent(phrase) is None


@pytest.mark.parametrize("phrase,yes", [
    ("yes", True), ("Yes please", True), ("close it", True), ("yes secure it", True),
    ("no", False), ("no, close it", False), ("don't", False),
    ("lights on", False), ("", False), (None, False),
])
def test_is_affirmative(ir, phrase, yes):
    assert ir.is_affirmative(phrase) is yes


async def test_the_intent_package_setup_hook_does_nothing(load):
    pkg = load("intent")
    assert await pkg.async_setup_intents(object()) is None
    assert {"LocalIntentRouter", "match_intent", "is_affirmative",
            "EVENT_FEEDBACK_WINDOW", "async_setup_intents"} <= set(pkg.__all__)


# ── stand ins ───────────────────────────────────────────────────────────────

class _Decision:
    def __init__(self, allowed, code=""):
        self.allowed, self.code = allowed, code


@pytest.fixture
def policy(monkeypatch):
    """jc.policy whose answer per entity is set by the test."""
    mod = types.ModuleType("jc.policy")
    mod.SOURCE_VOICE = "voice"
    mod.answers = {}
    mod.asked = []

    class AuthorityRequest:
        def __init__(self, domain, service, entity_id, *, source, user_id):
            self.args = (domain, service, entity_id, source, user_id)

    def authorize_now(hass, req):
        mod.asked.append(req.args)
        answer = mod.answers.get(req.args[2], True)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, _Decision) else _Decision(answer)
    mod.AuthorityRequest = AuthorityRequest
    mod.authorize_now = authorize_now
    monkeypatch.setitem(sys.modules, "jc.policy", mod)
    return mod


@pytest.fixture
def log(monkeypatch):
    """jc.action_log recording requests and execution results."""
    mod = types.ModuleType("jc.action_log")
    mod.started, mod.results = [], []
    mod.new_request_id = lambda: "req-1"

    def start_many(request_id, intent, source, rows, requested_by_user_id=None):
        mod.started.append((request_id, intent, source, rows, requested_by_user_id))
        return {row["entity_id"]: f"row-{row['entity_id']}" for row in rows}

    def set_execution(row_id, result, reason_code=None):
        mod.results.append((row_id, result, reason_code))
    mod.start_many = start_many
    mod.set_execution = set_execution
    monkeypatch.setitem(sys.modules, "jc.action_log", mod)
    return mod


@pytest.fixture
def secure_check(monkeypatch):
    """The modules the after action check reaches: the state check, the
    decision log, and the alert path for anything left unsecured."""
    seen = {"decisions": [], "alerts": [], "secure": set()}
    verify = types.ModuleType("jc.entity_verify")
    verify.check_state_once = lambda hass, eid, expected: (
        seen.setdefault("expected", expected) and eid in seen["secure"])
    ap = types.ModuleType("jc.alert_path")
    ap.situation = lambda hass: "sit"

    async def record(hass, kind, **kw):
        seen["decisions"].append((kind, kw))
    ap.async_record_decision = record
    core_state = types.ModuleType("jc.core_state")
    core_state._CORE = types.SimpleNamespace(config={"honorific": "x"})
    bridge = types.ModuleType("jc.core_bridge")

    async def emit(hass, cfg, action, flag):
        seen["alerts"].append((cfg, action, flag))
    bridge._emit_action = emit
    for name, mod in (("entity_verify", verify), ("alert_path", ap),
                      ("core_state", core_state), ("core_bridge", bridge)):
        monkeypatch.setitem(sys.modules, f"jc.{name}", mod)
    return seen


class _Mutex:
    def __init__(self, busy=()):
        self.busy, self.taken, self.released = set(busy), [], []

    def try_acquire(self, eid, priority):
        if eid in self.busy:
            return None
        self.taken.append((eid, priority))
        return f"tok-{eid}"

    def release(self, token):
        self.released.append(token)


def _router(ir, hass, areas, **kw):
    r = ir.LocalIntentRouter(hass, **kw)
    r._area_of = areas.get
    r.verify_delay = 0
    return r


# ── which entity "it" means ─────────────────────────────────────────────────

def test_resolve_active_entity_order_and_area(ir):
    hass = FakeHass()
    hass.states.set("media_player.tv", "paused")
    hass.states.set("light.lamp", "on")
    hass.states.set("light.other", "on")
    hass.states.set("cover.blind", "open")
    hass.states.set("switch.fan", "on")
    areas = {"media_player.tv": "den", "light.lamp": "den", "light.other": "hall",
             "cover.blind": "den", "switch.fan": "den"}
    r = ir.LocalIntentRouter(hass)
    assert r.resolve_active_entity("den", area_of=areas.get) == ("light.lamp", "light")
    hass.states.set("media_player.tv", "playing")
    assert r.resolve_active_entity("den", area_of=areas.get) == ("media_player.tv",
                                                                 "media_player")
    assert r.resolve_active_entity("den", ("cover",), area_of=areas.get) == (
        "cover.blind", "cover")
    assert r.resolve_active_entity("den", ("switch",), area_of=areas.get) == (
        "switch.fan", "switch")
    hass.states.set("media_player.tv", "on")    # "on" is not active for media
    hass.states.set("light.lamp", "off")
    assert r.resolve_active_entity("den", area_of=areas.get) == (None, None)


def test_resolve_skips_an_entity_whose_area_lookup_fails(ir):
    hass = FakeHass()
    hass.states.set("light.bad", "on")
    hass.states.set("light.good", "on")

    def area_of(eid):
        if eid == "light.bad":
            raise RuntimeError("registry")
        return "den"
    assert ir.LocalIntentRouter(hass).resolve_active_entity(
        "den", ("light",), area_of=area_of) == ("light.good", "light")


def test_resolve_uses_the_router_area_lookup_by_default(ir):
    hass = FakeHass()
    hass.states.set("light.lamp", "on")
    r = ir.LocalIntentRouter(hass)
    r._area_of = {"light.lamp": "den"}.get
    assert r.resolve_active_entity("den") == ("light.lamp", "light")


# ── authority ───────────────────────────────────────────────────────────────

def test_authorized_splits_by_policy_and_keeps_the_reason(ir, policy):
    policy.answers = {"lock.a": _Decision(False, "already_secure"),
                      "lock.b": _Decision(False, ""),
                      "lock.c": RuntimeError("boom")}
    r = ir.LocalIntentRouter(FakeHass())
    allowed, held, why = r._authorized("lock", "lock", ["lock.a", "lock.b", "lock.c",
                                                        "lock.d"], "u1")
    assert allowed == ["lock.d"]
    assert held == ["lock.a", "lock.b", "lock.c"]
    assert why == {"lock.a": "already_secure"}
    assert policy.asked[0] == ("lock", "lock", "lock.a", "voice", "u1")
    r._authorized("lock", "lock", ["lock.a"])
    assert policy.asked[-1][-1] == ""


def test_authorized_fails_closed_without_policy(ir, monkeypatch):
    monkeypatch.setitem(sys.modules, "jc.policy", None)
    monkeypatch.delattr(sys.modules["jc"], "policy", raising=False)
    r = ir.LocalIntentRouter(FakeHass())
    assert r._authorized("lock", "lock", ["lock.a"]) == ([], ["lock.a"], {})


# ── the action log ──────────────────────────────────────────────────────────

async def test_log_start_rows_for_allowed_and_held(ir, log):
    r = ir.LocalIntentRouter(FakeHass())
    rows = await r._log_start("secure_area", "lock", "lock", ["lock.a"],
                              ["lock.b", "lock.c", "lock.d"], "u1",
                              {"lock.c": "already_secure", "lock.d": "door_open"})
    assert rows == {"lock.a": "row-lock.a", "lock.b": "row-lock.b",
                    "lock.c": "row-lock.c", "lock.d": "row-lock.d"}
    assert rows.request_id == "req-1"
    (rid, intent, source, sent, uid), = log.started
    assert (rid, intent, source, uid) == ("req-1", "voice_reply:secure_area", "voice", "u1")
    by_id = {row["entity_id"]: row for row in sent}
    assert by_id["lock.a"] == {"key": "lock.a", "domain": "lock", "service": "lock",
                               "entity_id": "lock.a"}
    assert by_id["lock.b"]["approval_required"] is True
    assert by_id["lock.b"]["approval_result"] == "not_requested"
    assert by_id["lock.b"]["reason_code"] == "confirmation_unavailable_in_voice_reply"
    assert by_id["lock.c"]["approval_required"] is False
    assert by_id["lock.c"]["approval_result"] == "not_required"
    assert by_id["lock.c"]["reason_code"] == "already_secure"
    assert by_id["lock.d"]["approval_required"] is True
    assert by_id["lock.d"]["reason_code"] == "door_open"
    assert all(row["execution_result"] == "blocked" for row in sent[1:])


async def test_log_start_with_nothing_or_a_broken_log(ir, log, monkeypatch):
    r = ir.LocalIntentRouter(FakeHass())
    empty = await r._log_start("x", "light", "turn_on", [], [])
    assert empty == {} and empty.request_id is None and log.started == []

    def boom(*a, **k):
        raise RuntimeError("db")
    monkeypatch.setattr(log, "start_many", boom)
    out = await r._log_start("x", "light", "turn_on", ["light.a"], [])
    assert out == {} and out.request_id is None
    await r._log_start("x", "light", "turn_on", ["light.a"], [], None)
    assert log.started == []


async def test_log_start_without_a_user_sends_none(ir, log):
    await ir.LocalIntentRouter(FakeHass())._log_start("x", "light", "turn_on",
                                                      ["light.a"], [], "")
    assert log.started[0][4] is None


async def test_log_set_marks_only_known_rows(ir, log, monkeypatch):
    r = ir.LocalIntentRouter(FakeHass())
    await r._log_set({"a": 1, "b": 2}, ["a", "c"], "accepted")
    await r._log_set({"b": 2}, ["b"], "blocked", "entity_busy")
    assert log.results == [(1, "accepted", None), (2, "blocked", "entity_busy")]
    monkeypatch.setattr(log, "set_execution", lambda *a, **k: 1 / 0)
    await r._log_set({"a": 1}, ["a"], "accepted")      # never raises


# ── acting on a whole area ──────────────────────────────────────────────────

AREAS = {"light.k1": "kitchen", "light.k2": "kitchen", "light.h": "hall",
         "lock.k": "kitchen", "cover.k": "kitchen"}


@pytest.fixture
def home():
    hass = FakeHass()
    for eid in ("light.k1", "light.k2", "light.h"):
        hass.states.set(eid, "on")
    hass.states.set("lock.k", "unlocked")
    hass.states.set("cover.k", "open")
    yield hass
    for coro in hass._tasks:        # checks scheduled but not awaited here
        coro.close()


async def test_area_call_acts_on_allowed_entities_in_the_area(ir, home, policy, log):
    policy.answers = {"light.k2": False}
    r = _router(ir, home, AREAS)
    acted = await r._call_domain_in_area("light", "turn_off", "kitchen",
                                         intent="lights_off", user_id="u")
    assert acted == ["light.k1"]
    assert home.service_calls == [("light", "turn_off", {"entity_id": ["light.k1"]})]
    assert ("row-light.k1", "accepted", None) in log.results
    assert log.started[0][1] == "voice_reply:lights_off"


async def test_area_call_names_its_log_entry_when_no_intent_is_given(ir, home, policy, log):
    await _router(ir, home, AREAS)._call_domain_in_area("light", "turn_off", "kitchen")
    assert log.started[0][1] == "voice_reply:light.turn_off"


async def test_area_call_with_nothing_in_the_area(ir, home, policy, log):
    assert await _router(ir, home, AREAS)._call_domain_in_area(
        "light", "turn_on", "attic") == []
    assert log.started == [] and home.service_calls == []


async def test_area_call_skips_entities_whose_area_lookup_fails(ir, home, policy, log):
    def area_of(eid):
        if eid == "light.k1":
            raise RuntimeError("registry")
        return AREAS.get(eid)
    r = _router(ir, home, AREAS)
    r._area_of = area_of
    assert await r._call_domain_in_area("light", "turn_off", "kitchen") == ["light.k2"]


async def test_already_secure_counts_without_a_call(ir, home, policy, log):
    policy.answers = {"lock.k": _Decision(False, "already_secure")}
    acted = await _router(ir, home, AREAS)._call_domain_in_area("lock", "lock", "kitchen")
    assert acted == ["lock.k"] and home.service_calls == []


async def test_area_call_skips_busy_entities(ir, home, policy, log):
    mutex = _Mutex(busy={"light.k2"})
    r = _router(ir, home, AREAS, mutex=mutex)
    acted = await r._call_domain_in_area("light", "turn_off", "kitchen", priority=5)
    assert acted == ["light.k1"]
    assert mutex.taken == [("light.k1", 5)] and mutex.released == ["tok-light.k1"]
    assert ("row-light.k2", "blocked", "entity_busy") in log.results


async def test_area_call_without_a_priority_ignores_the_mutex(ir, home, policy, log):
    mutex = _Mutex(busy={"light.k1", "light.k2"})
    r = _router(ir, home, AREAS, mutex=mutex)
    assert await r._call_domain_in_area("light", "turn_off", "kitchen") == [
        "light.k1", "light.k2"]
    assert mutex.taken == []


async def test_preserved_quirk_all_busy_drops_the_already_secure(ir, home, policy, log):
    # When every allowed entity is busy, nothing is reported, not even the
    # ones that were already secure.
    home.states.set("lock.k2", "locked")
    areas = dict(AREAS, **{"lock.k2": "kitchen"})
    policy.answers = {"lock.k2": _Decision(False, "already_secure")}
    r = _router(ir, home, areas, mutex=_Mutex(busy={"lock.k"}))
    assert await r._call_domain_in_area("lock", "lock", "kitchen", priority=5) == []
    assert ("row-lock.k", "blocked", "entity_busy") in log.results


async def test_a_failed_call_logs_releases_and_reports_nothing(ir, home, policy, log):
    async def boom(*a, **k):
        raise RuntimeError("zigbee")
    home.services.async_call = boom
    mutex = _Mutex()
    r = _router(ir, home, AREAS, mutex=mutex)
    assert await r._call_domain_in_area("light", "turn_off", "kitchen", priority=1) == []
    assert ("row-light.k1", "failed", "service_call_failed") in log.results
    assert sorted(mutex.released) == ["tok-light.k1", "tok-light.k2"]


async def test_high_stakes_is_written_ahead_and_completed(ir, home, policy, log):
    events = []

    class Ledger:
        def record_intent(self, eid, desired, action):
            events.append(("intent", eid, desired, action))
            if eid == "lock.bad":
                raise RuntimeError("disk")
            return f"t-{eid}"

        def mark_complete(self, txn):
            events.append(("done", txn))
    home.states.set("lock.bad", "unlocked")
    areas = dict(AREAS, **{"lock.bad": "kitchen"})
    r = _router(ir, home, areas, ledger=Ledger())
    await r._call_domain_in_area("lock", "lock", "kitchen", high_stakes=True,
                                 desired_state="locked")
    assert events == [("intent", "lock.k", "locked", "lock.lock"),
                      ("intent", "lock.bad", "locked", "lock.lock"),
                      ("done", "t-lock.k")]
    events.clear()
    await r._call_domain_in_area("lock", "lock", "kitchen", high_stakes=True)
    assert events == []         # no desired state, nothing written


async def test_a_ledger_that_cannot_complete_is_ignored(ir, home, policy, log):
    class Ledger:
        def record_intent(self, *a, **k):
            return "t"

        def mark_complete(self, txn):
            raise RuntimeError("disk")
    r = _router(ir, home, AREAS, ledger=Ledger())
    assert await r._call_domain_in_area("lock", "lock", "kitchen", high_stakes=True,
                                        desired_state="locked") == ["lock.k"]


async def test_release_all_tolerates_a_broken_release(ir):
    class Bad(_Mutex):
        def release(self, token):
            raise RuntimeError("gone")
    ir.LocalIntentRouter(FakeHass(), mutex=Bad())._release_all({"a": 1})
    ir.LocalIntentRouter(FakeHass())._release_all({"a": 1})


# ── the check after securing ────────────────────────────────────────────────

async def test_only_locks_and_covers_are_checked(ir, home):
    started = []
    home.async_create_task = lambda coro: (started.append(coro), coro.close())
    r = ir.LocalIntentRouter(home)
    r._schedule_check("x", "light", ["light.a"], {}, None)
    r._schedule_check("x", "lock", [], {}, None)
    assert started == []
    r._schedule_check("x", "cover", ["cover.a"], {}, None)
    assert len(started) == 1


async def test_scheduling_a_check_never_raises(ir, home):
    def boom(coro):
        coro.close()
        raise RuntimeError("loop closed")
    home.async_create_task = boom
    ir.LocalIntentRouter(home)._schedule_check("x", "lock", ["lock.a"], {}, None)


async def test_check_secured_logs_records_and_alerts(ir, home, log, secure_check):
    home.states.set("lock.b", "unlocked", friendly_name="Back door")
    secure_check["secure"] = {"lock.a"}
    rows = ir._Rows({"lock.a": 1, "lock.b": 2, "lock.c": 3})
    rows.request_id = "req-9"
    r = ir.LocalIntentRouter(home)
    r.verify_delay = 0
    await r._check_secured("secure_area", "lock", ["lock.a", "lock.b", "lock.c"], rows, "u")
    assert secure_check["expected"] == "locked"
    assert log.results == [(1, "verified", None), (2, "unverified", "not_secure_after_check"),
                           (3, "unverified", "not_secure_after_check")]
    (kind, kw), = secure_check["decisions"]
    assert kind == "voice_secure"
    assert kw == {"source": "voice_reply", "sit": "sit",
                  "facts": {"intent": "secure_area", "secured": ["lock.a"],
                            "not_secured_after_check": ["lock.b", "lock.c"]},
                  "assessment": "asked by voice to secure the room",
                  "decision": "checked, some not secured",
                  "reason": "the voice reply's check after acting",
                  "request_ids": ["req-9"]}
    (cfg, action, flag), = secure_check["alerts"]
    assert cfg == {"honorific": "x"} and flag is False
    assert action == {"type": "voice_secure_failed", "urgency": "high",
                      "message": "I tried to secure Back door, lock.c, but it is not "
                                 "secured yet. Please check it.",
                      "entity_id": "lock.b", "auto_act": True}


async def test_check_secured_all_good_raises_nothing(ir, home, log, secure_check):
    secure_check["secure"] = {"cover.a"}
    r = ir.LocalIntentRouter(home)
    r.verify_delay = 0
    await r._check_secured("context_close", "cover", ["cover.a"], {"cover.a": 1}, None)
    assert secure_check["expected"] == "closed"
    (_kind, kw), = secure_check["decisions"]
    assert kw["decision"] == "checked, secured" and kw["request_ids"] == [None]
    assert secure_check["alerts"] == []


async def test_check_secured_never_raises(ir, home, monkeypatch):
    monkeypatch.setitem(sys.modules, "jc.entity_verify", None)
    r = ir.LocalIntentRouter(home)
    r.verify_delay = 0
    await r._check_secured("x", "lock", ["lock.a"], {}, None)


# ── execute ─────────────────────────────────────────────────────────────────

async def test_execute_lights_and_secure(ir, home, policy, log):
    started = []
    home.async_create_task = lambda coro: (started.append(coro), coro.close())
    r = _router(ir, home, AREAS)
    assert await r.execute({"intent": "lights_on"}, "hall") == {
        "executed": True, "intent": "lights_on", "entities": ["light.h"]}
    assert await r.execute({"intent": "lights_off"}, "attic") == {
        "executed": False, "intent": "lights_off", "entities": []}
    out = await r.execute({"intent": "secure_area"}, "kitchen", user_id="u")
    assert out == {"executed": True, "intent": "secure_area",
                   "entities": ["cover.k", "lock.k"]}
    assert home.service_calls[-2:] == [
        ("cover", "close_cover", {"entity_id": ["cover.k"]}),
        ("lock", "lock", {"entity_id": ["lock.k"]})]
    assert len(started) == 2        # one check for covers, one for locks


async def test_execute_uses_the_intent_priority_with_a_mutex(ir, home, policy, log, load):
    mutex = _Mutex()
    r = _router(ir, home, AREAS, mutex=mutex)
    await r.execute({"intent": "lights_off"}, "hall")
    assert mutex.taken == [("light.h", load("automation.mutex").Priority.INTENT)]


async def test_execute_refuses_arming_and_unknown_intents(ir, home):
    r = _router(ir, home, AREAS)
    out = await r.execute({"intent": "arm_alarm"}, "hall")
    assert out["executed"] is False and "not available" in out["reason"]
    assert await r.execute({"intent": "dance"}, "hall") == {
        "executed": False, "intent": "dance", "reason": "unhandled intent"}
    assert home.service_calls == []


async def test_context_off_turns_off_the_active_thing(ir, home, policy, log):
    started = []
    home.async_create_task = lambda coro: (started.append(coro), coro.close())
    r = _router(ir, home, AREAS)
    out = await r.execute({"intent": "context_off"}, "hall", user_id="u")
    assert out == {"executed": True, "intent": "context_off", "entity_id": "light.h"}
    assert home.service_calls == [("light", "turn_off", {"entity_id": "light.h"})]
    assert ("row-light.h", "accepted", None) in log.results
    assert started == []            # lights are not checked afterwards


async def test_context_close_closes_a_cover_and_checks_it(ir, home, policy, log):
    started = []
    home.async_create_task = lambda coro: (started.append(coro), coro.close())
    r = _router(ir, home, AREAS)
    out = await r.execute({"intent": "context_close"}, "kitchen")
    assert out == {"executed": True, "intent": "context_close", "entity_id": "cover.k"}
    assert home.service_calls == [("cover", "close_cover", {"entity_id": "cover.k"})]
    assert len(started) == 1


async def test_context_with_nothing_active(ir, home, policy):
    out = await _router(ir, home, AREAS).execute({"intent": "context_close"}, "hall")
    assert out == {"executed": False, "intent": "context_close",
                   "reason": "nothing active to act on in this area"}


@pytest.mark.parametrize("code,reason", [
    ("already_secure", "it is already closed"),
    ("device_unreadable", "it can't be read right now"),
    ("door_open", "needs a confirmation the voice reply cannot ask for"),
    ("", "needs a confirmation the voice reply cannot ask for"),
])
async def test_context_held_by_policy(ir, home, policy, log, code, reason):
    policy.answers = {"cover.k": _Decision(False, code)}
    out = await _router(ir, home, AREAS).execute({"intent": "context_close"}, "kitchen")
    assert out == {"executed": False, "intent": "context_close", "entity_id": "cover.k",
                   "reason": reason}
    assert home.service_calls == []


async def test_context_busy_target(ir, home, policy, log):
    mutex = _Mutex(busy={"light.h"})
    out = await _router(ir, home, AREAS, mutex=mutex).execute(
        {"intent": "context_off"}, "hall")
    assert out == {"executed": False, "intent": "context_off", "entity_id": "light.h",
                   "reason": "entity busy (higher-priority lock)"}
    assert ("row-light.h", "blocked", "entity_busy") in log.results


async def test_context_failed_call_releases_and_logs(ir, home, policy, log):
    async def boom(*a, **k):
        raise RuntimeError("down")
    home.services.async_call = boom
    mutex = _Mutex()
    out = await _router(ir, home, AREAS, mutex=mutex).execute(
        {"intent": "context_off"}, "hall")
    assert out == {"executed": False, "intent": "context_off", "entity_id": "light.h"}
    assert mutex.released == ["tok-light.h"]
    assert ("row-light.h", "failed", "service_call_failed") in log.results


async def test_context_success_releases_its_lock(ir, home, policy, log):
    mutex = _Mutex()
    await _router(ir, home, AREAS, mutex=mutex).execute({"intent": "context_off"}, "hall")
    assert mutex.released == ["tok-light.h"]


# ── route ───────────────────────────────────────────────────────────────────

async def test_route(ir, home, policy, log):
    r = _router(ir, home, AREAS)
    assert await r.route("what time is it", "hall") == {
        "matched": False, "intent": None, "executed": False}
    assert await r.route("lights off", "hall", user_id="u") == {
        "matched": True, "executed": True, "intent": "lights_off", "entities": ["light.h"]}
    assert log.started[-1][4] == "u"


# ── the confirmation window ─────────────────────────────────────────────────

@pytest.fixture
def timers(monkeypatch):
    made = []
    ev = types.ModuleType("homeassistant.helpers.event")

    def async_call_later(hass, delay, cb):
        timer = {"delay": delay, "cb": cb, "cancelled": False}
        made.append(timer)

        def cancel():
            timer["cancelled"] = True
            if timer.get("broken"):
                raise RuntimeError("already fired")
        return cancel
    ev.async_call_later = async_call_later
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)
    return made


async def test_window_opens_fires_and_replaces_an_earlier_one(ir, home, timers):
    r = _router(ir, home, AREAS)
    await r.open_feedback_window({"intent": "secure_area", "area": "kitchen"}, timeout=3)
    assert home.bus.fired == [(ir.EVENT_FEEDBACK_WINDOW, {
        "timeout": 3, "action": {"intent": "secure_area", "area": "kitchen"}})]
    timers[0]["broken"] = True
    await r.open_feedback_window({"intent": "lights_off", "area": "hall"})
    assert timers[0]["cancelled"] is True
    assert timers[1]["delay"] == ir.FEEDBACK_TIMEOUT_S == 10.0
    assert r._pending_feedback == {"intent": "lights_off", "area": "hall"}
    timers[1]["cb"](None)
    assert r._pending_feedback is None and r._feedback_cancel is None


async def test_voice_response(ir, home, policy, log, timers):
    r = _router(ir, home, AREAS)
    assert await r.handle_voice_response("yes") == {"handled": False,
                                                     "reason": "no open window"}
    await r.open_feedback_window({"intent": "lights_off", "area": "hall"})
    assert await r.handle_voice_response("maybe") == {"handled": False,
                                                       "affirmative": False}
    assert r._pending_feedback is not None
    out = await r.handle_voice_response("yes")
    assert out == {"handled": True, "executed": True, "intent": "lights_off",
                   "entities": ["light.h"]}
    assert timers[0]["cancelled"] is True and r._pending_feedback is None
    assert log.started[-1][4] is None       # a window yes carries no user
