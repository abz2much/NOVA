"""cognitive_core._emit_action and the delivery helpers behind it: where an
action is spoken or pushed, how critical bypasses quiet hours, sleep and every
mute, notifications only mode, habituation, and what happens when a service
fails.

Characterisation tests (8.7.15). The real audio_routing rules and the real
output_gate are used; only the speakers (tts_helper), the phone push and the
clock are faked. test_current_behaviour_* tests pin something odd that is not
being changed here (see "Found, not fixed" in the PR).
"""
import types

import pytest

from cognitive_safety_kit import (  # noqa: F401  (fixtures)
    _isolated_core, cc, clock, service_calls)

SPEAKER = "media_player.kitchen"


@pytest.fixture
def al(load):
    return load("action_log")


@pytest.fixture(autouse=True)
def audit_db(al, tmp_path, monkeypatch):
    monkeypatch.setattr(al, "_DEFAULT_DB", str(tmp_path / "audit.db"))


@pytest.fixture
def gate(load, monkeypatch):
    """The real output gate with a clean slate, restored afterwards."""
    og = load("output_gate")
    state = og._STATE
    monkeypatch.setattr(state, "mute_all", False)
    monkeypatch.setattr(state, "muted_entities", set())
    monkeypatch.setattr(state, "muted_categories", set())
    return og


@pytest.fixture
def quiet(load, monkeypatch):
    """Control the quiet hours clock: quiet["on"] = True puts us inside them."""
    flag = {"on": False}
    sd = load("sleep_detection")
    monkeypatch.setattr(sd, "_in_quiet_hours", lambda start, end: flag["on"])
    return flag


@pytest.fixture
def out(cc, fake_hass, load, monkeypatch, quiet):
    """Capture what _emit_action speaks and pushes. Someone is home, one
    announcement speaker is configured, and the TTS engine resolves."""
    tts = load("tts_helper")
    rec = types.SimpleNamespace(spoken=[], pushed=[], devices=[], tts_engine="tts.test",
                                 speakers=[SPEAKER])
    monkeypatch.setattr(tts, "resolve_tts_for_context",
                        lambda hass, ctx, engine, premium, contexts: rec.tts_engine)

    async def announce(hass, message, tts_entity, targets, context="", action_request_id=None):
        rec.spoken.append({"message": message, "tts": tts_entity, "targets": list(targets),
                           "context": context, "request_id": action_request_id})
    monkeypatch.setattr(tts, "async_announce", announce)

    async def push(hass, config, message, action_type, snapshot_url=None, *,
                   request_id=None, extra_data=None):
        rec.pushed.append({"message": message, "type": action_type, "snapshot": snapshot_url,
                           "request_id": request_id, "extra": extra_data})
    monkeypatch.setattr(cc, "_push_notification", push)

    async def every(hass, config, message, action_type, snapshot_url=None, *, request_id=None):
        rec.devices.append({"message": message, "type": action_type, "snapshot": snapshot_url,
                            "request_id": request_id})
    monkeypatch.setattr(cc, "_notify_all_devices", every)
    monkeypatch.setattr(cc, "_live_runtime_config",
                        lambda: {"announcement_speakers": rec.speakers})
    fake_hass.states.set(SPEAKER, "idle")
    fake_hass.states.set("person.username", "home")
    return rec


async def _emit(cc, hass, action, sleeping=False, config=None):
    await cc._emit_action(hass, {} if config is None else config, action, sleeping)


def _act(**kw):
    base = {"type": "test_alert", "urgency": "medium", "message": "hello"}
    base.update(kw)
    return base


# ── where an action goes, by urgency, sleep and quiet hours ─────────────────

async def test_critical_is_spoken_and_pushed_even_when_asleep_and_in_quiet_hours(
        cc, fake_hass, out, quiet):
    quiet["on"] = True
    await _emit(cc, fake_hass, _act(urgency="critical"), sleeping=True)
    assert [s["targets"] for s in out.spoken] == [[SPEAKER]]
    assert len(out.pushed) == 1


