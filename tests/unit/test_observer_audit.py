"""8.47.0: every Observer alert decision leaves one compact audit entry.

The entry holds the event and when it happened, the reading it was decided
on (its snapshot id and only the facts used), the rule and reason, the
result (alerted, suppressed or ignored) and what delivery did. Routine
events that never reach a decision get none. A critical hazard always gets
one.

All state is fake. Nothing touches a real speaker or phone.
"""
import types

import pytest

SPEAKER = "media_player.hall"


@pytest.fixture
def observer(load, fake_hass, monkeypatch):
    o = load("observer")
    dr = load("decision_record")
    out = {"entries": [], "spoken": [], "pushed": [], "allow": True,
           "classify": {"worth_considering": True, "urgency": "medium",
                        "category": "doors_windows"},
           "decide": {"speak": True, "message": "The back door is open.",
                      "urgency": "medium"},
           "trace": {"origin": "rule", "rule": "entry_open"}}

    async def _classify(*a, **kw):
        return dict(out["classify"])

    async def _decide(*a, trace=None, **kw):
        if trace is not None and out["trace"]:
            trace.update(out["trace"])
        return dict(out["decide"])

    async def _announce(hass, message, tts_entity, targets, **kw):
        out["spoken"].append(message)
        return True

    async def _push(hass, req, request_id):
        out["pushed"].append(req.message)
        return ["notify.phone"]

    monkeypatch.setattr(dr, "record", lambda **kw: out["entries"].append(kw) or 1)
    monkeypatch.setattr(o.classifier, "classify", _classify)
    monkeypatch.setattr(o.reasoning_loop, "decide", _decide)
    dl, tts = load("delivery"), load("tts_helper")
    monkeypatch.setattr(tts, "async_announce", _announce)
    monkeypatch.setattr(tts, "resolve_tts_for_context", lambda *a, **k: "tts.x")
    monkeypatch.setattr(dl, "_push", _push)
    monkeypatch.setattr(dl, "_announcement_speakers", lambda hass: [SPEAKER])
    monkeypatch.setattr(o.output_gate, "habit_note", lambda **kw: kw["message"])
    monkeypatch.setattr(o.output_gate, "can_announce",
                        lambda **kw: (True, "") if out["allow"] else (False, "muted"))
    monkeypatch.setattr(o.output_gate, "record_announcement", lambda **kw: None)
    monkeypatch.setattr(o.output_gate, "recent_announcements", lambda n=5: [])
    monkeypatch.setattr(load("database"), "save_activity", lambda **k: None)
    monkeypatch.setattr(o.audio_routing, "entity_area", lambda h, e: None)
    monkeypatch.setattr(o.audio_routing, "currently_occupied_areas", lambda h: [])
    monkeypatch.setattr(o.sleep_detection, "is_sleeping", lambda *a, **k: (False, "awake"))
    monkeypatch.setattr(o.sleep_detection, "_in_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(o._STATE, "hass", fake_hass)
    monkeypatch.setattr(o._STATE, "config", {})
    fake_hass.states.set(SPEAKER, "idle")
    fake_hass.states.set("person.a", "not_home")

    def run(entity_id="binary_sensor.back_door", device_class="door", old="off", new="on"):
        mk = lambda s: types.SimpleNamespace(  # noqa: E731
            state=s, attributes={"friendly_name": "Back door", "device_class": device_class})
        return o._process_event(types.SimpleNamespace(data={
            "entity_id": entity_id, "old_state": mk(old), "new_state": mk(new)}))
    out["run"] = run
    return out


async def test_an_alert_leaves_one_compact_entry(observer, fake_hass):
    fake_hass.states.set("lock.front", "unlocked")       # not used by this decision
    await observer["run"]()
    assert observer["pushed"] == ["The back door is open."]   # nobody home: phones
    (e,) = observer["entries"]
    assert e["kind"] == "observer"
    assert e["decision"] == "alerted"
    obs = e["observation"]
    assert obs["entity_id"] == "binary_sensor.back_door"
    assert obs["event"]["type"] == "doors_windows"
    assert (obs["event"]["from"], obs["event"]["to"]) == ("off", "on")
    assert obs["event"]["at"]
    assert obs["snapshot_id"]
    assert obs["rule"] == {"origin": "rule", "rule": "entry_open"}
    assert set(obs["read"]) == {"household", "asleep", "quiet_hours"}
    assert e["evidence"]["pushed"] is True and e["evidence"]["spoken"] is False
    assert e["evidence"]["delivery"]
    assert e["evidence"]["request_ids"]
    assert "lock.front" not in str(e)


async def test_a_held_alert_is_suppressed_with_what_delivery_said(observer, fake_hass):
    fake_hass.states.set("person.a", "home")
    observer["allow"] = False
    observer["classify"] = {"worth_considering": True, "urgency": "medium",
                            "category": "security"}
    await observer["run"]("lock.back", "", "locked", "unlocked")
    assert observer["spoken"] == [] and observer["pushed"] == []
    (e,) = observer["entries"]
    assert e["decision"] == "suppressed"
    assert e["evidence"]["spoken"] is False and e["evidence"]["pushed"] is False
    assert "muted" in e["evidence"]["delivery"]


async def test_a_silent_decision_is_ignored_with_its_rule_and_reason(observer):
    observer["decide"] = {"speak": False, "reason": "door/window closed — normal operation"}
    observer["trace"] = {"origin": "rule", "rule": "entry_closed"}
    await observer["run"](old="on", new="off")
    (e,) = observer["entries"]
    assert e["decision"] == "ignored"
    assert e["reason"] == "door/window closed — normal operation"
    assert e["observation"]["rule"] == {"origin": "rule", "rule": "entry_closed"}
    assert e["observation"]["snapshot_id"]
    assert e["evidence"] == {}


async def test_a_routine_event_with_no_decision_leaves_none(observer):
    observer["classify"] = {"worth_considering": False, "urgency": "low",
                            "category": "motion"}
    await observer["run"]("binary_sensor.hall_motion", "motion")
    assert observer["entries"] == []


async def test_an_opening_while_home_leaves_none(observer, fake_hass):
    """Decided as ordinary household activity before any reasoning."""
    fake_hass.states.set("person.a", "home")
    await observer["run"]()
    assert observer["entries"] == []


async def test_a_critical_hazard_always_gets_an_entry(observer):
    """Even when the classifier passes over it."""
    observer["classify"] = {"worth_considering": False, "urgency": "low",
                            "category": "safety"}
    await observer["run"]("binary_sensor.kitchen_smoke", "smoke", "off", "on")
    (e,) = observer["entries"]
    assert e["decision"] == "ignored"
    assert e["observation"]["rule"] == {"origin": "classifier",
                                        "rule": "not_worth_considering"}
    assert e["observation"]["snapshot_id"]


async def test_an_entry_failure_never_affects_the_alert(observer, load, monkeypatch):
    def boom(**kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(load("decision_record"), "record", boom)
    await observer["run"]()
    assert observer["pushed"] == ["The back door is open."]


# ── the rule, from the decision itself ──────────────────────────────────────

def _kwargs(**over):
    kw = dict(honorific="", event_summary="Back door (binary_sensor.back_door) changed "
              "from off to on", home_state_summary="", classifier_urgency="medium",
              classifier_category="doors_windows", recent_announcements=[],
              anyone_home=False, entity_id="binary_sensor.back_door",
              device_class="door", from_state="off", to_state="on",
              friendly_name="Back door")
    kw.update(over)
    return kw


async def test_a_local_rule_names_itself(reasoning_loop, fake_hass, provider_factory,
                                         monkeypatch):
    monkeypatch.setattr(reasoning_loop, "_rich_mode", lambda hass: False)
    trace = {}
    out = await reasoning_loop.decide(fake_hass, provider_factory(), trace=trace,
                                      **_kwargs())
    plain = await reasoning_loop.decide(fake_hass, provider_factory(), **_kwargs())
    # The decision itself is unchanged (its wording varies by design).
    assert {k: v for k, v in out.items() if k != "message"} == \
        {k: v for k, v in plain.items() if k != "message"}
    assert trace == {"origin": "rule", "rule": "entry_open"}


async def test_a_provider_decision_names_itself(reasoning_loop, fake_hass, provider_factory,
                                                connectivity, load, monkeypatch):
    connectivity.reset()
    cache = load("reasoning_cache")
    monkeypatch.setattr(reasoning_loop, "_try_local_reasoning", lambda *a, **k: None)
    monkeypatch.setattr(reasoning_loop, "_rich_mode", lambda hass: False)
    monkeypatch.setattr(cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(cache, "remember", lambda *a, **k: None)
    trace = {}
    await reasoning_loop.decide(
        fake_hass, provider_factory(['{"speak": false, "reason": "routine"}']),
        trace=trace, **_kwargs())
    assert trace == {"origin": "provider", "rule": "provider_silent"}
    connectivity.reset()


# ── the snapshot id ─────────────────────────────────────────────────────────

def test_a_reading_inside_a_tick_names_the_tick(load, fake_hass):
    w, ap = load("world"), load("alert_path")
    snap = w.begin_tick(fake_hass, {})
    try:
        assert snap.id
        assert ap.situation(fake_hass, sleeping=False, quiet=False).snapshot_id == snap.id
    finally:
        w.end_tick()


def test_each_reading_outside_a_tick_has_its_own_id(load, fake_hass):
    ap = load("alert_path")
    a = ap.situation(fake_hass, sleeping=False, quiet=False)
    b = ap.situation(fake_hass, sleeping=False, quiet=False)
    assert a.snapshot_id and b.snapshot_id and a.snapshot_id != b.snapshot_id
