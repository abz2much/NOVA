"""Critical hazard sensors are decided from structured data (Phase 2B.2).

A smoke, gas, water-leak or carbon monoxide sensor is critical because of its
device_class (const.URGENCY_CEILINGS), not because of what it is called.
reasoning_loop._try_local_reasoning decides an active hazard from the
structured fields decide() already has, before recent-announcement dedup and
before the "someone is home, so security activity is normal" shortcut, and
without calling the provider. Cleared, unavailable and unknown transitions
stay silent; ordinary doors, windows and locks keep their existing
behaviour; the old name-based check remains as a fallback.

Focused run:
    python -m pytest tests/unit/test_reasoning_critical_hazard.py -q
"""
import pytest

CRITICAL = ["smoke", "gas", "moisture", "carbon_monoxide"]
WORDS = {"smoke": "smoke", "gas": "gas", "moisture": "water leak",
         "carbon_monoxide": "carbon monoxide"}


def _local(rl, device_class="smoke", to_state="on", *, from_state="off",
           urgency="critical", category="security", anyone_home=True,
           recent=None, entity_id="binary_sensor.hall_detector",
           friendly_name="Hall Detector", summary=None, **kw):
    summary = summary or (f"{friendly_name} ({entity_id}) changed from "
                          f"{from_state} to {to_state}")
    return rl._try_local_reasoning(
        summary, urgency, category, "sir", list(recent or []), anyone_home,
        from_state=from_state, to_state=to_state, entity_id=entity_id,
        friendly_name=friendly_name, device_class=device_class, **kw)


# ── 1–3, 11: CRIT-011 and the other critical classes, generic names ────────

@pytest.mark.parametrize("dc", CRITICAL)
def test_active_hazard_with_a_generic_name_speaks_critical(reasoning_loop, dc):
    out = _local(reasoning_loop, dc)
    assert out["speak"] is True and out["urgency"] == "critical"
    assert "Hall Detector" in out["message"]
    assert WORDS[dc] in out["message"]
    # nothing in the event text names the hazard
    assert WORDS[dc].split()[-1] not in "hall detector (binary_sensor.hall_detector)"


def test_crit_011_message(reasoning_loop):
    out = _local(reasoning_loop, "smoke")
    assert out["message"] == ("Sir, smoke detected by Hall Detector — "
                              "immediate attention required.")


def test_the_classes_come_from_the_canonical_ceilings(reasoning_loop, load):
    const = load("const")
    expected = {dc for dc, u in const.URGENCY_CEILINGS.items() if u == "critical"}
    assert reasoning_loop._CRITICAL_HAZARD_CLASSES == expected
    assert set(CRITICAL) <= expected


def test_entity_id_names_the_sensor_when_there_is_no_friendly_name(reasoning_loop):
    out = _local(reasoning_loop, "gas", friendly_name="",
                 entity_id="binary_sensor.utility_2",
                 summary="binary_sensor.utility_2 changed from off to on")
    assert "binary_sensor.utility_2" in out["message"]


# ── 4: the provider is never asked ─────────────────────────────────────────