@pytest.mark.parametrize("sleeping,in_quiet", [(True, False), (False, True), (True, True)])
async def test_non_critical_while_asleep_or_in_quiet_hours_is_a_phone_push_only(
        cc, fake_hass, out, quiet, sleeping, in_quiet):
    quiet["on"] = in_quiet
    for urgency in ("high", "medium", "low"):
        await _emit(cc, fake_hass, _act(urgency=urgency, snapshot_url="https://x/s.jpg"), sleeping=sleeping)
    assert out.spoken == []
    assert [p["snapshot"] for p in out.pushed] == ["https://x/s.jpg"] * 3


async def test_quiet_hours_do_not_depend_on_anyone_being_in_a_bedroom(
        cc, fake_hass, out, quiet):
    quiet["on"] = True
    await _emit(cc, fake_hass, _act(urgency="medium"), sleeping=False)
    assert out.spoken == [] and len(out.pushed) == 1


async def test_notify_all_goes_to_every_device_instead_of_the_single_push(
        cc, fake_hass, out):
    await _emit(cc, fake_hass, _act(urgency="critical", notify_all=True, snapshot_url="u"))
    assert len(out.devices) == 1 and out.pushed == []
    assert out.devices[0]["snapshot"] == "u" and out.devices[0]["type"] == "test_alert"
    out.devices.clear()
    await _emit(cc, fake_hass, _act(notify_all=True), sleeping=True)                 # asleep, non critical
    assert len(out.devices) == 1 and out.pushed == []


async def test_a_spoken_medium_alert_is_not_also_pushed(cc, fake_hass, out):
    await _emit(cc, fake_hass, _act(urgency="medium"))
    assert len(out.spoken) == 1 and out.pushed == []


@pytest.mark.parametrize("urgency", ["critical", "high"])
async def test_critical_and_high_are_spoken_and_also_pushed(cc, fake_hass, out, urgency):
    await _emit(cc, fake_hass, _act(urgency=urgency))
    assert len(out.spoken) == 1 and len(out.pushed) == 1


async def test_one_request_id_links_the_speech_and_the_push(cc, fake_hass, out):
    await _emit(cc, fake_hass, _act(urgency="high"))
    assert out.spoken[0]["request_id"] == out.pushed[0]["request_id"] and out.spoken[0]["request_id"]
    assert out.spoken[0]["context"] == "sentinel"


async def test_nothing_is_spoken_without_a_speaker_or_a_tts_engine(cc, fake_hass, out):
    out.tts_engine = None
    await _emit(cc, fake_hass, _act(urgency="high"))
    assert out.spoken == [] and len(out.pushed) == 1                                   # high still reaches the phone
    out.tts_engine = "tts.test"
    out.speakers = []
    fake_hass.states.remove(SPEAKER)
    await _emit(cc, fake_hass, _act(urgency="medium"))
    assert out.spoken == []


async def test_a_broadcast_group_in_config_is_used_when_no_speakers_are_selected(
        cc, fake_hass, out):
    out.speakers = []
    fake_hass.states.set("media_player.house", "idle")
    await _emit(cc, fake_hass, _act(urgency="critical"), config={"broadcast_group": "media_player.house"})
    assert out.spoken[0]["targets"] == ["media_player.house"]


async def test_announcement_speakers_may_be_a_json_string_and_bad_json_is_ignored(
        cc, fake_hass, out):
    out.speakers = '["media_player.kitchen"]'
    await _emit(cc, fake_hass, _act(urgency="critical"))
    assert out.spoken[0]["targets"] == [SPEAKER]
    out.spoken.clear()
    out.speakers = "{not json"
    await _emit(cc, fake_hass, _act(urgency="critical"))
    assert out.spoken == []                                                            # no speakers: nothing spoken


async def test_the_action_defaults_are_medium_urgency_and_an_unknown_type(cc, fake_hass, out):
    await _emit(cc, fake_hass, {})
    assert out.spoken[0]["message"] == "" and cc._CORE.actions_taken == 1


async def test_each_emitted_action_is_counted(cc, fake_hass, out):
    for _ in range(3):
        await _emit(cc, fake_hass, _act())
    assert cc._CORE.actions_taken == 3


# ── mutes and the real output gate ──────────────────────────────────────────

