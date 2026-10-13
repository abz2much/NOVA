"""delivery.py: one delivery request for every proactive message (8.44.0).

decide() is checked rule by rule: critical, the output gate, the
Announcements switch, quiet hours and sleep, "Notifications only", and the
person's own reminders and briefings. async_deliver() is checked with fake
speakers and phones. Nothing touches a real speaker or phone.
"""
import pytest


@pytest.fixture
def d(load):
    return load("delivery")


def _req(d, **kw):
    kw.setdefault("source", "test")
    kw.setdefault("message", "The washer is done.")
    return d.DeliveryRequest(**kw)


def _say(dec):
    return (dec.speak, dec.push)


# ── decide ──────────────────────────────────────────────────────────────────

def test_a_normal_message_is_spoken(d):
    assert _say(d.decide(_req(d), d.Conditions())) == (True, False)


@pytest.mark.parametrize("cond", [
    dict(gate_allowed=False, gate_reason="shush"),
    dict(announcements_on=False),
    dict(quiet_hours=True),
    dict(asleep=True),
    dict(notify_only=True),
])
def test_critical_ignores_every_rule(d, cond):
    req = _req(d, urgency="critical")
    assert _say(d.decide(req, d.Conditions(**cond))) == (True, True)


def test_critical_never_pushes_a_speakers_only_source(d):
    req = _req(d, urgency="critical", push=d.PUSH_NEVER)
    assert _say(d.decide(req, d.Conditions())) == (True, False)


def test_the_gate_holds_everything_but_an_always_push(d):
    for push in d.PUSH_MODES:
        dec = d.decide(_req(d, urgency="high", push=push),
                       d.Conditions(gate_allowed=False, gate_reason="shush"))
        assert _say(dec) == (False, push == d.PUSH_ALWAYS)
        assert "shush" in dec.reason


@pytest.mark.parametrize("push,urgency,pushed", [
    ("fallback", "high", False),
    ("high", "medium", False),
    ("high", "high", True),
    ("always", "low", True),
    ("never", "high", False),
])
def test_announcements_off_speaks_nothing(d, push, urgency, pushed):
    dec = d.decide(_req(d, push=push, urgency=urgency),
                   d.Conditions(announcements_on=False))
    assert _say(dec) == (False, pushed)


def test_notify_only_goes_to_the_phones(d):
    cond = d.Conditions(notify_only=True)
    assert _say(d.decide(_req(d), cond)) == (False, True)
    assert _say(d.decide(_req(d, push=d.PUSH_NEVER), cond)) == (False, False)


@pytest.mark.parametrize("cond", [dict(quiet_hours=True), dict(asleep=True)])
def test_at_night_only_what_matters_goes_to_the_phones(d, cond):
    c = d.Conditions(**cond)
    for urgency in ("low", "medium"):
        assert _say(d.decide(_req(d, urgency=urgency), c)) == (False, False)
    assert _say(d.decide(_req(d, urgency="high"), c)) == (False, True)
    assert _say(d.decide(_req(d, requested_by_user=True), c)) == (False, True)
    assert _say(d.decide(_req(d, push=d.PUSH_ALWAYS), c)) == (False, True)
    assert _say(d.decide(_req(d, urgency="high", push=d.PUSH_NEVER), c)) == (False, False)


def test_quiet_hours_come_before_notify_only(d):
    dec = d.decide(_req(d), d.Conditions(quiet_hours=True, notify_only=True))
    assert dec.reason == "quiet hours"


@pytest.mark.parametrize("push,urgency,pushed", [
    ("fallback", "high", False),
    ("high", "high", True),
    ("high", "medium", False),
    ("always", "low", True),
])
def test_spoken_messages_push_as_the_source_asks(d, push, urgency, pushed):
    dec = d.decide(_req(d, push=push, urgency=urgency), d.Conditions())
    assert _say(dec) == (True, pushed)


def test_your_own_reminder_skips_shush_and_the_switch(d):
    req = _req(d, requested_by_user=True)
    for cond in (dict(gate_allowed=False, gate_reason="shush"),
                 dict(announcements_on=False)):
        assert _say(d.decide(req, d.Conditions(**cond))) == (True, False)


