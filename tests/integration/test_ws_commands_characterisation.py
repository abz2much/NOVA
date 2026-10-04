"""Characterisation tests for panel websocket commands that had little coverage
(11% to 40%): the knowledge, memory and lockdown commands, the decision
analysis commands, and the suggestion, trial and goal commands.

They drive the real commands through a real Home Assistant (PHACC) and fake
only what each command calls (the knowledge store, goals, the pattern engine,
the cognitive core ...). Each test pins the payload a handler sends back, the
exact arguments it passes on, the error code it uses, and that no raw
exception text reaches the panel. They run unchanged whichever module a
command lives in, so they pass before and after the commands move out of
websocket.py.
"""
import types

import pytest

from .test_runtime_data import (  # noqa: F401  (autouse fixtures)
    DOMAIN,
    _no_real_config,
    _restore_nova_config,
    _setup,
)

SECRET = "secret-detail-9137"          # must never reach the panel


async def _ws(hass, hass_ws_client, payload) -> dict:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(payload)
    return await client.receive_json()


def _boom(*_a, **_k):
    raise RuntimeError(SECRET)


async def _aboom(*_a, **_k):
    raise RuntimeError(SECRET)


def _assert_error(resp, code):
    assert resp["success"] is False, resp
    assert resp["error"]["code"] == code, resp
    assert SECRET not in resp["error"]["message"], resp


def _log_messages():
    from custom_components.nova.websocket import recent_debug_log
    return [e["msg"] for e in recent_debug_log(500)]


@pytest.fixture
async def nova(hass):
    return await _setup(hass)


# ── knowledge, memory, lockdown ──────────────────────────────────────────────

@pytest.fixture
def knowledge_fake(monkeypatch):
    from custom_components.nova import knowledge
    calls = types.SimpleNamespace(remember=[], forget=[], confirm=[], edit=[],
                                  all_facts=[], boom=False)
    state = types.SimpleNamespace(remember_result={"id": 1}, forget_result=1,
                                  confirm_result=True, edit_result={"id": 7})

    def remember(key, value, **kw):
        if calls.boom:
            raise RuntimeError(SECRET)
        calls.remember.append((key, value, kw))
        return state.remember_result

    def forget(**kw):
        if calls.boom:
            raise RuntimeError(SECRET)
        calls.forget.append(kw)
        return state.forget_result

    def all_facts(*a, **kw):
        calls.all_facts.append(kw)
        return ["fact-a"]

    monkeypatch.setattr(knowledge, "remember", remember)
    monkeypatch.setattr(knowledge, "forget", forget)
    monkeypatch.setattr(knowledge, "all_facts", all_facts)
    monkeypatch.setattr(knowledge, "pending_facts", lambda *a, **k: ["pending-a"])
    monkeypatch.setattr(knowledge, "confirm_fact",
                        lambda fid: (calls.confirm.append(fid), state.confirm_result)[1])
    monkeypatch.setattr(knowledge, "edit_fact",
                        lambda fid, value: (calls.edit.append((fid, value)), state.edit_result)[1])
    return types.SimpleNamespace(calls=calls, state=state, mod=knowledge)


async def test_add_knowledge_defaults_and_payload(hass, hass_ws_client, nova, knowledge_fake):
    resp = await _ws(hass, hass_ws_client,
                     {"type": "nova/add_knowledge", "key": "wifi", "value": "pw"})
    assert resp["success"] and resp["result"] == {"ok": True, "facts": ["fact-a"]}
    assert knowledge_fake.calls.remember == [
        ("wifi", "pw", {"subject": "household", "kind": "fact", "source": "stated"})]


async def test_add_knowledge_passes_subject_and_kind(hass, hass_ws_client, nova, knowledge_fake):
    knowledge_fake.state.remember_result = None
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/add_knowledge", "key": "k", "value": "v",
        "subject": "Abi", "kind": "preference"})
    assert resp["result"]["ok"] is False                       # nothing stored
    assert knowledge_fake.calls.remember[0][2] == {
        "subject": "Abi", "kind": "preference", "source": "stated"}