async def test_the_real_gate_lets_critical_through_every_mute_but_stops_the_rest(gate):
    gate._STATE.mute_all = True
    gate._STATE.muted_entities.add("sensor.x")
    gate._STATE.muted_categories.add("cat")
    ok, reason = gate.can_announce(entity_id="sensor.x", category="cat", urgency="critical", message="m")
    assert (ok, reason) == (True, "critical bypass")
    for urgency in ("high", "medium", "low"):
        assert gate.can_announce(entity_id="sensor.y", category="c", urgency=urgency, message="m")[0] is False


async def test_a_critical_alert_is_spoken_under_a_blanket_shush(cc, fake_hass, out, gate):
    gate._STATE.mute_all = True
    await _emit(cc, fake_hass, _act(urgency="critical", type="intrusion_confirmed", notify_all=True))
    assert len(out.spoken) == 1 and len(out.devices) == 1


async def test_current_behaviour_cognitive_core_announcements_never_consult_the_mute_gate(
        cc, fake_hass, out, gate):
    """The output gate (shush, entity and category mutes, rate limit, dedup) is
    applied by the observer and a few monitors. _emit_action never calls it, so
    a blanket shush does not quiet a proactive offer, a freeze warning or a
    lockdown message."""
    gate._STATE.mute_all = True
    gate._STATE.muted_categories.add("test_alert")
    for urgency in ("low", "medium", "high"):
        out.spoken.clear()
        out.pushed.clear()
        await _emit(cc, fake_hass, _act(urgency=urgency, type="test_alert"))
        if urgency == "low":
            # low is only spoken from an occupied room, so it is held by routing, not by the mute
            assert out.spoken == []
        else:
            assert len(out.spoken) == 1
    assert gate._STATE.history == type(gate._STATE.history)()                     # and nothing is recorded in the gate


# ── notifications only mode ─────────────────────────────────────────────────

@pytest.fixture
def notify_only(load, monkeypatch):
    nc = load("nova_config")
    flag = {"on": True}
    monkeypatch.setattr(nc, "announce_notify_only", lambda hass: flag["on"])
    return flag


async def test_with_notify_only_on_a_medium_alert_goes_to_the_phone_not_the_speaker(
        cc, fake_hass, out, notify_only):
    await _emit(cc, fake_hass, _act(urgency="medium", snapshot_url="u"))
    assert out.spoken == []
    assert [(p["message"], p["snapshot"]) for p in out.pushed] == [("hello", "u")]


async def test_with_notify_only_on_a_high_alert_is_pushed_once_not_spoken(
        cc, fake_hass, out, notify_only):
    await _emit(cc, fake_hass, _act(urgency="high"))
    assert out.spoken == [] and len(out.pushed) == 1


async def test_with_notify_only_on_critical_still_speaks(cc, fake_hass, out, notify_only):
    await _emit(cc, fake_hass, _act(urgency="critical"))
    assert len(out.spoken) == 1 and len(out.pushed) == 1


async def test_with_notify_only_off_a_medium_alert_is_spoken_and_not_pushed(
        cc, fake_hass, out, notify_only):
    notify_only["on"] = False
    await _emit(cc, fake_hass, _act(urgency="medium"))
    assert len(out.spoken) == 1 and out.pushed == []


async def test_notify_only_with_notify_all_pushes_to_every_device(cc, fake_hass, out, notify_only):
    await _emit(cc, fake_hass, _act(urgency="medium", notify_all=True))
    assert out.spoken == [] and len(out.devices) == 1 and out.pushed == []


async def test_nobody_home_means_a_medium_alert_is_not_spoken(cc, fake_hass, out):
    fake_hass.states.set("person.username", "not_home")
    await _emit(cc, fake_hass, _act(urgency="medium"))
    assert out.spoken == []                                                         # routed to notify_only: no speech


# ── the rating buttons (adaptive awareness) ─────────────────────────────────

@pytest.fixture
def rating(load, monkeypatch):
    aa = load("adaptive_awareness")
    calls = []

    async def prompt(hass, config, message, decision_id):
        calls.append((message, decision_id))
    monkeypatch.setattr(aa, "async_send_rating_prompt", prompt)
    monkeypatch.setattr(aa, "rating_push_data", lambda decision_id: {"rate": decision_id})
    return calls


