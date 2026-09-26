"""Typed cognitive models, pure evaluators and arbitration (Phase 8).

Focused run:
    python -m pytest tests/unit/test_cognitive_models.py -q
"""
import copy
import dataclasses
import itertools

import pytest


@pytest.fixture
def m(load):
    return load("cognitive.models")


@pytest.fixture
def ev(load):
    return load("cognitive.evaluators")


@pytest.fixture
def arb(load):
    return load("cognitive.arbitration")


def _snap(m, **kw):
    base = dict(entity_id="binary_sensor.cellar_window", device_class="window",
                from_state="off", to_state="on", friendly_name="Cellar Window",
                event_summary="Cellar Window (binary_sensor.cellar_window) changed from off to on",
                category="doors_windows", urgency="medium", anyone_home=False)
    base.update(kw)
    return m.EventSnapshot.build(**base)


# ── Models and invariants ───────────────────────────────────────────────────

def test_models_are_immutable(m):
    s = _snap(m)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.urgency = "critical"
    d = m.silent("x", "why")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.action = m.ACTION_SPEAK
    e = m.Evidence("state_machine", "hazard")
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.trust = "reported"


def test_snapshot_copies_recent_announcements_into_a_tuple(m):
    recent = ["one"]
    s = _snap(m, recent_announcements=recent)
    recent.append("two")
    assert s.recent_announcements == ("one",)
    assert s.domain == "binary_sensor"


def test_decision_rejects_unknown_actions(m):
    with pytest.raises(ValueError):
        m.Decision("shout", "x")


def test_only_a_validated_provider_decision_may_be_cacheable(m):
    with pytest.raises(ValueError):
        m.Decision(m.ACTION_SILENT, "x", origin=m.ORIGIN_RULE, cacheable=True)
    with pytest.raises(ValueError):
        m.Decision(m.ACTION_SILENT, "x", origin=m.ORIGIN_PROVIDER, cacheable=True,
                   validated=False)
    ok = m.Decision(m.ACTION_SILENT, "x", origin=m.ORIGIN_PROVIDER, cacheable=True)
    assert ok.cacheable


def test_confidence_is_optional_and_bounded(m):
    assert m.Evidence("history", "routine").confidence is None
    assert m.Evidence("attribution", "provenance", confidence=0.5).confidence == 0.5
    with pytest.raises(ValueError):
        m.Evidence("attribution", "provenance", confidence=1.5)
    with pytest.raises(ValueError):
        m.Evidence("history", "routine", age_s=-1)


def test_provider_failure_carries_no_reply_content(m):
    f = m.ProviderFailure(m.FAIL_UNREADABLE, "reply is not JSON", 26)
    assert {x.name for x in dataclasses.fields(f)} == {"kind", "detail", "reply_length"}
    with pytest.raises(ValueError):
        m.ProviderFailure("oops")


def test_as_dict_reproduces_the_legacy_shapes(m):
    assert m.silent("x", "why").as_dict() == {"speak": False, "reason": "why"}
    assert m.silent("x", "why", urgency="low").as_dict() == {
        "speak": False, "urgency": "low", "reason": "why"}
    d = m.speak("x", "high", m.Phrase.of("lead_in", sentence="hi")).with_message("Sir, hi")
    assert d.as_dict() == {"speak": True, "message": "Sir, hi", "urgency": "high"}


# ── Evaluators ──────────────────────────────────────────────────────────────

GRID = list(itertools.product(
    ["smoke", "moisture", "door", "window", "", "motion"],
    ["off", "on", "unavailable"], ["on", "off", "unavailable", "detected"],
    ["security", "doors_windows", "presence", "general", "climate"],
    ["low", "medium", "high", "critical"], [True, False]))


def test_evaluators_are_deterministic_and_leave_the_snapshot_unchanged(m, ev):
    for dc, frm, to, cat, urg, home in GRID:
        s = _snap(m, device_class=dc, from_state=frm, to_state=to, category=cat,
                  urgency=urg, anyone_home=home,
                  event_summary=f"Hall Sensor (binary_sensor.hall) changed from {frm} to {to}")
        before = copy.deepcopy(s)
        first = ev.evaluate_local_rules(s)
        again = ev.evaluate_local_rules(s)
        assert first == again
        assert s == before


def test_every_rule_returns_a_typed_decision_or_none(m, ev):
    s = _snap(m)
    for name, rule in ev.LOCAL_RULES:
        out = rule(s)
        assert out is None or isinstance(out, m.Decision), name


def test_the_priority_order_is_explicit(ev):
    names = [n for n, _ in ev.LOCAL_RULES]
    assert names[:3] == ["critical_hazard", "named_hazard", "repetition"]
    assert names.index("alarm") < names.index("presence") < names.index("entry_point")
    assert names[-1] == "low_urgency"
    assert len(set(names)) == len(names)


