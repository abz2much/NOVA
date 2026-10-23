"""The agent tools for acceptance counts and automation suggestions: what each
asks of the rest of Nova and what the model reads back."""
import json
import sys
import types

import pytest

from fakes import FakeHass

SAFE = "RuntimeError (details are in the Home Assistant log)"


@pytest.fixture
def auto(load):
    return load("agent_runtime.capabilities.automation")


# ── manage_autonomy ──────────────────────────────────────────────────────────

@pytest.fixture
def core(monkeypatch):
    box = types.SimpleNamespace(revoked=[], status_value={}, error=None)

    def status():
        if box.error:
            raise box.error
        return box.status_value

    def revoke_autonomy(key):
        box.revoked.append(key)
        return {"revoked": key}

    mod = types.ModuleType("jc.cognitive_core")
    mod.status, mod.revoke_autonomy = status, revoke_autonomy
    monkeypatch.setitem(sys.modules, "jc.cognitive_core", mod)
    return box


NOTE = "Every offer waits for a yes; these are acceptance counts only."


async def test_the_default_is_a_list_of_counts_that_never_acts_alone(auto, core):
    core.status_value = {"autonomy_grants": [{"pattern_key": "k", "approvals": 2}]}
    out = await auto._exec_manage_autonomy(FakeHass(), {})
    assert json.loads(out) == {"acts_on_its_own": False, "note": NOTE,
                               "grants": [{"pattern_key": "k", "approvals": 2}]}
    assert list(json.loads(out)) == ["acts_on_its_own", "note", "grants"]


@pytest.mark.parametrize("action", ["list", "anything else", None, ""])
async def test_any_action_but_revoke_lists(auto, core, action):
    out = await auto._exec_manage_autonomy(FakeHass(), {"action": action})
    assert json.loads(out)["grants"] == [] and core.revoked == []


async def test_a_revoke_resets_one_pattern(auto, core):
    out = await auto._exec_manage_autonomy(
        FakeHass(), {"action": "revoke", "pattern_key": "lights:lounge"})
    assert json.loads(out) == {"revoked": "lights:lounge"}
    assert core.revoked == ["lights:lounge"]


@pytest.mark.parametrize("args", [{"action": "revoke"},
                                  {"action": "revoke", "pattern_key": ""}])
async def test_a_revoke_needs_a_pattern_key(auto, core, args):
    out = await auto._exec_manage_autonomy(FakeHass(), args)
    assert json.loads(out) == {"error": "pattern_key required for revoke"}
    assert core.revoked == []


async def test_a_failing_status_is_a_safe_error(auto, core):
    core.error = RuntimeError("secret")
    assert json.loads(await auto._exec_manage_autonomy(FakeHass(), {})) == {"error": SAFE}


# ── review and dismiss ───────────────────────────────────────────────────────

class _Analyzer:
    def __init__(self):
        self.pending = []
        self.dismissed = []
        self.dismiss_result = True
        self.error = None

    def get_pending_suggestions(self):
        if self.error:
            raise self.error
        return self.pending

    def dismiss_suggestion(self, sid):
        if self.error:
            raise self.error
        self.dismissed.append(sid)
        return self.dismiss_result


@pytest.fixture
def analyzer(load, monkeypatch):
    box = _Analyzer()
    monkeypatch.setattr(load("automation.patterns"), "get_analyzer", lambda: box)
    return box


async def test_pending_suggestions_are_returned_as_they_are(auto, analyzer):
    analyzer.pending = [{"id": 1, "description": "porch on at 18:00"}]
    out = await auto._exec_review_suggestions(FakeHass(), {})
    assert json.loads(out) == analyzer.pending


async def test_no_pending_suggestions_gives_a_message(auto, analyzer):
    out = await auto._exec_review_suggestions(FakeHass(), {})
    assert json.loads(out) == {
        "message": "No pending suggestions. I need more data to identify patterns."}