async def test_a_spoken_low_urgency_alert_with_a_decision_gets_a_silent_rating_prompt(
        cc, fake_hass, out, rating):
    await _emit(cc, fake_hass, _act(urgency="medium", decision_id=7))
    assert rating == [("hello", 7)]


@pytest.mark.parametrize("urgency", ["critical", "high"])
async def test_critical_and_high_alerts_never_get_a_separate_rating_prompt(
        cc, fake_hass, out, rating, urgency):
    await _emit(cc, fake_hass, _act(urgency=urgency, decision_id=7))
    assert rating == []
    assert out.pushed[0]["extra"] == {"rate": 7}                                    # the push itself carries the buttons


async def test_without_a_decision_there_are_no_rating_buttons(cc, fake_hass, out, rating):
    await _emit(cc, fake_hass, _act(urgency="high"))
    assert rating == [] and out.pushed[0]["extra"] == {}


async def test_a_phone_push_while_asleep_carries_the_rating_buttons(
        cc, fake_hass, out, rating, quiet):
    quiet["on"] = True
    await _emit(cc, fake_hass, _act(urgency="medium", decision_id=3))
    assert out.pushed[0]["extra"] == {"rate": 3}


async def test_a_failing_rating_prompt_does_not_stop_the_alert(cc, fake_hass, out, load, monkeypatch):
    aa = load("adaptive_awareness")

    async def boom(*a, **k):
        raise RuntimeError("prompt failed")
    monkeypatch.setattr(aa, "async_send_rating_prompt", boom)
    await _emit(cc, fake_hass, _act(urgency="medium", decision_id=7))
    assert len(out.spoken) == 1


def test_rating_data_is_empty_without_a_decision_or_when_it_breaks(cc, load, monkeypatch):
    assert cc._rating_data(None) == {}
    aa = load("adaptive_awareness")
    monkeypatch.setattr(aa, "rating_push_data", lambda d: (_ for _ in ()).throw(RuntimeError("x")))
    assert cc._rating_data(5) == {}


# ── habituation: the third day running goes quiet, emergencies never do ─────

@pytest.fixture
def habit(load, monkeypatch):
    hb = load("habituation")
    state = types.SimpleNamespace(quiet=set(), recorded=[], note="")
    monkeypatch.setattr(hb, "is_quiet", lambda key: key in state.quiet)
    monkeypatch.setattr(hb, "with_note", lambda key, message: message + state.note)
    monkeypatch.setattr(hb, "record", lambda key, entity_id="", now=None: state.recorded.append((key, entity_id)))
    return state


async def test_a_quiet_notification_is_dropped_and_not_counted(cc, fake_hass, out, habit):
    habit.quiet.add("lights:kitchen")
    await _emit(cc, fake_hass, _act(urgency="medium", pattern_key="lights:kitchen"))
    assert out.spoken == [] and out.pushed == [] and cc._CORE.actions_taken == 0


async def test_a_new_habit_key_is_recorded_and_gets_the_closing_note(cc, fake_hass, out, habit):
    habit.note = " (I'll stop mentioning it.)"
    await _emit(cc, fake_hass, _act(urgency="medium", pattern_key="lights:kitchen", entity_id="light.k"))
    assert out.spoken[0]["message"] == "hello (I'll stop mentioning it.)"
    assert habit.recorded == [("lights:kitchen", "light.k")]


@pytest.mark.parametrize("extra", [
    {"urgency": "critical"},
    {"type": "intrusion_investigating"},
    {"type": "lockdown_engaged"},
    {"type": "freeze_warning"},
    {"entity_id": "lock.front"},
    {"entity_id": "alarm_control_panel.home"},
])
async def test_emergencies_are_exempt_from_habituation_even_when_quiet(
        cc, fake_hass, out, habit, quiet, extra):
    habit.quiet.add("k")
    action = _act(pattern_key="k", **extra)
    if extra.get("urgency") != "critical":
        action.setdefault("urgency", "high")
    await _emit(cc, fake_hass, action)
    assert cc._CORE.actions_taken == 1 and habit.recorded == []                      # delivered, and not counted toward quiet


