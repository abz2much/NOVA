"""The Chat tab's phone tap rule in policy.py (8.31.0).

A turn whose conversation id starts nova_chat_ is run inside policy.chat_turn().
For unlock, open (cover, garage, door), disarm and standing down an intrusion,
the gate then asks for a tap on the requesting user's own phone, as it does for
voice, even when voice confirmation is switched off. It only ever makes the gate
stricter: voice, automatic and plain typed Assist behave as before.
"""
import types

import pytest

from fakes import FakeHass


@pytest.fixture
def pol(load):
    return load("policy")


@pytest.fixture
def vc(load):
    return load("voice_confirm")


def _no_protection(vc, monkeypatch):
    """voice_confirm_enabled off: nothing is protected by the general opt in."""
    monkeypatch.setattr(vc, "action_is_protected", lambda hass, d, s, e="": False)
    monkeypatch.setattr(vc, "is_voice_satellite_device", lambda hass, did, **k: False)


def _phones(vc, monkeypatch, phones):
    asked = []

    def lookup(hass, user_id):
        asked.append(user_id)
        return list(phones.get(user_id, []))
    monkeypatch.setattr(vc, "phone_services_for_user", lookup)
    return asked


def _tap(vc, monkeypatch, result):
    sent = []

    async def phone(hass, question, timeout=None, services=None):
        sent.append({"question": question, "services": services})
        return result

    async def not_phone(*a, **k):
        raise AssertionError("a spoken confirmation must never run for a chat turn")
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", phone)
    monkeypatch.setattr(vc, "confirm", not_phone)
    monkeypatch.setattr(vc, "confirm_typed", not_phone)
    return sent


OPENING = [
    ("lock", "unlock", "lock.front_door"),
    ("cover", "open_cover", "cover.garage_door"),
    ("cover", "open", "cover.back_door"),
    ("alarm_control_panel", "alarm_disarm", "alarm_control_panel.home"),
    ("nova", "dismiss_intrusion", ""),
]


# ── the context ─────────────────────────────────────────────────────────────

def test_chat_id_names_the_user(pol):
    assert pol.chat_conversation_id("abc") == "nova_chat_abc"
    assert pol.CHAT_ID_PREFIX == "nova_chat_"


def test_chat_turn_is_marked_only_for_a_chat_id_and_resets(pol):
    assert pol._chat_user() is None
    with pol.chat_turn("nova_chat_u1", "u1") as active:
        assert active is True and pol._chat_user() == "u1"
    assert pol._chat_user() is None
    with pol.chat_turn("default", "u1") as active:
        assert active is False and pol._chat_user() is None
    with pol.chat_turn(None, "u1") as active:
        assert active is False and pol._chat_user() is None


def test_chat_turn_without_a_user_is_still_a_chat_turn(pol):
    with pol.chat_turn("nova_chat_", None):
        assert pol._chat_user() == ""


async def test_chat_turn_follows_awaits_and_tasks(pol):
    import asyncio
    seen = []

    async def inner():
        await asyncio.sleep(0)
        seen.append(pol._chat_user())

    with pol.chat_turn("nova_chat_u1", "u1"):
        await inner()
        await asyncio.create_task(inner())
    assert seen == ["u1", "u1"]


# ── requires_confirmation (used by authorize_now) ───────────────────────────

@pytest.mark.parametrize("domain,service,entity", OPENING[:4])
def test_without_a_chat_turn_the_opening_actions_are_unchanged(pol, vc, monkeypatch, domain, service, entity):
    _no_protection(vc, monkeypatch)
    assert pol.requires_confirmation(FakeHass(), domain, service, entity) is False


@pytest.mark.parametrize("domain,service,entity", OPENING)
def test_in_a_chat_turn_the_opening_actions_are_held(pol, vc, monkeypatch, domain, service, entity):
    _no_protection(vc, monkeypatch)
    with pol.chat_turn("nova_chat_u1", "u1"):
        assert pol.requires_confirmation(FakeHass(), domain, service, entity) is True


def test_in_a_chat_turn_authorize_now_defers_an_unlock(pol, vc, monkeypatch):
    _no_protection(vc, monkeypatch)
    req = pol.AuthorityRequest("lock", "unlock", "lock.front_door")
    assert pol.authorize_now(FakeHass(), req).allowed is True
    with pol.chat_turn("nova_chat_u1", "u1"):
        dec = pol.authorize_now(FakeHass(), req)
    assert (dec.allowed, dec.approval) == (False, "deferred")


@pytest.mark.parametrize("domain,service,entity", [
    ("lock", "lock", "lock.front_door"),
    ("light", "turn_on", "light.kitchen"),
    ("cover", "close_cover", "cover.garage_door"),
    ("alarm_control_panel", "alarm_arm_away", "alarm_control_panel.home"),
])
def test_chat_adds_no_friction_to_everything_else(pol, vc, monkeypatch, domain, service, entity):
    _no_protection(vc, monkeypatch)
    with pol.chat_turn("nova_chat_u1", "u1"):
        assert pol.requires_confirmation(FakeHass(), domain, service, entity) is False


# ── confirm_gate: the phone tap ─────────────────────────────────────────────

