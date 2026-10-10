"""Stage F (8.24.0): Nova learns only from outcomes a person confirmed.

* The interruption budget and the outcome rate (adaptive awareness and
  suggestion selectivity read it) count only confirmed sources: a phone
  rating, a call-off, a panel label, a suggestion accepted or dismissed.
* Learned damping counts only labels a person gave.
* A panel label also sets the decision's outcome.
* A call-off labels the intrusion's first alert "false", so call-offs teach
  learned damping.

All state is fake; the decision log and the intrusion log are temporary.
"""
import pytest

from cognitive_safety_kit import _isolated_core, cc, clock  # noqa: F401

ALARM = "alarm_control_panel.home_security"


@pytest.fixture
def dr(load, tmp_path, monkeypatch):
    d = load("decision_record")
    monkeypatch.setattr(d, "_DEFAULT_DB", str(tmp_path / "decisions.db"))
    return d


@pytest.fixture
def intr(load, tmp_path, monkeypatch, dr):
    i = load("intrusion")
    monkeypatch.setattr(i, "_log_path", lambda: tmp_path / "intrusion_log.json")
    monkeypatch.setattr(i, "_log", [])
    monkeypatch.setattr(i, "_log_loaded", True)
    monkeypatch.setattr(i, "_called_off_until", 0.0)
    monkeypatch.setattr(i, "_last_decision_id", None)
    return i


def _judged(dr, kind, verdicts, source):
    for v in verdicts:
        rid = dr.record(kind)
        assert dr.set_outcome(rid, v, source) is True


# ── only confirmed outcomes are learned from ────────────────────────────────

@pytest.mark.parametrize("source", ["auto", "inferred", "", "test"])
def test_unlisted_sources_are_ignored(dr, source):
    _judged(dr, "anticipation", ["unnecessary"] * 6, source)
    assert dr.interruption_budget()["judged"] == 0
    assert dr.interruption_budget()["multiplier"] == 1.0
    assert dr.outcome_rate("anticipation", prefix=True)["judged"] == 0


@pytest.mark.parametrize("source", sorted(
    ["phone", "dismiss_intrusion", "panel_label", "installed", "dismiss_suggestion"]))
def test_each_confirmed_source_counts(dr, source):
    _judged(dr, "anticipation", ["unnecessary", "good"], source)
    assert dr.interruption_budget()["judged"] == 2
    assert dr.outcome_rate("anticipation")["judged"] == 2


def test_the_allowlist_is_exactly_the_five(dr):
    assert dr.CONFIRMED_SOURCES == {"phone", "dismiss_intrusion", "panel_label",
                                    "installed", "dismiss_suggestion"}


def test_learned_damping_ignores_labels_nobody_gave(intr):
    for _ in range(4):
        ev = intr.record_event("investigating", breach_area="hall")
        ev["label"], ev["label_source"] = "false", "auto"
    assert intr.pattern_verdict("hall", None)["damp"] is False
    for ev in intr._log:
        ev["label_source"] = "panel_label"
    assert intr.pattern_verdict("hall", None)["damp"] is True


def test_labels_saved_before_8_24_still_count_as_panel_labels(intr):
    for _ in range(3):
        ev = intr.record_event("investigating", breach_area="hall")
        ev["label"] = "false"                 # no label_source: an old panel label
    assert intr.pattern_verdict("hall", None)["damp"] is True


# ── a panel label sets the decision outcome ─────────────────────────────────

@pytest.mark.parametrize("label,outcome", [("false", "wrong"), ("real", "good")])
def test_a_panel_label_sets_the_decision_outcome(intr, dr, label, outcome):
    rid = dr.record("intrusion")
    ev = intr.record_event("investigating", breach_area="hall", decision_id=rid)
    assert intr.label_event(ev["id"], label)["ok"]
    rec = dr.get(rid)
    assert rec["outcome"] == outcome and rec["outcome_source"] == "panel_label"


def test_a_label_never_overrides_a_verdict_already_given(intr, dr):
    rid = dr.record("intrusion")
    dr.set_outcome(rid, "wrong", "dismiss_intrusion")
    ev = intr.record_event("investigating", breach_area="hall", decision_id=rid)
    intr.label_event(ev["id"], "real")
    assert dr.get(rid)["outcome"] == "wrong"


# ── a call-off teaches learned damping ──────────────────────────────────────

async def _first_alert(cc, fake_hass):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "security_alarm_entity": ALARM})
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set(ALARM, "armed_away")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    actions = await safety.tick(sleeping=False, anyone_home=False)
    fake_hass.close_pending()
    return [a["type"] for a in actions]


async def test_a_call_off_labels_the_first_alert_false(cc, intr, dr, fake_hass, clock):
    assert await _first_alert(cc, fake_hass) == ["intrusion_investigating"]
    (ev,) = [e for e in intr._log if e["kind"] == "investigating"]
    assert ev["decision_id"] is not None
    intr.dismiss_intrusion("it was me")
    assert ev["label"] == "false" and ev["label_source"] == "dismiss_intrusion"
    rec = dr.get(ev["decision_id"])
    assert rec["outcome"] == "wrong" and rec["outcome_source"] == "dismiss_intrusion"


async def test_call_offs_feed_learned_damping(cc, intr, fake_hass, clock):
    import time as _t
    # 13:30 local time, so every alert falls in the same three hour band.
    clock["now"] = _t.mktime((2026, 10, 10, 13, 30, 0, 0, 0, -1))
    for _ in range(intr._LEARN_MIN_FALSE):
        await _first_alert(cc, fake_hass)
        intr.dismiss_intrusion()
        intr._called_off_until = 0.0
        clock["now"] += 600
    pattern = intr._log[-1]["pattern"]
    false_n = sum(1 for e in intr._log if e.get("pattern") == pattern and e.get("label") == "false")
    assert false_n == intr._LEARN_MIN_FALSE
    area = intr._log[-1]["breach_area"] or None
    assert intr.pattern_verdict(area, None)["damp"] is True


async def test_a_call_off_never_overwrites_a_real_label(cc, intr, fake_hass, clock):
    await _first_alert(cc, fake_hass)
    (ev,) = intr._log
    intr.label_event(ev["id"], "real")
    intr.dismiss_intrusion()
    assert ev["label"] == "real" and ev["label_source"] == "panel_label"


def test_a_call_off_with_no_recent_alert_labels_nothing(intr):
    old = intr.record_event("investigating", breach_area="hall")
    old["ts"] -= 3 * 3600                     # three hours ago
    intr.dismiss_intrusion()
    assert old["label"] is None