async def test_add_knowledge_error(hass, hass_ws_client, nova, knowledge_fake):
    knowledge_fake.calls.boom = True
    _assert_error(await _ws(hass, hass_ws_client, {
        "type": "nova/add_knowledge", "key": "k", "value": "v"}), "add_failed")


async def test_clear_scene_memory(hass, hass_ws_client, nova, monkeypatch):
    from custom_components.nova import scene_memory
    monkeypatch.setattr(scene_memory, "forget_all", lambda: 3)
    monkeypatch.setattr(scene_memory, "stats", lambda: {"descriptions": 0})
    resp = await _ws(hass, hass_ws_client, {"type": "nova/clear_scene_memory"})
    assert resp["result"] == {"removed": 3, "stats": {"descriptions": 0}}
    monkeypatch.setattr(scene_memory, "forget_all", _boom)
    _assert_error(await _ws(hass, hass_ws_client, {"type": "nova/clear_scene_memory"}),
                  "clear_failed")


async def test_forget_knowledge_by_fact_id_and_by_subject_key(
        hass, hass_ws_client, nova, knowledge_fake):
    resp = await _ws(hass, hass_ws_client, {"type": "nova/forget_knowledge", "fact_id": 4})
    assert resp["result"] == {"removed": 1, "facts": ["fact-a"]}
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/forget_knowledge", "subject": "Abi", "key": "wifi"})
    assert resp["success"]
    assert knowledge_fake.calls.forget == [
        {"fact_id": 4, "subject": None, "key": None},
        {"fact_id": None, "subject": "Abi", "key": "wifi"}]


async def test_forget_knowledge_error(hass, hass_ws_client, nova, knowledge_fake):
    knowledge_fake.calls.boom = True
    _assert_error(await _ws(hass, hass_ws_client, {
        "type": "nova/forget_knowledge", "fact_id": 1}), "forget_failed")


async def test_pending_fact_confirm_and_reject(hass, hass_ws_client, nova, knowledge_fake):
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/pending_fact_action", "fact_id": 9, "action": "confirm"})
    assert resp["result"] == {"ok": True, "facts": ["fact-a"], "pending": ["pending-a"]}
    assert knowledge_fake.calls.confirm == [9]
    assert knowledge_fake.calls.all_facts[-1] == {"status": "confirmed"}
    knowledge_fake.state.forget_result = 2                      # truthy count -> True
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/pending_fact_action", "fact_id": 9, "action": "reject"})
    assert resp["result"]["ok"] is True
    assert knowledge_fake.calls.forget == [{"fact_id": 9}]
    knowledge_fake.state.forget_result = 0
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/pending_fact_action", "fact_id": 9, "action": "reject"})
    assert resp["result"]["ok"] is False


async def test_pending_fact_action_rejects_other_actions_and_reports_errors(
        hass, hass_ws_client, nova, knowledge_fake):
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/pending_fact_action", "fact_id": 9, "action": "delete"})
    assert resp["success"] is False and resp["error"]["code"] == "invalid_format"
    assert knowledge_fake.calls.confirm == [] and knowledge_fake.calls.forget == []
    knowledge_fake.mod.confirm_fact = _boom
    _assert_error(await _ws(hass, hass_ws_client, {
        "type": "nova/pending_fact_action", "fact_id": 9, "action": "confirm"}),
        "pending_fact_action_failed")


async def test_edit_pending_fact(hass, hass_ws_client, nova, knowledge_fake):
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/edit_pending_fact", "fact_id": 7, "value": "new"})
    assert resp["result"] == {"ok": True, "pending": ["pending-a"]}
    assert knowledge_fake.calls.edit == [(7, "new")]
    knowledge_fake.state.edit_result = None
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/edit_pending_fact", "fact_id": 7, "value": "new"})
    assert resp["result"]["ok"] is False
    knowledge_fake.mod.edit_fact = _boom
    _assert_error(await _ws(hass, hass_ws_client, {
        "type": "nova/edit_pending_fact", "fact_id": 7, "value": "x"}),
        "edit_pending_fact_failed")