@pytest.fixture
def isolated(reasoning_loop, connectivity, load, monkeypatch):
    connectivity.reset()
    cache = load("reasoning_cache")
    monkeypatch.setattr(cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(cache, "remember", lambda *a, **k: None)
    monkeypatch.setattr(cache, "note_hit", lambda *a, **k: None)
    monkeypatch.setattr(reasoning_loop, "_rich_mode", lambda hass: False)
    yield reasoning_loop
    connectivity.reset()


@pytest.mark.parametrize("dc", CRITICAL)
async def test_decide_handles_a_structured_hazard_without_the_provider(
        isolated, fake_hass, provider_factory, dc):
    provider = provider_factory(replies=['{"speak": false, "reason": "routine"}'])
    out = await isolated.decide(
        fake_hass, provider, honorific="sir",
        event_summary="Hall Detector (binary_sensor.hall_detector) changed from off to on",
        home_state_summary="", classifier_urgency="critical",
        classifier_category="security", recent_announcements=[], anyone_home=True,
        entity_id="binary_sensor.hall_detector", device_class=dc,
        from_state="off", to_state="on", friendly_name="Hall Detector")
    assert out["speak"] is True and out["urgency"] == "critical"
    assert provider.calls == 0


# ── 5: recent-announcement dedup doesn't hold back an active hazard ────────

def test_a_matching_recent_announcement_does_not_suppress_it(reasoning_loop):
    summary = "Hall Detector (binary_sensor.hall_detector) changed from off to on"
    out = _local(reasoning_loop, "smoke", recent=[summary])
    assert out["speak"] is True and out["urgency"] == "critical"


def test_the_same_announcement_still_dedups_ordinary_events(reasoning_loop):
    summary = "Hall Detector (binary_sensor.hall_detector) changed from off to on"
    out = _local(reasoning_loop, "", recent=[summary], urgency="medium",
                 category="doors_windows", anyone_home=False)
    assert out == {"speak": False, "reason": "recently announced similar event"}


# ── 6–7: active versus inactive transitions ────────────────────────────────

@pytest.mark.parametrize("state", ["on", "detected", "wet", "triggered", "unsafe"])
def test_active_states_speak(reasoning_loop, state):
    assert _local(reasoning_loop, "moisture", state)["speak"] is True


@pytest.mark.parametrize("dc", CRITICAL)
@pytest.mark.parametrize("state", ["off", "clear", "safe", "dry", "unavailable", "unknown"])
def test_clearing_or_unknown_states_are_not_a_new_alert(reasoning_loop, dc, state):
    out = _local(reasoning_loop, dc, state, from_state="on")
    assert out["speak"] is False
    assert out.get("urgency") != "critical"


def test_no_state_to_judge_leaves_the_existing_logic_in_charge(reasoning_loop):
    """Neither to_state nor a 'changed … to X' summary: the structured check
    has nothing to judge, so it doesn't decide (the old path runs)."""
    assert reasoning_loop._structured_hazard(
        "smoke", "", "Hall Detector (binary_sensor.hall_detector)",
        "binary_sensor.hall_detector", "Hall Detector", "sir") is None


# ── 8–9: ordinary security activity is unchanged ───────────────────────────

def test_door_open_while_home_is_still_normal(reasoning_loop):
    out = _local(reasoning_loop, "door", urgency="medium", category="doors_windows",
                 entity_id="binary_sensor.back_door", friendly_name="Back Door")
    assert out["speak"] is False and "user is home" in out["reason"]


def test_lock_unlocked_while_home_is_still_normal(reasoning_loop):
    out = _local(reasoning_loop, "", "unlocked", from_state="locked", urgency="medium",
                 entity_id="lock.front_door", friendly_name="Front Door")
    assert out["speak"] is False and "user is home" in out["reason"]


@pytest.mark.parametrize("dc", ["tamper", "safety", "sound", "door", "window", "garage_door"])
def test_a_non_critical_class_is_not_promoted(reasoning_loop, dc):
    """Even labelled critical and security, a non-critical class goes down the
    existing path: someone is home, so it stays silent."""
    out = _local(reasoning_loop, dc, entity_id="binary_sensor.hall_sensor",
                 friendly_name="Hall Sensor")
    assert out["speak"] is False
    assert out["reason"] == "security event but a registered user is home — normal"


# ── 10: the name-based fallback still works without a device_class ────────

@pytest.mark.parametrize("name, eid", [
    ("Kitchen Smoke", "binary_sensor.kitchen_smoke"),
    ("Cellar Leak", "binary_sensor.cellar_leak"),
])
def test_text_fallback_without_a_device_class(reasoning_loop, name, eid):
    out = _local(reasoning_loop, "", entity_id=eid, friendly_name=name)
    assert out["speak"] is True and out["urgency"] == "critical"


def test_existing_positional_callers_still_work(reasoning_loop):
    out = reasoning_loop._try_local_reasoning(
        "Kitchen Smoke (binary_sensor.kitchen_smoke) changed from off to on",
        "critical", "security", "sir", [], True, from_state="off", to_state="on")
    assert out["speak"] is True and out["urgency"] == "critical"
