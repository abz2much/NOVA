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


def test_the_gate_holds_everything(d):
    for push in d.PUSH_MODES:
        dec = d.decide(_req(d, urgency="high", push=push),
                       d.Conditions(gate_allowed=False, gate_reason="shush"))
        assert _say(dec) == (False, False)
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


@pytest.mark.parametrize("cond", [dict(quiet_hours=True), dict(asleep=True),
                                  dict(notify_only=True)])
def test_quiet_sleep_and_notify_only_go_to_the_phones(d, cond):
    assert _say(d.decide(_req(d), d.Conditions(**cond))) == (False, True)
    assert _say(d.decide(_req(d, push=d.PUSH_NEVER), d.Conditions(**cond))) == (False, False)


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
    monkeypatch.setattr(d, "_speakers", lambda hass, req, cond: list(sent["targets"]))
    monkeypatch.setattr(d, "_habit_note", lambda req: req.message)

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
    await d.async_deliver(fake_hass, _req(d, record_kind="package"))
    await d.async_deliver(fake_hass, _req(d))
    assert fake["recorded"] == [("package", False, True)]


async def test_a_held_message_reaches_no_one(d, fake, fake_hass):
    fake["cond"] = d.Conditions(gate_allowed=False, gate_reason="shush")
    out = await d.async_deliver(fake_hass, _req(d))
    assert not out.decision.delivered
    assert fake["spoken"] == [] and fake["pushed"] == []
