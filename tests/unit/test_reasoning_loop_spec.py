"""The observer's reasoning loop, checked against its spec (7d).

The module is a thin front for cognitive.coordinator: it builds the event
snapshot, voices hazard decisions, carries the local rule into the trace,
holds the observer prompt and reads the Rich Reasoning switch. Its names are
monkeypatch points for other tests, so they stay.
"""
import hashlib
import types

import pytest


@pytest.fixture
def rl(load):
    return load("reasoning_loop")


def test_the_observer_prompt_is_unchanged(rl):
    text = rl.REASONING_SYSTEM_APPENDIX
    assert len(text) == 2782
    assert hashlib.sha256(text.encode()).hexdigest() == (
        "e0a8b8b81a3228128aefaf180b51399a1607d4974bd0228e9e23872687af969d")
    assert text.startswith("\nYou are currently operating in OBSERVER MODE")
    assert "{honorific}" in text


def test_the_compatibility_names_point_at_the_evaluators(rl, load):
    ev = load("cognitive.evaluators")
    assert rl._ACTIVE_TRIGGER_STATES == set(ev.ACTIVE_TRIGGER_STATES)
    assert isinstance(rl._ACTIVE_TRIGGER_STATES, set)
    assert rl._CRITICAL_HAZARD_CLASSES is ev.CRITICAL_HAZARD_CLASSES
    assert rl._HAZARD_WORDS == dict(ev.HAZARD_WORDS)
    assert rl._HAZARD_WORDS is not ev.HAZARD_WORDS
    assert rl._summary_new_state is ev.summary_new_state
    assert rl._parse_summary is ev.parse_summary


def test_a_snapshot_carries_every_field(rl):
    snap = rl._snapshot(event_summary="s", urgency="high", category="security",
                        recent_announcements=["a"], anyone_home=1, from_state="off",
                        to_state="on", entity_id="binary_sensor.door",
                        friendly_name="Door", device_class="door")
    assert (snap.entity_id, snap.domain, snap.device_class) == (
        "binary_sensor.door", "binary_sensor", "door")
    assert (snap.from_state, snap.to_state, snap.friendly_name) == ("off", "on", "Door")
    assert (snap.event_summary, snap.category, snap.urgency) == ("s", "security", "high")
    assert snap.anyone_home is True and snap.recent_announcements == ("a",)


def test_a_structured_hazard_is_voiced(rl):
    out = rl._structured_hazard("smoke", "on", "Smoke detected", "binary_sensor.hall",
                                "Hall", "sir")
    assert out["speak"] is True and out["urgency"] == "critical"
    assert "{honorific}" not in out["message"]


def test_a_non_hazard_has_no_structured_decision(rl):
    assert rl._structured_hazard("door", "on", "", "binary_sensor.door", "Door", "sir") is None


def test_local_reasoning_records_its_rule_in_the_trace(rl):
    trace: dict = {}
    out = rl._try_local_reasoning("Smoke detected", "critical", "safety", "sir", [],
                                  to_state="on", entity_id="binary_sensor.hall",
                                  friendly_name="Hall", device_class="smoke", trace=trace)
    assert out["speak"] is True
    assert trace["origin"] and trace["rule"]


def test_local_reasoning_without_a_rule_leaves_the_trace_alone(rl, monkeypatch):
    monkeypatch.setattr(rl._coordinator, "local_decision", lambda snap, h: None)
    trace: dict = {}
    assert rl._try_local_reasoning("x", "low", "", "sir", [], trace=trace) is None
    assert trace == {}


def test_local_reasoning_hands_the_coordinator_a_snapshot(rl, monkeypatch):
    seen = []

    def _local(snap, honorific):
        seen.append((snap, honorific))
        return types.SimpleNamespace(origin="o", reason_code="r",
                                     as_dict=lambda: {"speak": False})
    monkeypatch.setattr(rl._coordinator, "local_decision", _local)
    assert rl._try_local_reasoning("e", "u", "c", "madam", ["x"], anyone_home=True,
                                   from_state="a", to_state="b", entity_id="lock.front",
                                   friendly_name="F", device_class="lock") == {"speak": False}
    snap, honorific = seen[0]
    assert honorific == "madam" and snap.domain == "lock" and snap.anyone_home is True
    assert snap.recent_announcements == ("x",)


def test_compose_passes_through_to_the_presentation(rl, monkeypatch):
    calls = []
    monkeypatch.setattr(rl._presentation, "_lm_compose",
                        lambda *a, **k: calls.append((a, k)) or "said")
    assert rl._lm_compose("sir", "Hall", device_class="smoke", to_state="on",
                          away=True, escalated=True) == "said"
    assert calls == [(("sir", "Hall"), {"device_class": "smoke", "to_state": "on",
                                        "away": True, "escalated": True})]


@pytest.mark.parametrize("value, expected", [
    (None, False), (True, True), (False, False), ("on", True), ("TRUE", True),
    ("1", True), ("yes", True), ("off", False), ("", False), (1, True), (0, False),
])
def test_rich_reasoning_switch(rl, load, monkeypatch, value, expected):
    runtime = load("runtime")
    rc = {} if value is None else {"rich_reasoning": value}
    monkeypatch.setattr(runtime, "domain_runtime_config", lambda hass: rc)
    assert rl._rich_mode(object()) is expected


async def test_decide_hands_the_coordinator_its_hooks_and_every_field(rl, monkeypatch):
    seen = {}

    async def _decide(hass, provider, **kw):
        seen.update(kw, hass=hass, provider=provider)
        return {"speak": False}
    monkeypatch.setattr(rl._coordinator, "decide", _decide)
    marker = object()
    out = await rl.decide("H", "P", honorific="sir", event_summary="e",
                          home_state_summary="h", classifier_urgency="u",
                          classifier_category="c", recent_announcements=["r"],
                          presence_context="p", anyone_home=True, entity_id="x.y",
                          device_class="d", from_state="f", to_state="t",
                          friendly_name="n", trace=marker)
    assert out == {"speak": False}
    hooks = seen.pop("hooks")
    assert hooks.local is rl._try_local_reasoning
    assert hooks.rich_mode is rl._rich_mode
    assert hooks.build_system_prompt is rl.build_system_prompt
    assert hooks.system_appendix is rl.REASONING_SYSTEM_APPENDIX
    assert seen == {"hass": "H", "provider": "P", "honorific": "sir", "event_summary": "e",
                    "home_state_summary": "h", "classifier_urgency": "u",
                    "classifier_category": "c", "recent_announcements": ["r"],
                    "presence_context": "p", "anyone_home": True, "entity_id": "x.y",
                    "device_class": "d", "from_state": "f", "to_state": "t",
                    "friendly_name": "n", "trace": marker}


async def test_decide_uses_a_patched_local_rule(rl, monkeypatch):
    seen = {}

    async def _decide(hass, provider, **kw):
        seen["local"] = kw["hooks"].local
        return {}
    monkeypatch.setattr(rl._coordinator, "decide", _decide)

    def _patched(*a, **k):
        return None
    monkeypatch.setattr(rl, "_try_local_reasoning", _patched)
    await rl.decide(None, None, honorific="", event_summary="", home_state_summary="",
                    classifier_urgency="", classifier_category="", recent_announcements=[])
    assert seen["local"] is _patched