async def test_the_pending_read_runs_in_the_executor(auto, analyzer):
    hass, jobs = FakeHass(), []

    async def run(func, *args):
        jobs.append((func, args))
        return func(*args)
    hass.async_add_executor_job = run
    await auto._exec_review_suggestions(hass, {})
    assert jobs == [(analyzer.get_pending_suggestions, ())]


async def test_a_failing_review_is_a_safe_error(auto, analyzer):
    analyzer.error = RuntimeError("secret")
    assert json.loads(await auto._exec_review_suggestions(FakeHass(), {})) == {"error": SAFE}


@pytest.mark.parametrize("args, sid", [({"suggestion_id": 4}, 4), ({"suggestion_id": "9"}, 9),
                                       ({}, 0), ({"suggestion_id": 2.7}, 2)])
async def test_a_dismissal_reports_the_suggestion_it_handled(auto, analyzer, args, sid):
    out = await auto._exec_dismiss_suggestion(FakeHass(), args)
    assert json.loads(out) == {"success": True, "suggestion_id": sid}
    assert analyzer.dismissed == [sid]


async def test_a_dismissal_that_did_nothing_says_so(auto, analyzer):
    analyzer.dismiss_result = False
    out = await auto._exec_dismiss_suggestion(FakeHass(), {"suggestion_id": 3})
    assert json.loads(out) == {"success": False, "suggestion_id": 3}


async def test_a_bad_suggestion_id_on_dismiss_is_a_safe_error(auto, analyzer):
    out = await auto._exec_dismiss_suggestion(FakeHass(), {"suggestion_id": "x"})
    assert json.loads(out) == {"error": "ValueError (details are in the Home Assistant log)"}
    assert analyzer.dismissed == []


async def test_a_failing_dismissal_is_a_safe_error(auto, analyzer):
    analyzer.error = RuntimeError("secret")
    assert json.loads(await auto._exec_dismiss_suggestion(
        FakeHass(), {"suggestion_id": 1})) == {"error": SAFE}


# ── approve ──────────────────────────────────────────────────────────────────

@pytest.fixture
def installer(load, monkeypatch):
    box = types.SimpleNamespace(calls=[], result={"ok": True, "installed": True}, error=None)

    async def install(hass, sid, **kw):
        box.calls.append((sid, kw))
        if box.error:
            raise box.error
        return box.result

    monkeypatch.setattr(load("automation.installation"), "install_approved_suggestion",
                        install)
    return box


async def test_an_approval_installs_with_the_real_user_and_device(auto, installer):
    out = await auto._exec_approve_suggestion(
        FakeHass(), {"suggestion_id": 5}, user_id="user-1", device_id="dev-1")
    assert json.loads(out) == {"ok": True, "installed": True}
    assert installer.calls == [(5, {"requested_by_user_id": "user-1",
                                    "request_device_id": "dev-1"})]


@pytest.mark.parametrize("user, device", [(None, None), ("", ""), (None, "dev")])
async def test_an_approval_never_invents_a_user_or_device(auto, installer, user, device):
    await auto._exec_approve_suggestion(
        FakeHass(), {"suggestion_id": 5}, user_id=user, device_id=device)
    assert installer.calls == [(5, {"requested_by_user_id": None,
                                    "request_device_id": device or None})]


async def test_an_approval_with_no_id_asks_for_suggestion_zero(auto, installer):
    await auto._exec_approve_suggestion(FakeHass(), {})
    assert installer.calls[0][0] == 0


async def test_a_bad_id_on_approve_is_a_safe_error(auto, installer):
    out = await auto._exec_approve_suggestion(FakeHass(), {"suggestion_id": "x"})
    assert json.loads(out) == {"error": "ValueError (details are in the Home Assistant log)"}
    assert installer.calls == []


async def test_a_failing_install_is_a_safe_error(auto, installer):
    installer.error = RuntimeError("secret")
    out = await auto._exec_approve_suggestion(FakeHass(), {"suggestion_id": 1})
    assert json.loads(out) == {"error": SAFE}


def test_the_agent_logger_keeps_the_name_it_always_had(auto):
    assert auto._LOGGER.name == "jc.agent"