def test_your_own_reminder_still_respects_quiet_hours_and_sleep(d):
    req = _req(d, requested_by_user=True)
    for cond in (dict(quiet_hours=True), dict(asleep=True), dict(notify_only=True)):
        assert _say(d.decide(req, d.Conditions(**cond))) == (False, True)


def test_decide_is_pure(d):
    import inspect
    src = inspect.getsource(d.decide)
    assert "hass" not in src and "import" not in src


# ── async_deliver ───────────────────────────────────────────────────────────

@pytest.fixture
def fake(d, monkeypatch):
    """Fake rules, speakers and phones. Returns what was sent."""
    sent = {"spoken": [], "pushed": [], "recorded": [], "cond": d.Conditions(),
            "targets": ["media_player.hall"], "speech_works": True}

    monkeypatch.setattr(d, "conditions", lambda hass, req: sent["cond"])
    monkeypatch.setattr(d, "_speakers", lambda hass, req, cond: (list(sent["targets"]), "source"))
    monkeypatch.setattr(d, "_habit_note", lambda req: req.spoken or req.message)

    async def speak(hass, req, message, targets, request_id):
        sent["spoken"].append((message, targets))
        return sent["speech_works"]

    async def push(hass, req, request_id):
        sent["pushed"].append(req.message)
        return ["notify.phone"]

    async def record(hass, req, cond, out):
        sent["recorded"].append((req.record_kind, out.spoken, bool(out.pushed)))

    monkeypatch.setattr(d, "_speak", speak)
    monkeypatch.setattr(d, "_push", push)
    monkeypatch.setattr(d, "_record", record)
    return sent


async def test_deliver_speaks(d, fake, fake_hass):
    out = await d.async_deliver(fake_hass, _req(d))
    assert out.spoken and not out.pushed
    assert fake["spoken"] == [("The washer is done.", ["media_player.hall"])]


async def test_deliver_pushes_when_no_speaker_takes_it(d, fake, fake_hass):
    fake["targets"] = []
    out = await d.async_deliver(fake_hass, _req(d))
    assert not out.spoken and out.pushed == ["notify.phone"]
    assert out.decision.reason == "no speaker took it"


async def test_deliver_pushes_when_speech_fails(d, fake, fake_hass):
    fake["speech_works"] = False
    out = await d.async_deliver(fake_hass, _req(d))
    assert not out.spoken and out.pushed


async def test_a_speakers_only_source_is_never_pushed(d, fake, fake_hass):
    fake["targets"] = []
    out = await d.async_deliver(fake_hass, _req(d, push=d.PUSH_NEVER))
    assert not out.spoken and not out.pushed


async def test_deliver_records_when_asked(d, fake, fake_hass):
    fake["cond"] = d.Conditions(quiet_hours=True)
    await d.async_deliver(fake_hass, _req(d, urgency="high", record_kind="package"))
    await d.async_deliver(fake_hass, _req(d))
    assert fake["recorded"] == [("package", False, True)]


async def test_a_held_message_reaches_no_one(d, fake, fake_hass):
    fake["cond"] = d.Conditions(gate_allowed=False, gate_reason="shush")
    out = await d.async_deliver(fake_hass, _req(d))
    assert not out.decision.delivered
    assert fake["spoken"] == [] and fake["pushed"] == []


async def test_routed_to_the_phones_pushes_an_everyday_alert(d, fake, monkeypatch, fake_hass):
    # 8.45.0: speaker routing sends it to the phones while nobody is home.
    monkeypatch.setattr(d, "_speakers", lambda hass, req, cond: ([], "notify_only"))
    out = await d.async_deliver(fake_hass, _req(d, push=d.PUSH_HIGH))
    assert not out.spoken and out.pushed
    assert out.decision.reason == "routed to the phones"


async def test_no_speaker_drops_an_everyday_alert_that_pushes_only_when_high(
        d, fake, fake_hass):
    fake["targets"] = []
    out = await d.async_deliver(fake_hass, _req(d, push=d.PUSH_HIGH))
    assert not out.spoken and not out.pushed


async def test_notifications_only_with_no_phone_speaks_after_all(d, fake, monkeypatch, fake_hass):
    fake["cond"] = d.Conditions(notify_only=True)

    async def no_phone(hass, req, request_id):
        return []
    monkeypatch.setattr(d, "_push", no_phone)
    out = await d.async_deliver(fake_hass, _req(d, speakers=("media_player.hall",)))
    assert out.spoken and out.decision.reason == "notifications only, no phone took it"