@pytest.mark.parametrize("dc", ["smoke", "gas", "moisture", "carbon_monoxide"])
@pytest.mark.parametrize("home", [True, False])
def test_critical_hazard_beats_occupancy_and_dedup(m, ev, arb, dc, home):
    s = _snap(m, device_class=dc, category="security", urgency="critical", anyone_home=home,
              entity_id="binary_sensor.hall_detector", friendly_name="Hall Detector",
              event_summary="Hall Detector (binary_sensor.hall_detector) changed from off to on",
              recent_announcements=["Hall Detector (binary_sensor.hall_detector) changed"])
    won = arb.arbitrate(ev.evaluate_local_rules(s))
    assert won.speak and won.urgency == "critical" and won.evaluator == "critical_hazard"
    assert won.evidence[0].provenance == m.PROVENANCE_DEVICE_CLASS


def test_an_all_clear_is_not_overridden_by_a_broader_critical_rule(m, ev, arb):
    s = _snap(m, device_class="smoke", from_state="on", to_state="off", category="security",
              urgency="critical", anyone_home=False,
              event_summary="Hall Detector (binary_sensor.hall_detector) changed from on to off")
    won = arb.arbitrate(ev.evaluate_local_rules(s))
    assert won.speak is False and won.reason_code == m.R_HAZARD_INACTIVE


def test_a_triggered_alarm_is_not_deduplicated_away(m, ev, arb):
    summary = "Home Alarm (alarm_control_panel.home) changed from armed_away to triggered"
    s = _snap(m, entity_id="alarm_control_panel.home", device_class="", category="security",
              urgency="critical", anyone_home=True, event_summary=summary,
              from_state="armed_away", to_state="triggered", recent_announcements=[summary])
    rules = ev.evaluate_local_rules(s)
    assert rules[0].reason_code == m.R_RECENTLY_ANNOUNCED
    won = arb.arbitrate(rules)
    assert won.speak and won.urgency == "critical" and won.evaluator == "alarm"


def test_repetition_still_silences_non_critical_events(m, ev, arb):
    s = _snap(m, recent_announcements=["Cellar Window (binary_sensor.cellar_window) chan"])
    won = arb.arbitrate(ev.evaluate_local_rules(s))
    assert won.reason_code == m.R_RECENTLY_ANNOUNCED and not won.speak


def test_arbitration_ties_follow_declaration_order_not_hashing(m, arb):
    a = m.silent("a", "a", evaluator="one")
    b = m.silent("b", "b", evaluator="two")
    assert arb.arbitrate([a, b]) is a
    assert arb.arbitrate([None, b, a]) is b
    assert arb.arbitrate([]) is None


def test_critical_hazard_activation(ev):
    assert ev.is_critical_hazard_activation("smoke", "off", "on")
    assert ev.is_critical_hazard_activation("moisture", "unavailable", "wet")
    assert not ev.is_critical_hazard_activation("smoke", "on", "on")
    assert not ev.is_critical_hazard_activation("smoke", "on", "off")
    assert not ev.is_critical_hazard_activation("door", "off", "on")
    assert not ev.is_critical_hazard_activation("", "off", "on")


def test_delivery_rules(ev):
    assert ev.cap_reasoned_urgency("critical", "medium") == "high"
    assert ev.cap_reasoned_urgency("critical", "critical") == "critical"
    assert ev.cap_reasoned_urgency("medium", "critical") == "medium"
    assert ev.held_for_sleep("high", True) is True
    assert ev.held_for_sleep("critical", True) is False
    assert ev.held_for_sleep("medium", False) is False


def test_local_mind_evaluator_is_pure(m, ev):
    s = _snap(m, category="general")
    kw = dict(history={"grade": "unknown"}, prior=(0, 0), flapping=False, hour=14)
    first = ev.evaluate_local_mind(s, **kw)
    assert first == ev.evaluate_local_mind(s, **kw)
    assert first.speak and first.urgency == "high"          # entry point while away
    flapping = ev.evaluate_local_mind(s, **{**kw, "flapping": True})
    assert not flapping.speak and "flapping" in flapping.reason
    crit = ev.evaluate_local_mind(_snap(m, urgency="critical"), **{**kw, "flapping": True})
    assert crit.speak and crit.urgency == "critical"


def test_the_presentation_uses_the_friendly_name(load, m):
    pres = load("cognitive.presentation")
    assert pres.entity_label("Kitchen Light", "light.kitchen") == "Kitchen Light"
    assert pres.entity_label("", "light.kitchen") == "light.kitchen"
    assert pres.entity_label("Lamp", "light.a", area="Office", collides=True) == "Lamp (Office)"
    assert pres.entity_label("Lamp", "light.a", collides=True) == "Lamp (light.a)"