@pytest.mark.parametrize("domain,service,entity", OPENING)
async def test_chat_action_needs_the_users_phone_tap(pol, vc, monkeypatch, domain, service, entity):
    _no_protection(vc, monkeypatch)
    asked = _phones(vc, monkeypatch, {"u1": ["mobile_app_pixel"], "u2": ["mobile_app_iphone"]})
    sent = _tap(vc, monkeypatch, "approved")
    with pol.chat_turn("nova_chat_u1", "u1"):
        ok, note, approval = await pol.confirm_gate(FakeHass(), domain, service, entity, service)
    assert (ok, note, approval) == (True, "", "approved")
    assert asked == ["u1"]
    assert [s["services"] for s in sent] == [["mobile_app_pixel"]]
    assert "chat" in sent[0]["question"].lower()


@pytest.mark.parametrize("result", ["rejected", "expired", "error", "deferred"])
async def test_a_refused_unanswered_or_failed_tap_denies(pol, vc, monkeypatch, result):
    _no_protection(vc, monkeypatch)
    _phones(vc, monkeypatch, {"u1": ["mobile_app_pixel"]})
    _tap(vc, monkeypatch, result)
    with pol.chat_turn("nova_chat_u1", "u1"):
        ok, note, approval = await pol.confirm_gate(
            FakeHass(), "lock", "unlock", "lock.front_door", "unlock")
    assert ok is False and approval == result
    assert "phone" in note and "not done" in note


@pytest.mark.parametrize("user", ["u1", "", "someone_without_a_phone"])
async def test_no_phone_for_the_user_refuses_with_a_reason(pol, vc, monkeypatch, user):
    _no_protection(vc, monkeypatch)
    _phones(vc, monkeypatch, {"u2": ["mobile_app_iphone"]})

    async def must_not_ask(*a, **k):
        raise AssertionError("no tap can be asked when there is no phone")
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", must_not_ask)
    with pol.chat_turn("nova_chat_x", user):
        ok, note, approval = await pol.confirm_gate(
            FakeHass(), "lock", "unlock", "lock.front_door", "unlock")
    assert ok is False and approval == "denied"
    assert "no phone is registered" in note


async def test_a_lookup_failure_denies(pol, vc, monkeypatch):
    _no_protection(vc, monkeypatch)

    def boom(hass, user_id):
        raise RuntimeError("registry gone")
    monkeypatch.setattr(vc, "phone_services_for_user", boom)
    with pol.chat_turn("nova_chat_u1", "u1"):
        ok, note, approval = await pol.confirm_gate(
            FakeHass(), "alarm_control_panel", "alarm_disarm", "alarm_control_panel.home", "disarm")
    assert (ok, approval) == (False, "error")


async def test_a_tap_that_raises_denies(pol, vc, monkeypatch):
    _no_protection(vc, monkeypatch)
    _phones(vc, monkeypatch, {"u1": ["mobile_app_pixel"]})

    async def boom(*a, **k):
        raise RuntimeError("push failed")
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", boom)
    with pol.chat_turn("nova_chat_u1", "u1"):
        ok, _, approval = await pol.confirm_gate(
            FakeHass(), "lock", "unlock", "lock.front_door", "unlock")
    assert (ok, approval) == (False, "error")


async def test_authorize_is_still_the_one_authority_for_chat(pol, vc, monkeypatch):
    _no_protection(vc, monkeypatch)
    _phones(vc, monkeypatch, {"u1": ["mobile_app_pixel"]})
    _tap(vc, monkeypatch, "rejected")
    with pol.chat_turn("nova_chat_u1", "u1"):
        dec = await pol.authorize(FakeHass(), pol.AuthorityRequest(
            "lock", "unlock", "lock.front_door", user_id="u1"))
    assert (dec.allowed, dec.approval, dec.risk) == (False, "rejected", "high")


# ── nothing else changes ────────────────────────────────────────────────────

async def test_typed_assist_without_a_chat_id_is_unchanged(pol, vc, monkeypatch):
    _no_protection(vc, monkeypatch)

    async def must_not_ask(*a, **k):
        raise AssertionError("typed Assist must not be sent to the phone")
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", must_not_ask)
    with pol.chat_turn("some-other-id", "u1"):
        ok, note, approval = await pol.confirm_gate(
            FakeHass(), "lock", "unlock", "lock.front_door", "unlock")
    assert (ok, note, approval) == (True, "", "not_required")


async def test_voice_still_taps_every_phone(pol, vc, monkeypatch):
    monkeypatch.setattr(vc, "is_voice_satellite_device", lambda hass, did, **k: True)
    sent = []

    async def phone(hass, question, timeout=None, services=None):
        sent.append(services)
        return "approved"
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", phone)
    ok, _, approval = await pol.confirm_gate(
        FakeHass(), "lock", "unlock", "lock.front_door", "unlock", device_id="dev-sat-1")
    assert (ok, approval) == (True, "approved") and sent == [None]