async def test_a_source_push_function_gets_the_shared_request_id(d, fake, fake_hass):
    ids = []

    async def push(request_id):
        ids.append(request_id)
        return ["phone"]
    out = await d.async_deliver(fake_hass, _req(d, urgency="high", push=d.PUSH_ALWAYS),
                                push=push)
    assert ids == [out.request_id] and out.pushed == ["phone"] and fake["pushed"] == []


def test_motion_counts_leaves_presence_to_the_speaker_routing(d, load, monkeypatch, fake_hass):
    seen = []
    ar = load("audio_routing")
    monkeypatch.setattr(ar, "observer_speak_target",
                        lambda hass, **kw: seen.append(kw["authoritative_anyone_home"])
                        or (["media_player.hall"], "local"))
    cond = d.Conditions(anyone_home=False)
    d._speakers(fake_hass, _req(d), cond)
    d._speakers(fake_hass, _req(d, motion_counts=True), cond)
    assert seen == [False, None]


async def test_every_outcome_is_recorded_with_its_source(d, load, monkeypatch, fake_hass):
    og = load("output_gate")
    rows = []
    monkeypatch.setattr(og, "record_announcement", lambda **kw: rows.append(kw))
    monkeypatch.setattr(d, "conditions", lambda hass, req: d.Conditions(quiet_hours=True))
    out = await d.async_deliver(fake_hass, _req(d, source="appliance", dedup_key="k1"))
    assert rows == [dict(entity_id="", category="appliance", urgency="medium",
                         message="The washer is done.", was_spoken=False, dedup_key="k1",
                         habit_key=None,
                         source="appliance", pushed=False, outcome="quiet hours",
                         request_id=out.request_id)]


def test_a_repeat_label_holds_a_repeat_whatever_its_words(load, monkeypatch):
    og = load("output_gate")
    monkeypatch.setattr(og, "_STATE", og.GateState())
    hab = load("habituation")
    monkeypatch.setattr(hab, "is_quiet", lambda k: False)
    monkeypatch.setattr(hab, "record", lambda *a, **k: None)
    monkeypatch.setattr(load("database"), "save_activity", lambda **kw: None)
    og.record_announcement(entity_id="", category="offer", urgency="medium",
                           message="Shall I turn the porch light on?", was_spoken=True,
                           dedup_key="offer:porch")
    assert og.can_announce(entity_id="", category="offer", urgency="medium",
                           message="Want the outside light?", dedup_key="offer:porch") == (
        False, "repeat of a recent message")
    assert og.can_announce(entity_id="", category="offer", urgency="medium",
                           message="Want the outside light?", dedup_key="offer:hall")[0]


# ── senders ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sent_requests(d, monkeypatch):
    """Capture every DeliveryRequest a sender makes."""
    reqs = []

    async def deliver(hass, req):
        reqs.append(req)
        return d.Outcome(decision=d.Decision(True, False))
    monkeypatch.setattr(d, "async_deliver", deliver)
    return reqs


def _reminder_watcher(load, fake_hass, monkeypatch, due):
    import sys
    import types
    ev = types.ModuleType("homeassistant.helpers.event")
    ev.async_track_time_interval = lambda *a, **k: (lambda: None)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)
    rem = load("reminders")
    monkeypatch.setattr(rem, "get_due_reminders", lambda: [dict(r) for r in due])
    monkeypatch.setattr(rem, "mark_fired", lambda rid: None)
    monkeypatch.setattr(rem, "_advance_repeating", lambda r: None)
    monkeypatch.setattr(rem, "get_presence_summary", lambda h: {"anyone_home": True})

    async def run(fn, *a):
        return fn(*a)
    fake_hass.async_add_executor_job = run
    return rem.ReminderWatcher(fake_hass, lambda: "", lambda: "tts.x",
                               lambda: ["media_player.hall"])


_DUE = {"id": 1, "label": "bins out", "respect_quiet": 1, "require_home": 0}


