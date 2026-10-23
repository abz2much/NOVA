"""Core delivery details: the image block on a phone alert and the authority
check and audit trail of a proactive action.

The routing of alerts to phones and speakers is pinned in
test_cognitive_core_routing and test_alert_path_unchanged. These pin the parts
those leave open."""
import logging
import sys
import types

import pytest

from fakes import FakeHass


@pytest.fixture
def cc(load):
    return load("cognitive_core")


# ── the image block ──────────────────────────────────────────────────────────

def _hass(external=None, internal=None, has_config=True):
    hass = FakeHass()
    hass.config = (types.SimpleNamespace(external_url=external, internal_url=internal)
                   if has_config else types.SimpleNamespace())
    return hass


@pytest.mark.parametrize("url", [None, "", 0])
def test_no_snapshot_means_no_image_block(cc, url):
    assert cc._notification_image_data(_hass("https://ha.test"), url) == {}


def test_both_platforms_get_the_url(cc):
    assert cc._notification_image_data(_hass(), "https://x/snap.jpg") == {
        "image": "https://x/snap.jpg", "attachment": {"url": "https://x/snap.jpg"}}
    assert list(cc._notification_image_data(_hass(), "https://x/s.jpg")) == [
        "image", "attachment"]


def test_a_local_path_is_made_absolute_with_the_external_url(cc):
    out = cc._notification_image_data(
        _hass("https://ha.example.net/", "http://192.168.1.2:8123"), "/local/snap.jpg")
    assert out["image"] == "https://ha.example.net/local/snap.jpg"
    assert out["attachment"] == {"url": "https://ha.example.net/local/snap.jpg"}


def test_the_internal_url_is_used_when_there_is_no_external_one(cc):
    out = cc._notification_image_data(_hass(None, "http://ha.lan:8123//"), "/local/s.jpg")
    assert out["image"] == "http://ha.lan:8123/local/s.jpg"


@pytest.mark.parametrize("hass", [_hass(None, None), _hass("", ""), _hass(has_config=False)])
def test_with_no_known_address_the_path_stays_relative(cc, hass):
    assert cc._notification_image_data(hass, "/local/s.jpg")["image"] == "/local/s.jpg"


def test_a_full_url_is_never_changed(cc):
    out = cc._notification_image_data(_hass("https://ha.test"), "http://cam.lan/s.jpg")
    assert out["image"] == "http://cam.lan/s.jpg"


# ── proactive actions: the authority check ───────────────────────────────────

class _Decision:
    def __init__(self, allowed=True, note="", approval="approved", code=""):
        self.allowed, self.note, self.approval, self.code = allowed, note, approval, code


@pytest.fixture
def env(cc, load, monkeypatch):
    box = types.SimpleNamespace(requests=[], now=[], decisions=[], started=[], finished=[])

    class Request:
        def __init__(self, domain, service, entity_id="", **kw):
            self.domain, self.service, self.entity_id, self.kw = domain, service, entity_id, kw

    def decide():
        return box.decisions.pop(0) if box.decisions else _Decision()

    async def authorize(hass, request):
        box.requests.append(request)
        return decide()

    def authorize_now(hass, request):
        box.now.append(request)
        return decide()

    policy = load("policy")
    monkeypatch.setattr(policy, "AuthorityRequest", Request)
    monkeypatch.setattr(policy, "authorize", authorize)
    monkeypatch.setattr(policy, "authorize_now", authorize_now)
    monkeypatch.setattr(policy, "SOURCE_CHAT", "chat-source")
    monkeypatch.setattr(policy, "SOURCE_AUTOMATIC", "automatic-source")

    al = types.ModuleType("jc.action_log")
    al.new_request_id = lambda: "minted"

    def start(request_id, action, source, **kw):
        box.started.append((request_id, action, source, kw))
        return 7
    al.start = start
    al.set_execution = lambda aid, res, **kw: box.finished.append((aid, res, kw))
    monkeypatch.setitem(sys.modules, "jc.action_log", al)
    return box


DATA = {"domain": "light", "service": "turn_on", "entity_ids": ["light.a", "light.b"]}


