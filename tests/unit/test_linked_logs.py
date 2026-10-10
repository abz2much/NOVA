"""Gap 3 (8.25.0): a decision entry carries the Actions log request ids of
the commands it caused, so a decision can be traced to its commands and
their checks. No behaviour change. All state is fake; both logs are
temporary.
"""
import pytest

from cognitive_safety_kit import _isolated_core, cc, clock, run_sweeps  # noqa: F401


@pytest.fixture
def logs(load, tmp_path, monkeypatch):
    dr = load("decision_record")
    al = load("action_log")
    monkeypatch.setattr(dr, "_DEFAULT_DB", str(tmp_path / "decisions.db"))
    monkeypatch.setattr(al, "_DEFAULT_DB", str(tmp_path / "actions.db"))

    def requests():
        return {p["request_id"]: p for p in al.page_requests(limit=50)["requests"]}
    return dr, requests


def obey(fake_hass):
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        await real(domain, service, data, blocking=blocking, **kw)
        eid = (data or {}).get("entity_id")
        new = {("lock", "lock"): "locked", ("cover", "close_cover"): "closed"}.get(
            (domain, service))
        st = fake_hass.states.get(eid) if isinstance(eid, str) else None
        if st is not None and new:
            fake_hass.states.set(eid, new, **dict(st.attributes))
    fake_hass.services.async_call = call


async def test_a_sweep_decision_shows_its_action_log_request(cc, fake_hass, logs, clock):
    dr, requests = logs
    obey(fake_hass)
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "lockdown_auto_on_arm": True})
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    await safety.tick(sleeping=True, anyone_home=True)
    await run_sweeps(safety, fake_hass)
    (rec,) = dr.recent(kind="lockdown_sweep")
    (rid,) = rec["evidence"]["request_ids"]
    req = requests()[rid]
    assert req["action"] == "lockdown"
    assert [(t["entity_id"], t["execution_result"]) for t in req["targets"]] == [
        ("lock.front", "verified")]


async def test_a_lockdown_decision_shows_its_action_log_request(cc, fake_hass, logs):
    dr, requests = logs
    obey(fake_hass)
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    await cc.LockdownManager(fake_hass, {}).engage("alarm armed")
    fake_hass.close_pending()
    (rec,) = [r for r in dr.recent(kind="lockdown") if r["decision"] == "lockdown engaged"]
    (rid,) = rec["evidence"]["request_ids"]
    assert [t["entity_id"] for t in requests()[rid]["targets"]] == ["lock.front"]


async def test_lockdown_checks_link_to_the_same_request(cc, fake_hass, logs, monkeypatch):
    import sys
    dr, _requests = logs
    monkeypatch.setattr(sys.modules["jc.core_lockdown"], "LOCKDOWN_SECURE_VERIFY_DELAY", 0)
    obey(fake_hass)
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    await cc.LockdownManager(fake_hass, {}).engage("alarm armed")
    await fake_hass.drain()
    rows = {r["decision"]: r for r in dr.recent(kind="lockdown")}
    assert (rows["checked, secured"]["evidence"]["request_ids"]
            == rows["lockdown engaged"]["evidence"]["request_ids"])


async def test_a_voice_reply_check_shows_its_action_log_request(load, fake_hass, logs):
    dr, requests = logs
    obey(fake_hass)
    ir = load("intent.intent_router")
    r = ir.LocalIntentRouter(fake_hass)
    r._area_of = lambda eid: "hall"
    r.verify_delay = 0
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    await r.execute({"intent": "secure_area"}, "hall")
    await fake_hass.drain()
    (rec,) = dr.recent(kind="voice_secure")
    (rid,) = rec["evidence"]["request_ids"]
    assert requests()[rid]["source"] == "voice"


async def test_nothing_else_about_the_decision_changes(cc, fake_hass, logs, clock):
    # The ids are added to evidence; the decision and its facts are as before.
    dr, _ = logs
    obey(fake_hass)
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "lockdown_auto_on_arm": True})
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    await safety.tick(sleeping=True, anyone_home=True)
    await run_sweeps(safety, fake_hass)
    (rec,) = dr.recent(kind="lockdown_sweep")
    assert rec["decision"] == "lock and close, checked"
    assert rec["observation"]["locked"] == ["Front"]
    assert set(rec["evidence"]) == {"request_ids"}