async def test_the_habit_key_is_habit_key_then_pattern_key_then_offer_key(cc, fake_hass, out, habit):
    await _emit(cc, fake_hass, _act(habit_key="h", pattern_key="p", offer_key="o"))
    await _emit(cc, fake_hass, _act(pattern_key="p", offer_key="o"))
    await _emit(cc, fake_hass, _act(offer_key="o"))
    await _emit(cc, fake_hass, _act())
    assert [k for k, _ in habit.recorded] == ["h", "p", "o"]


async def test_a_broken_habituation_store_does_not_stop_delivery(cc, fake_hass, out, load, monkeypatch):
    hb = load("habituation")
    monkeypatch.setattr(hb, "exempt", lambda **k: (_ for _ in ()).throw(RuntimeError("store broke")))
    await _emit(cc, fake_hass, _act(pattern_key="k"))
    assert len(out.spoken) == 1


# ── failure handling ────────────────────────────────────────────────────────

async def test_a_routing_failure_is_swallowed_and_logged(cc, fake_hass, out, load, monkeypatch, caplog):
    ar = load("audio_routing")
    monkeypatch.setattr(ar, "observer_speak_target",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("routing broke")))
    with caplog.at_level("WARNING"):
        await _emit(cc, fake_hass, _act(urgency="critical"))
    assert "action routing failed" in caplog.text and cc._CORE.actions_taken == 1


async def test_a_failing_announcement_does_not_skip_the_phone_push(
        cc, fake_hass, out, load, monkeypatch, caplog):
    """Speech and push have their own error handling (8.7.16). Before, they
    shared one try block, so an announcement that raised skipped the push for a
    critical alert."""
    tts = load("tts_helper")

    async def boom(*a, **k):
        raise RuntimeError("speaker offline")
    monkeypatch.setattr(tts, "async_announce", boom)
    with caplog.at_level("WARNING"):
        await _emit(cc, fake_hass, _act(urgency="critical"))
    assert len(out.pushed) == 1 and cc._CORE.actions_taken == 1
    assert "action routing failed" in caplog.text
    out.pushed.clear()
    await _emit(cc, fake_hass, _act(urgency="high", notify_all=True))
    assert len(out.devices) == 1                                                     # every device path too


async def test_a_failing_push_does_not_undo_the_speech_and_is_logged(
        cc, fake_hass, out, monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError("push service down")
    monkeypatch.setattr(cc, "_push_notification", boom)
    monkeypatch.setattr(cc, "_notify_all_devices", boom)
    with caplog.at_level("WARNING"):
        await _emit(cc, fake_hass, _act(urgency="critical"))
        await _emit(cc, fake_hass, _act(urgency="critical", notify_all=True))
    assert len(out.spoken) == 2
    assert caplog.text.count("action push failed") == 2


async def test_a_failing_push_while_asleep_is_logged_not_raised(
        cc, fake_hass, out, monkeypatch, caplog, quiet):
    async def boom(*a, **k):
        raise RuntimeError("push service down")
    monkeypatch.setattr(cc, "_push_notification", boom)
    with caplog.at_level("WARNING"):
        await _emit(cc, fake_hass, _act(urgency="high"), sleeping=True)
    assert "action push failed" in caplog.text and cc._CORE.actions_taken == 1


async def test_a_routing_failure_still_pushes_a_critical_or_high_alert_but_not_a_medium_one(
        cc, fake_hass, out, load, monkeypatch):
    ar = load("audio_routing")
    monkeypatch.setattr(ar, "observer_speak_target",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("routing broke")))
    for urgency, pushed in (("critical", 1), ("high", 1), ("medium", 0)):
        out.pushed.clear()
        await _emit(cc, fake_hass, _act(urgency=urgency))
        assert len(out.pushed) == pushed, urgency
    assert out.spoken == []


async def test_critical_still_bypasses_sleep_quiet_hours_and_a_blanket_shush_after_the_split(
        cc, fake_hass, out, quiet, gate):
    quiet["on"] = True
    gate._STATE.mute_all = True
    await _emit(cc, fake_hass, _act(urgency="critical"), sleeping=True)
    assert len(out.spoken) == 1 and len(out.pushed) == 1
    out.spoken.clear()
    out.pushed.clear()
    await _emit(cc, fake_hass, _act(urgency="medium"), sleeping=True)               # a non critical alert is still push only
    assert out.spoken == [] and len(out.pushed) == 1