async def test_an_offer_the_user_accepted_is_checked_per_entity_like_a_request(cc, env):
    hass = FakeHass()
    assert await cc._execute_action_data(hass, DATA, source="proactive_accepted") is True
    assert [(r.domain, r.service, r.entity_id, r.kw) for r in env.requests] == [
        ("light", "turn_on", "light.a", {"label": "turn on", "source": "chat-source"}),
        ("light", "turn_on", "light.b", {"label": "turn on", "source": "chat-source"})]
    assert env.now == []
    assert env.started == [("minted", "proactive_offer_execute", "proactive_accepted", {
        "domain": "light", "service": "turn_on", "entity_id": "light.a, light.b"})]
    assert env.finished == [(7, "accepted", {})]
    assert hass.service_calls == [("light", "turn_on", {"entity_id": ["light.a", "light.b"]})]


async def test_the_default_source_is_proactive_and_counts_as_a_request(cc, env):
    await cc._execute_action_data(FakeHass(), DATA)
    assert env.started[0][2] == "proactive" and len(env.requests) == 2 and env.now == []


async def test_an_offer_nova_runs_alone_is_checked_as_automatic_and_not_asked_about(cc, env):
    await cc._execute_action_data(FakeHass(), DATA, source="proactive_autonomous")
    assert [(r.entity_id, r.kw["source"]) for r in env.now] == [
        ("light.a", "automatic-source"), ("light.b", "automatic-source")]
    assert env.requests == []


async def test_a_single_entity_given_as_text_is_checked_once(cc, env):
    hass = FakeHass()
    await cc._execute_action_data(
        hass, {"domain": "switch", "service": "turn_off", "entity_ids": "switch.pump"})
    assert [r.entity_id for r in env.requests] == ["switch.pump"]
    assert hass.service_calls == [("switch", "turn_off", {"entity_id": "switch.pump"})]
    assert env.started[0][3]["entity_id"] == "switch.pump"


async def test_one_refusal_blocks_the_whole_action_and_is_logged(cc, env, caplog):
    env.decisions = [_Decision(), _Decision(False, note="Needs a tap", approval="rejected",
                                            code="needs_confirmation")]
    hass = FakeHass()
    with caplog.at_level(logging.INFO, logger="jc.cognitive_core"):
        assert await cc._execute_action_data(hass, DATA) is False
    assert hass.service_calls == []
    assert env.finished == [(7, "blocked", {"reason_code": "needs_confirmation"})]
    assert [r.getMessage() for r in caplog.records] == [
        "Proactive action light.turn_on on light.b not authorized: Needs a tap"]


async def test_a_refusal_with_no_code_or_note_uses_the_defaults(cc, env, caplog):
    env.decisions = [_Decision(False, note="", approval="expired", code="")]
    with caplog.at_level(logging.INFO, logger="jc.cognitive_core"):
        assert await cc._execute_action_data(FakeHass(), DATA) is False
    assert env.finished == [(7, "blocked", {"reason_code": "not_authorized"})]
    assert caplog.records[0].getMessage().endswith("not authorized: expired")


async def test_the_first_refusal_stops_the_checks(cc, env):
    env.decisions = [_Decision(False, code="x")]
    await cc._execute_action_data(FakeHass(), DATA)
    assert len(env.requests) == 1


async def test_service_data_is_merged_after_the_entities_and_the_call_waits(cc, env):
    seen = []

    class Hass(FakeHass):
        pass

    hass = Hass()

    async def call(domain, service, data=None, blocking=False, **kw):
        seen.append((domain, service, data, blocking))

    hass.services.async_call = call
    await cc._execute_action_data(hass, {**DATA, "service_data": {"brightness": 50}})
    assert seen == [("light", "turn_on",
                     {"entity_id": ["light.a", "light.b"], "brightness": 50}, True)]
    assert list(seen[0][2]) == ["entity_id", "brightness"]


async def test_null_service_data_is_treated_as_none(cc, env):
    hass = FakeHass()
    await cc._execute_action_data(hass, {**DATA, "service_data": None})
    assert hass.service_calls == [("light", "turn_on", {"entity_id": ["light.a", "light.b"]})]


async def test_a_failing_call_is_a_warning_and_a_failed_audit_row(cc, env, caplog):
    hass = FakeHass()

    async def boom(*a, **k):
        raise RuntimeError("device offline")
    hass.services.async_call = boom
    with caplog.at_level(logging.WARNING, logger="jc.cognitive_core"):
        assert await cc._execute_action_data(hass, DATA) is False
    assert env.finished == [(7, "failed", {"reason_code": "service_call_failed"})]
    assert [r.getMessage() for r in caplog.records] == [
        "Proactive action failed (light.turn_on): device offline"]


async def test_the_callers_request_id_is_used_and_none_is_minted(cc, env):
    await cc._execute_action_data(FakeHass(), DATA, request_id="mine")
    assert env.started[0][0] == "mine"