async def test_automatic_behaviour_is_unchanged(pol, vc, monkeypatch):
    _no_protection(vc, monkeypatch)
    low = pol.authorize_now(FakeHass(), pol.AuthorityRequest(
        "light", "turn_on", "light.kitchen", source=pol.SOURCE_AUTOMATIC))
    high = pol.authorize_now(FakeHass(), pol.AuthorityRequest(
        "lock", "unlock", "lock.front_door", source=pol.SOURCE_AUTOMATIC))
    assert low.allowed is True
    assert (high.allowed, high.approval) == (False, "denied")


# ── voice_confirm: which phones belong to a user ────────────────────────────

class _State:
    def __init__(self, entity_id, attributes):
        self.entity_id, self.attributes = entity_id, attributes


class _Hass:
    pass


def _hass_with_people(people, services, entries, tracker_platform="mobile_app"):
    hass = _Hass()
    hass.states = types.SimpleNamespace(async_all=lambda domain=None: [
        _State(f"person.{n}", a) for n, a in people.items()] if domain == "person" else [])
    hass.services = types.SimpleNamespace(async_services=lambda: {"notify": {s: None for s in services}})
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: (types.SimpleNamespace(data=entries[eid]) if eid in entries else None))
    return hass


def _registry(monkeypatch, trackers):
    import sys
    er_mod = sys.modules["homeassistant.helpers.entity_registry"]
    reg = types.SimpleNamespace(async_get=lambda eid: trackers.get(eid))
    monkeypatch.setattr(er_mod, "async_get", lambda hass: reg)


def _slugify(monkeypatch):
    import sys
    util = sys.modules.get("homeassistant.util")
    if util is None or not hasattr(util, "slugify"):
        util = types.ModuleType("homeassistant.util")
        monkeypatch.setitem(sys.modules, "homeassistant.util", util)
    monkeypatch.setattr(util, "slugify",
                        lambda s: "_".join(str(s).lower().replace("-", " ").split()), raising=False)


def test_phone_services_are_the_users_own_mobile_app_trackers(vc, monkeypatch):
    _slugify(monkeypatch)
    _registry(monkeypatch, {
        "device_tracker.abi_phone": types.SimpleNamespace(platform="mobile_app", config_entry_id="e1"),
        "device_tracker.sam_phone": types.SimpleNamespace(platform="mobile_app", config_entry_id="e2"),
        "device_tracker.abi_router": types.SimpleNamespace(platform="unifi", config_entry_id="e3"),
    })
    hass = _hass_with_people(
        {"abi": {"user_id": "u1", "device_trackers": ["device_tracker.abi_phone", "device_tracker.abi_router"]},
         "sam": {"user_id": "u2", "device_trackers": ["device_tracker.sam_phone"]}},
        ["mobile_app_abi_pixel", "mobile_app_sam_iphone", "other"],
        {"e1": {"device_name": "Abi Pixel"}, "e2": {"device_name": "Sam iPhone"}})
    assert vc.phone_services_for_user(hass, "u1") == ["mobile_app_abi_pixel"]
    assert vc.phone_services_for_user(hass, "u2") == ["mobile_app_sam_iphone"]


def test_no_phones_when_the_user_has_no_person_or_no_phone(vc, monkeypatch):
    _slugify(monkeypatch)
    _registry(monkeypatch, {})
    hass = _hass_with_people(
        {"abi": {"user_id": "u1", "device_trackers": []}}, ["mobile_app_abi_pixel"], {})
    assert vc.phone_services_for_user(hass, "u1") == []
    assert vc.phone_services_for_user(hass, "u9") == []
    assert vc.phone_services_for_user(hass, "") == []
    assert vc.phone_services_for_user(hass, None) == []


def test_a_phone_with_no_notify_service_is_not_counted(vc, monkeypatch):
    _slugify(monkeypatch)
    _registry(monkeypatch, {"device_tracker.p": types.SimpleNamespace(platform="mobile_app", config_entry_id="e1")})
    hass = _hass_with_people(
        {"abi": {"user_id": "u1", "device_trackers": ["device_tracker.p"]}},
        ["mobile_app_someone_else"], {"e1": {"device_name": "Abi Pixel"}})
    assert vc.phone_services_for_user(hass, "u1") == []


async def test_the_push_goes_only_to_the_named_services(vc):
    calls = []

    class Services:
        def async_services(self):
            return {"notify": {"mobile_app_a": None, "mobile_app_b": None, "mobile_app_c": None}}

        async def async_call(self, domain, service, data=None, blocking=False, **kw):
            calls.append(service)

    class Bus:
        def async_listen(self, name, cb):
            return lambda: None

    hass = _Hass()
    hass.services, hass.bus = Services(), Bus()
    assert await vc.confirm_via_phone_only_typed(
        hass, "ok?", timeout=0.01, services=["mobile_app_b"]) == "expired"
    assert calls == ["mobile_app_b"]
    calls.clear()
    assert await vc.confirm_via_phone_only_typed(hass, "ok?", timeout=0.01) == "expired"
    assert sorted(calls) == ["mobile_app_a", "mobile_app_b", "mobile_app_c"]
    calls.clear()
    assert await vc.confirm_via_phone_only_typed(hass, "ok?", timeout=0.01, services=[]) == "error"
    assert calls == []