async def test_an_unusable_speaker_setting_means_a_critical_alert_is_pushed_but_not_spoken(
        cc, fake_hass, out, monkeypatch):
    monkeypatch.setattr(cc, "_live_runtime_config", lambda: {"announcement_speakers": 5})
    await _emit(cc, fake_hass, _act(urgency="critical"), config={"broadcast_group": None})
    assert out.spoken == [] and len(out.pushed) == 1                                  # nothing usable: notify only


# ── _push_notification ──────────────────────────────────────────────────────

@pytest.fixture
def sent(load, monkeypatch):
    nt = load("notify_targets")
    calls = []

    async def send(hass, config, data, **kw):
        calls.append((data, kw))
        return ["notify.x"]
    monkeypatch.setattr(nt, "async_send_configured_notifications", send)
    return calls


async def test_push_notification_builds_title_message_image_and_rating_data(cc, fake_hass, sent):
    await cc._push_notification(fake_hass, {}, "Front door open", "intrusion_confirmed",
                                "https://x/snap.jpg", request_id="r1", extra_data={"actions": ["a"]})
    (data, kw), = sent
    assert data["message"] == "Front door open"
    assert data["title"] == cc._notify_i18n().title("intrusion_confirmed", "en")
    assert data["data"] == {"image": "https://x/snap.jpg", "attachment": {"url": "https://x/snap.jpg"},
                            "actions": ["a"]}
    assert kw == {"request_id": "r1", "action": "cognitive_alert", "source": "proactive",
                  "requested_state": "intrusion_confirmed"}


async def test_push_notification_without_extras_sends_no_data_block(cc, fake_hass, sent):
    await cc._push_notification(fake_hass, {}, "hi", "x")
    assert "data" not in sent[0][0]


# ── _notify_all_devices ─────────────────────────────────────────────────────

@pytest.fixture
def phones(cc, fake_hass, monkeypatch):
    pushed = []

    async def push(hass, config, message, action_type, snapshot_url=None, *, request_id=None, extra_data=None):
        pushed.append((message, action_type, snapshot_url, request_id))
    monkeypatch.setattr(cc, "_push_notification", push)
    return pushed


async def test_notify_all_calls_every_mobile_app_service_with_the_snapshot(
        cc, fake_hass, phones, al):
    fake_hass.services.register("notify", "mobile_app_a")
    fake_hass.services.register("notify", "mobile_app_b")
    fake_hass.services.register("notify", "slack")                                  # not a phone
    await cc._notify_all_devices(fake_hass, {}, "msg", "test_alert", "https://x/s.jpg", request_id="r9")
    calls = service_calls(fake_hass, "notify", "mobile_app_a") + service_calls(fake_hass, "notify", "mobile_app_b")
    assert len(calls) == 2 and service_calls(fake_hass, "notify", "slack") == []
    for _d, _s, payload in calls:
        assert payload["message"] == "msg" and payload["data"]["image"] == "https://x/s.jpg"
    assert phones == []                                                             # no fallback needed
    (request,) = al.page_requests()["requests"]
    assert request["request_id"] == "r9" and len(request["targets"]) == 2


async def test_notify_all_falls_back_to_the_single_push_when_no_phone_service_exists(
        cc, fake_hass, phones):
    await cc._notify_all_devices(fake_hass, {}, "msg", "test_alert")
    assert len(phones) == 1 and phones[0][3]                                         # the fallback reuses one request id


async def test_notify_all_falls_back_when_every_phone_call_fails(cc, fake_hass, phones, al):
    fake_hass.services.register("notify", "mobile_app_a")

    async def boom(domain, service, data=None, blocking=False, **kw):
        raise RuntimeError("push failed")
    fake_hass.services.async_call = boom
    await cc._notify_all_devices(fake_hass, {}, "msg", "test_alert", request_id="r1")
    assert len(phones) == 1
    (request,) = al.page_requests()["requests"]
    assert request["targets"][0]["execution_result"] == "failed"


async def test_notify_all_survives_the_service_list_breaking(cc, fake_hass, phones):
    fake_hass.services.async_services = lambda: (_ for _ in ()).throw(RuntimeError("no registry"))
    await cc._notify_all_devices(fake_hass, {}, "msg", "test_alert")
    assert len(phones) == 1