async def test_search_memory(hass, hass_ws_client, nova, monkeypatch):
    from custom_components.nova import memory
    seen = []
    monkeypatch.setattr(memory, "search_memory",
                        lambda query, k=5: (seen.append((query, k)), ["hit"])[1])
    resp = await _ws(hass, hass_ws_client, {"type": "nova/search_memory", "query": "pizza"})
    assert resp["result"] == {"results": ["hit"]}
    await _ws(hass, hass_ws_client, {"type": "nova/search_memory", "query": "x", "k": 2})
    assert seen == [("pizza", 5), ("x", 2)]                     # k defaults to 5
    monkeypatch.setattr(memory, "search_memory", _boom)
    _assert_error(await _ws(hass, hass_ws_client, {
        "type": "nova/search_memory", "query": "x"}), "search_failed")


async def test_set_lockdown(hass, hass_ws_client, nova, monkeypatch):
    from custom_components.nova import cognitive_core
    seen = []

    async def request(on, reason="requested", hass=None):
        seen.append((on, reason, hass))
        return True

    monkeypatch.setattr(cognitive_core, "request_lockdown", request)
    monkeypatch.setattr(cognitive_core, "lockdown_status", lambda: {"active": True})
    resp = await _ws(hass, hass_ws_client, {"type": "nova/set_lockdown", "on": True})
    assert resp["result"] == {"ok": True, "lockdown": {"active": True}}
    assert seen == [(True, "requested from panel", hass)]

    async def refused(on, reason="requested", hass=None):
        return False

    monkeypatch.setattr(cognitive_core, "request_lockdown", refused)
    resp = await _ws(hass, hass_ws_client, {"type": "nova/set_lockdown", "on": False})
    assert resp["result"] == {"ok": False, "lockdown": {"active": True}}

    monkeypatch.setattr(cognitive_core, "request_lockdown", _aboom)
    _assert_error(await _ws(hass, hass_ws_client, {"type": "nova/set_lockdown", "on": True}),
                  "lockdown_failed")


# ── decision analysis ────────────────────────────────────────────────────────

async def test_run_analysis_names_entities_in_the_diagnostic(
        hass, hass_ws_client, nova, monkeypatch):
    from custom_components.nova import cognitive_core
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen Light"})

    async def run(hass_):
        return {"ran": True, "diagnostic": {
            "candidates": [{"entity_id": "light.kitchen"}, {"entity_id": "switch.fan"},
                           {"no_entity": 1}, "not a dict"],
            "top_sources": [{"entity_id": "light.kitchen"}]}}

    monkeypatch.setattr(cognitive_core, "run_analysis_now", run)
    resp = await _ws(hass, hass_ws_client, {"type": "nova/run_analysis"})
    dg = resp["result"]["diagnostic"]
    assert dg["candidates"][0]["name"] == "Kitchen Light"
    assert dg["candidates"][1]["name"] == "fan"                 # readable id fallback
    assert "name" not in dg["candidates"][2]
    assert dg["top_sources"][0]["name"] == "Kitchen Light"


async def test_run_analysis_passes_other_results_through(
        hass, hass_ws_client, nova, monkeypatch):
    from custom_components.nova import cognitive_core

    async def run(hass_):
        return {"ran": False, "reason": "busy"}

    monkeypatch.setattr(cognitive_core, "run_analysis_now", run)
    resp = await _ws(hass, hass_ws_client, {"type": "nova/run_analysis"})
    assert resp["success"] and resp["result"] == {"ran": False, "reason": "busy"}


async def test_run_analysis_failure_is_a_result_not_an_error(
        hass, hass_ws_client, nova, monkeypatch):
    from custom_components.nova import cognitive_core
    monkeypatch.setattr(cognitive_core, "run_analysis_now", _aboom)
    resp = await _ws(hass, hass_ws_client, {"type": "nova/run_analysis"})
    assert resp["success"] is True
    assert resp["result"]["ran"] is False
    assert SECRET not in resp["result"]["error"]