async def test_a_reminder_goes_through_the_delivery_path(
        d, load, fake_hass, monkeypatch, sent_requests):
    monkeypatch.setattr(d, "in_quiet_hours", lambda hass: False)
    w = _reminder_watcher(load, fake_hass, monkeypatch, [_DUE])
    await w._check_async()
    (req,) = sent_requests
    assert req.message == "Reminder: bins out."
    assert req.requested_by_user and not req.quiet_ok
    assert req.speakers == ("media_player.hall",) and req.tts_entity == "tts.x"


async def test_a_reminder_waits_for_the_households_quiet_hours(
        d, load, fake_hass, monkeypatch, sent_requests):
    monkeypatch.setattr(d, "in_quiet_hours", lambda hass: True)
    w = _reminder_watcher(load, fake_hass, monkeypatch, [_DUE])
    await w._check_async()
    assert sent_requests == []


async def test_a_reminder_set_to_ignore_quiet_hours_is_sent(
        d, load, fake_hass, monkeypatch, sent_requests):
    monkeypatch.setattr(d, "in_quiet_hours", lambda hass: True)
    w = _reminder_watcher(load, fake_hass, monkeypatch, [dict(_DUE, respect_quiet=0)])
    await w._check_async()
    (req,) = sent_requests
    assert req.quiet_ok


def test_quiet_ok_speaks_in_quiet_hours_but_not_asleep_or_past_notify_only(d):
    req = _req(d, requested_by_user=True, quiet_ok=True)
    assert _say(d.decide(req, d.Conditions(quiet_hours=True))) == (True, False)
    assert _say(d.decide(req, d.Conditions(quiet_hours=True, asleep=True))) == (False, True)
    assert _say(d.decide(req, d.Conditions(notify_only=True))) == (False, True)


def test_reminders_keep_no_quiet_hours_of_their_own():
    import pathlib
    rem = (pathlib.Path(__file__).resolve().parents[2] / "custom_components"
           / "nova" / "reminders.py").read_text(encoding="utf-8")
    assert "QUIET_START" not in rem and "def _in_quiet_hours" not in rem


async def test_a_source_can_speak_its_own_way(d, fake, fake_hass):
    said = []

    async def say(words):
        said.append(words)
        return True
    out = await d.async_deliver(fake_hass, _req(d), speak=say)
    assert out.spoken and said == ["The washer is done."] and fake["spoken"] == []


async def test_own_speech_is_skipped_when_the_rules_hold_it(d, fake, fake_hass):
    fake["cond"] = d.Conditions(quiet_hours=True)
    said = []

    async def say(words):
        said.append(words)
        return True
    out = await d.async_deliver(fake_hass, _req(d, urgency="high"), speak=say)
    assert said == [] and out.pushed


async def test_a_hazard_pushes_first_and_speaks_its_own_words(d, fake, fake_hass, monkeypatch):
    order = []

    async def speak(hass, req, message, targets, request_id):
        order.append(("speak", message))
        return True

    async def push(hass, req, request_id):
        order.append(("push", req.message))
        return ["all"]
    monkeypatch.setattr(d, "_speak", speak)
    monkeypatch.setattr(d, "_push", push)
    await d.async_deliver(fake_hass, _req(d, message="Long text.", spoken="Short.",
                                           push=d.PUSH_ALWAYS, push_first=True))
    assert order == [("push", "Long text."), ("speak", "Short.")]


def test_shush_still_lets_an_always_pushed_message_reach_the_phones(d):
    cond = d.Conditions(gate_allowed=False, gate_reason="shush")
    assert _say(d.decide(_req(d, push=d.PUSH_ALWAYS), cond)) == (False, True)
    assert _say(d.decide(_req(d, push=d.PUSH_HIGH, urgency="high"), cond)) == (False, False)


def test_phone_only_is_never_spoken(d):
    req = _req(d, phone_only=True, push=d.PUSH_ALWAYS)
    assert _say(d.decide(req, d.Conditions())) == (False, True)


def test_a_night_level_hazard_speaks_through_quiet_hours_and_sleep(d):
    req = _req(d, urgency="high", quiet_ok=True, sleep_ok=True, push=d.PUSH_ALWAYS)
    assert _say(d.decide(req, d.Conditions(quiet_hours=True, asleep=True))) == (True, True)
    # shush and the Announcements switch still hold the speech
    assert _say(d.decide(req, d.Conditions(announcements_on=False))) == (False, True)