async def test_a_confirmed_intrusion_also_leaves_a_persistent_notification(
        cc, fake_hass, phones, al):
    fake_hass.services.register("notify", "mobile_app_a")
    await cc._notify_all_devices(fake_hass, {}, "msg", "intrusion_confirmed", request_id="r2")
    (_d, _s, payload), = service_calls(fake_hass, "persistent_notification", "create")
    assert payload["notification_id"] == "nova_intrusion" and payload["message"] == "msg"
    await cc._notify_all_devices(fake_hass, {}, "msg", "intrusion_unresolved", request_id="r3")
    assert len(service_calls(fake_hass, "persistent_notification", "create")) == 1   # only for a confirmed one


async def test_a_failing_persistent_notification_is_audited_not_raised(cc, fake_hass, phones, al):
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        if domain == "persistent_notification":
            raise RuntimeError("no panel")
        await real(domain, service, data, blocking=blocking, **kw)
    fake_hass.services.async_call = call
    await cc._notify_all_devices(fake_hass, {}, "msg", "intrusion_confirmed", request_id="r4")
    rows = [t for p in al.page_requests()["requests"] for t in p["targets"]
            if t["domain"] == "persistent_notification"]
    assert [r["execution_result"] for r in rows] == ["failed"]


# ── _execute_action_data ────────────────────────────────────────────────────

@pytest.mark.parametrize("data", [
    None, {}, {"domain": "light"}, {"domain": "light", "service": "turn_on"},
    {"service": "turn_on", "entity_ids": ["light.a"]},
    {"domain": "light", "service": "turn_on", "entity_ids": []}])
async def test_an_incomplete_action_does_nothing(cc, fake_hass, data):
    assert await cc._execute_action_data(fake_hass, data) is False
    assert fake_hass.service_calls == []


async def test_a_good_action_calls_the_service_and_is_audited(cc, fake_hass, al):
    ok = await cc._execute_action_data(
        fake_hass, {"domain": "light", "service": "turn_on", "entity_ids": ["light.a", "light.b"],
                    "service_data": {"brightness": 100}}, source="proactive_accepted")
    assert ok is True
    assert fake_hass.service_calls == [
        ("light", "turn_on", {"entity_id": ["light.a", "light.b"], "brightness": 100})]
    (request,) = al.page_requests()["requests"]
    assert request["action"] == "proactive_offer_execute" and request["source"] == "proactive_accepted"
    assert request["targets"][0]["entity_id"] == "light.a, light.b"
    assert request["targets"][0]["execution_result"] == "accepted"


async def test_a_failing_action_returns_false_and_is_audited_as_failed(cc, fake_hass, al):
    async def boom(*a, **k):
        raise RuntimeError("device offline")
    fake_hass.services.async_call = boom
    ok = await cc._execute_action_data(
        fake_hass, {"domain": "light", "service": "turn_on", "entity_ids": ["light.a"]})
    assert ok is False
    (request,) = al.page_requests()["requests"]
    assert request["targets"][0]["execution_result"] == "failed"
    assert request["targets"][0]["reason_code"] == "service_call_failed"


async def test_a_string_entity_id_and_a_caller_request_id_are_accepted(cc, fake_hass, al):
    ok = await cc._execute_action_data(
        fake_hass, {"domain": "light", "service": "turn_off", "entity_ids": "light.a"},
        request_id="mine")
    assert ok is True and al.get_request("mine")["targets"][0]["entity_id"] == "light.a"


@pytest.mark.parametrize("kind,text", [
    ("proactive_lights", "I turned the lights on for you — it was dark and you were there."),
    ("proactive_stale_light", "I turned off a light left on in an empty room to save energy."),
    ("proactive_hvac", "I set the climate back to eco — no one's home."),
    ("something_else", "I handled 2 device(s) for you automatically."),
])
def test_the_autonomous_done_message(cc, kind, text):
    assert cc._autonomous_done_message(
        {"type": kind, "action_data": {"entity_ids": ["a", "b"]}}) == text


def test_the_autonomous_done_message_copes_with_no_action_data(cc):
    assert cc._autonomous_done_message({}) == "I handled 0 device(s) for you automatically."