async def test_root_cause(hass, hass_ws_client, nova, monkeypatch):
    from custom_components.nova import rca
    seen = []
    monkeypatch.setattr(rca, "entity_names", lambda h: {"light.a": "A"})
    monkeypatch.setattr(rca, "analyze", lambda *a, **k: (seen.append((a, k)), {"cause": "x"})[1])
    resp = await _ws(hass, hass_ws_client, {"type": "nova/root_cause", "entity_id": "light.a"})
    assert resp["result"] == {"cause": "x"}
    await _ws(hass, hass_ws_client, {
        "type": "nova/root_cause", "entity_id": "light.a",
        "event_time": "2026-01-01T00:00:00", "window_secs": 60})
    assert seen == [
        (("light.a", None, rca.DEFAULT_WINDOW_SECS), {"names": {"light.a": "A"}}),
        (("light.a", "2026-01-01T00:00:00", 60), {"names": {"light.a": "A"}})]
    monkeypatch.setattr(rca, "analyze", _boom)
    _assert_error(await _ws(hass, hass_ws_client, {
        "type": "nova/root_cause", "entity_id": "light.a"}), "root_cause_failed")


# ── suggestions, trials, goals, routines ─────────────────────────────────────

@pytest.fixture
def analyzer_fake(monkeypatch):
    from custom_components.nova.automation import installation, patterns
    calls = types.SimpleNamespace(restore=[], dismiss=[], install=[], routines=[])
    state = types.SimpleNamespace(
        install_result={"ok": True, "installed": True, "alias": "Porch light"},
        restore_result=True, dismiss_result=True, rows=[], boom=False)

    analyzer = types.SimpleNamespace(
        restore_suggestion=lambda sid: (calls.restore.append(sid), state.restore_result)[1],
        dismiss_suggestion=lambda sid: (calls.dismiss.append(sid), state.dismiss_result)[1],
        get_person_patterns=lambda: state.rows)

    async def install(hass, sid, requested_by_user_id=None, requested_by_name=None):
        if state.boom:
            raise RuntimeError(SECRET)
        calls.install.append((sid, requested_by_user_id, requested_by_name))
        return state.install_result

    monkeypatch.setattr(patterns, "get_analyzer", lambda: analyzer)
    monkeypatch.setattr(installation, "install_approved_suggestion", install)
    return types.SimpleNamespace(calls=calls, state=state)


async def test_suggestion_approve_installed(hass, hass_ws_client, nova, analyzer_fake):
    resp = await _ws(hass, hass_ws_client,
                     {"type": "nova/suggestion_action", "suggestion_id": 5, "action": "approve"})
    assert resp["result"] == {"ok": True, "installed": True, "reason": None,
                              "alias": "Porch light"}
    (sid, user_id, user_name), = analyzer_fake.calls.install
    assert sid == 5 and user_id                                 # the admin who asked
    assert "Suggestion #5 approved & installed as 'Porch light'" in _log_messages()


async def test_suggestion_approve_advisory_and_failed(hass, hass_ws_client, nova, analyzer_fake):
    analyzer_fake.state.install_result = {"ok": True, "installed": False,
                                          "reason": "needs a manual step"}
    resp = await _ws(hass, hass_ws_client,
                     {"type": "nova/suggestion_action", "suggestion_id": 6, "action": "approve"})
    assert resp["result"] == {"ok": True, "installed": False,
                              "reason": "needs a manual step", "alias": None}
    assert "Suggestion #6 approved (advisory — needs a manual step)" in _log_messages()
    analyzer_fake.state.install_result = {"ok": False, "reason": "gone"}
    resp = await _ws(hass, hass_ws_client,
                     {"type": "nova/suggestion_action", "suggestion_id": 7, "action": "approve"})
    assert resp["result"] == {"ok": False, "installed": False, "reason": "gone", "alias": None}
    assert not any("Suggestion #7" in m for m in _log_messages())


async def test_suggestion_restore_and_dismiss(hass, hass_ws_client, nova, analyzer_fake):
    resp = await _ws(hass, hass_ws_client,
                     {"type": "nova/suggestion_action", "suggestion_id": 8, "action": "restore"})
    assert resp["result"] == {"ok": True}
    analyzer_fake.state.dismiss_result = False
    resp = await _ws(hass, hass_ws_client,
                     {"type": "nova/suggestion_action", "suggestion_id": 9, "action": "dismiss"})
    assert resp["result"] == {"ok": False}
    assert analyzer_fake.calls.restore == [8] and analyzer_fake.calls.dismiss == [9]
    assert analyzer_fake.calls.install == []
    msgs = _log_messages()
    assert "Suggestion #8 restored after AI review (ok=True)" in msgs
    assert "Suggestion #9 dismissed (ok=False)" in msgs


async def test_suggestion_action_error_and_bad_action(hass, hass_ws_client, nova, analyzer_fake):
    analyzer_fake.state.boom = True
    _assert_error(await _ws(hass, hass_ws_client, {
        "type": "nova/suggestion_action", "suggestion_id": 1, "action": "approve"}),
        "suggestion_action_failed")
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/suggestion_action", "suggestion_id": 1, "action": "delete"})
    assert resp["success"] is False and resp["error"]["code"] == "invalid_format"


async def test_list_automation_inventory(hass, hass_ws_client, nova, monkeypatch):
    from custom_components.nova.automation import inventory
    monkeypatch.setattr(inventory, "get_inventory", lambda h: None)
    resp = await _ws(hass, hass_ws_client, {"type": "nova/list_automation_inventory"})
    assert resp["result"] == {"available": False, "refreshed_at": None, "automations": []}
    fake = types.SimpleNamespace(refreshed_at=123.5, public_items=lambda: [{"alias": "A"}])
    monkeypatch.setattr(inventory, "get_inventory", lambda h: fake)
    resp = await _ws(hass, hass_ws_client, {"type": "nova/list_automation_inventory"})
    assert resp["result"] == {"available": True, "refreshed_at": 123.5,
                              "automations": [{"alias": "A"}]}
    monkeypatch.setattr(inventory, "get_inventory", _boom)
    _assert_error(await _ws(hass, hass_ws_client, {"type": "nova/list_automation_inventory"}),
                  "list_automation_inventory_failed")


async def test_automation_trial_feedback(hass, hass_ws_client, nova, monkeypatch):
    from custom_components.nova.automation import trials
    seen = []
    monkeypatch.setattr(trials, "set_manual_outcome",
                        lambda tid, verdict: (seen.append((tid, verdict)), tid == 3)[1])
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/automation_trial_feedback", "trial_id": 3, "verdict": "working"})
    assert resp["result"] == {"ok": True}
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/automation_trial_feedback", "trial_id": 4, "verdict": "needs_adjustment"})
    assert resp["result"] == {"ok": False}
    assert seen == [(3, "working"), (4, "needs_adjustment")]
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/automation_trial_feedback", "trial_id": 3, "verdict": "great"})
    assert resp["success"] is False and resp["error"]["code"] == "invalid_format"
    monkeypatch.setattr(trials, "set_manual_outcome", _boom)
    _assert_error(await _ws(hass, hass_ws_client, {
        "type": "nova/automation_trial_feedback", "trial_id": 3, "verdict": "working"}),
        "automation_trial_feedback_failed")


@pytest.fixture
def goals_fake(monkeypatch):
    from custom_components.nova import goals
    calls = types.SimpleNamespace(create=[], delete=[], cancel=[])
    state = types.SimpleNamespace(create_result={"id": 11, "title": "T"}, ok=True,
                                  recent=[{"id": 11, "title": "T"}])
    monkeypatch.setattr(goals, "create", lambda title, outcome, **kw: (
        calls.create.append((title, outcome, kw)), state.create_result)[1])
    monkeypatch.setattr(goals, "delete", lambda gid: (calls.delete.append(gid), state.ok)[1])
    monkeypatch.setattr(goals, "cancel", lambda gid: (calls.cancel.append(gid), state.ok)[1])
    monkeypatch.setattr(goals, "recent", lambda limit=20: state.recent)
    return types.SimpleNamespace(calls=calls, state=state, mod=goals)


async def test_goal_create(hass, hass_ws_client, nova, goals_fake):
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/goal_action", "action": "create",
        "title": "  Tidy up ", "outcome": " Garage is clear ", "interval_min": 30})
    assert resp["result"]["ok"] is True and resp["result"]["goal"] == {"id": 11, "title": "T"}
    assert resp["result"]["goals"][0]["id"] == 11               # panel shaped list
    assert goals_fake.calls.create == [("Tidy up", "Garage is clear",
                                        {"check_interval_min": 30.0})]
    assert "Goal created from panel: Tidy up" in _log_messages()
    await _ws(hass, hass_ws_client, {
        "type": "nova/goal_action", "action": "create", "outcome": "No title goal"})
    assert goals_fake.calls.create[1] == ("", "No title goal", {})
    assert "Goal created from panel: No title goal" in _log_messages()


async def test_goal_create_errors(hass, hass_ws_client, nova, goals_fake):
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/goal_action", "action": "create", "outcome": "   "})
    assert resp["success"] is False and resp["error"]["code"] == "empty_outcome"
    assert resp["error"]["message"] == "a goal needs an outcome to work toward"
    goals_fake.state.create_result = {"error": "store is full"}
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/goal_action", "action": "create", "outcome": "x"})
    assert resp["error"]["code"] == "create_failed"
    assert resp["error"]["message"] == "store is full"


async def test_goal_cancel_and_delete(hass, hass_ws_client, nova, goals_fake):
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/goal_action", "action": "delete", "goal_id": 3})
    assert resp["result"]["ok"] is True and resp["result"]["goals"][0]["id"] == 11
    goals_fake.state.ok = False
    resp = await _ws(hass, hass_ws_client, {
        "type": "nova/goal_action", "action": "cancel", "goal_id": 4})
    assert resp["result"]["ok"] is False
    assert goals_fake.calls.delete == [3] and goals_fake.calls.cancel == [4]
    msgs = _log_messages()
    assert "Goal #3 deleted from panel (ok=True)" in msgs
    assert "Goal #4 cancelled from panel (ok=False)" in msgs


async def test_goal_action_needs_a_goal_id_and_reports_errors(
        hass, hass_ws_client, nova, goals_fake):
    resp = await _ws(hass, hass_ws_client, {"type": "nova/goal_action", "action": "cancel"})
    assert resp["error"]["code"] == "missing_goal_id"
    assert resp["error"]["message"] == "cancel needs a goal_id"
    goals_fake.mod.delete = _boom
    _assert_error(await _ws(hass, hass_ws_client, {
        "type": "nova/goal_action", "action": "delete", "goal_id": 1}), "goal_action_failed")


async def test_get_person_routines(hass, hass_ws_client, nova, analyzer_fake):
    hass.states.async_set("light.lamp", "on", {"friendly_name": "Hall Lamp"})
    analyzer_fake.state.rows = [
        {"person": "Abi", "id": 1, "pattern_type": "device",
         "description": "turns on light.lamp", "confidence": 0.5, "occurrences": 4,
         "last_seen": "2026-01-01"},
        {"person": "", "id": 2}]
    resp = await _ws(hass, hass_ws_client, {"type": "nova/get_person_routines"})
    assert resp["result"] == {"routines": {"Abi": [{
        "id": 1, "pattern_type": "device", "description": "turns on Hall Lamp",
        "confidence": 0.5, "occurrences": 4, "last_seen": "2026-01-01"}]}}
