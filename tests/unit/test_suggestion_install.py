"""Approving a learned suggestion (v6.52.0) installs a real Home Assistant
automation instead of only flipping a flag in the database.

The first half covers the pure normaliser for each pattern shape the analyser
produces and the service lookup. The second half covers the async installer
and how it calls automation creation.
"""
import json
import types

import pytest


@pytest.fixture
def pa(load):
    return load("pattern_analyzer")


@pytest.fixture
def ac(load):
    return load("automation_creator")


def _stored(alias, trigger, action, **more):
    return json.dumps({"alias": alias, "trigger": trigger, "action": action, **more})


def _state_trigger(entity, to):
    return {"platform": "state", "entity_id": entity, "to": to}


# ── normalize_suggestion_automation (pure) ───────────────────────────────────

def test_time_routine_is_installable_and_brought_up_to_date(pa):
    out = pa.normalize_suggestion_automation(_stored(
        "Nova Learned: light.porch on at 18:00",
        {"platform": "time", "at": "18:00:00"},
        {"service": "light.turn_on", "entity_id": "light.porch"}))
    assert out["installable"] is True
    # the old key names are replaced by the current ones
    assert out["trigger"][0]["trigger"] == "time"
    assert "platform" not in out["trigger"][0]
    assert out["action"][0]["action"] == "light.turn_on"
    assert "service" not in out["action"][0]


def test_a_sequence_keeps_its_delay_step_as_it_was(pa):
    out = pa.normalize_suggestion_automation(_stored(
        "Nova Learned: light.hall after binary_sensor.door",
        _state_trigger("binary_sensor.door", "on"),
        [{"delay": "00:01:00"},
         {"service": "light.turn_on", "entity_id": "light.hall"}]))
    assert out["installable"] is True
    assert out["action"][0] == {"delay": "00:01:00"}
    assert out["action"][1]["action"] == "light.turn_on"


def test_a_presence_pattern_is_installable(pa):
    out = pa.normalize_suggestion_automation(_stored(
        "Nova Learned: light.entry when person.username home",
        _state_trigger("person.username", "home"),
        {"service": "light.turn_on", "entity_id": "light.entry"}))
    assert out["installable"] is True
    assert out["alias"].startswith("Nova Learned")


def test_a_manual_review_note_is_advice_only(pa):
    out = pa.normalize_suggestion_automation(json.dumps(
        {"note": "Consider automating: 'goodnight' at 23:00", "type": "manual_review"}))
    assert out["installable"] is False
    assert "advisory" in out["reason"]


def test_a_bare_note_is_not_installable(pa):
    out = pa.normalize_suggestion_automation(json.dumps({"note": "some description"}))
    assert out["installable"] is False


@pytest.mark.parametrize("payload", [
    "", "not json{", "[1,2,3]", json.dumps({"alias": "x"})])
def test_a_payload_that_is_not_an_automation_is_refused(pa, payload):
    assert pa.normalize_suggestion_automation(payload)["installable"] is False


# ── service_for: the right service for each domain (v6.52.1) ─────────────────

@pytest.mark.parametrize("entity, state, service", [
    ("light.porch", "on", "light.turn_on"),
    ("switch.pump", "off", "switch.turn_off"),
    ("fan.attic", "on", "fan.turn_on"),
])
def test_on_off_domains_use_turn_on_and_turn_off(pa, entity, state, service):
    assert pa.service_for(entity, state) == {"service": service, "entity_id": entity}


def test_a_lock_uses_lock_and_unlock_never_turn_locked(pa):
    """The bug that started this: a learned door lock routine must not become
    lock.turn_locked."""
    assert pa.service_for("lock.front", "locked") == {
        "service": "lock.lock", "entity_id": "lock.front"}
    assert pa.service_for("lock.front", "unlocked") == {
        "service": "lock.unlock", "entity_id": "lock.front"}


@pytest.mark.parametrize("state, service", [
    ("open", "cover.open_cover"),
    ("closed", "cover.close_cover"),
    ("opening", "cover.open_cover"),   # a state in motion means the end state
])
def test_a_cover_opens_and_closes(pa, state, service):
    assert pa.service_for("cover.garage", state)["service"] == service


@pytest.mark.parametrize("entity, state", [
    ("climate.hall", "heat"),         # needs parameters
    ("media_player.tv", "playing"),
    ("light.x", "weird_state"),
    ("notanentity", "on"),
    ("", "on"),
])
def test_a_state_with_no_plain_service_gives_nothing(pa, entity, state):
    assert pa.service_for(entity, state) is None


def test_a_lock_sequence_ends_up_calling_a_real_lock_service(pa):
    """The generator's lock step must come out of the normaliser as lock.lock."""
    out = pa.normalize_suggestion_automation(_stored(
        "Nova Learned: lock.front after cover.garage",
        _state_trigger("cover.garage", "closed"),
        [{"delay": "00:01:00"}, pa.service_for("lock.front", "locked")]))
    assert out["installable"] is True
    step = out["action"][1]
    assert step["action"] == "lock.lock"
    assert "turn_" not in step["action"]


# ── install_approved_suggestion (async wiring) ───────────────────────────────

class _Analyzer:
    def __init__(self, suggestion):
        self.suggestion = suggestion
        self.approved = None
        self.installed = None
        self.covered = None

    def get_suggestion(self, sid):
        return self.suggestion

    def approve_suggestion(self, sid):
        self.approved = sid
        return True

    def mark_installed(self, sid, auto_id):
        self.installed = (sid, auto_id)

    def mark_covered(self, sid):
        self.covered = sid


def _row(sid, description, automation_yaml):
    return {"id": sid, "description": description, "automation_yaml": automation_yaml}


def _porch_row(sid=3, description="d", alias="a", entity="light.p"):
    return _row(sid, description, _stored(
        alias, {"platform": "time", "at": "18:00:00"},
        {"service": "light.turn_on", "entity_id": entity}))


@pytest.fixture
def wire(pa, ac, fake_hass, monkeypatch):
    """Put a suggestion in front of the installer and record how it calls
    automation creation."""
    def _wire(suggestion, reply=None):
        analyzer = _Analyzer(suggestion)
        monkeypatch.setattr(pa, "get_analyzer", lambda: analyzer)
        seen = {"calls": 0, "kwargs": {}}

        async def create(hass, **kwargs):
            seen["calls"] += 1
            seen["kwargs"] = kwargs
            return reply if reply is not None else {"success": True}

        monkeypatch.setattr(ac, "create_automation", create)
        return types.SimpleNamespace(analyzer=analyzer, seen=seen, hass=fake_hass)
    return _wire


async def test_a_concrete_suggestion_is_installed(pa, wire):
    w = wire(_porch_row(7, "learned", "porch on at 18:00", "light.porch"),
             {"success": True, "automation_id": "nova_auto_porch",
              "alias": "Nova · porch on at 18:00"})
    res = await pa.install_approved_suggestion(w.hass, 7)
    assert res["ok"] is True and res["installed"] is True
    assert res["automation_id"] == "nova_auto_porch"
    # It goes from pending straight to installed once the automation is
    # confirmed (Phase 5, D7); it is never marked approved first.
    assert w.analyzer.approved is None
    assert w.analyzer.installed == (7, "nova_auto_porch")
    assert w.seen["kwargs"]["alias"] == "porch on at 18:00"


async def test_the_restart_mode_is_passed_on(pa, wire):
    row = _row(8, "presence release", _stored(
        "office presence release", _state_trigger("binary_sensor.door", "on"),
        [{"service": "light.turn_on", "entity_id": "light.office"},
         {"service": "light.turn_off", "entity_id": "light.office"}],
        mode="restart"))
    w = wire(row, {"success": True, "automation_id": "nova_auto_release",
                   "alias": "Nova · office presence release"})
    result = await pa.install_approved_suggestion(w.hass, 8)
    assert result["installed"] is True
    assert w.seen["kwargs"]["mode"] == "restart"


async def test_an_advice_only_suggestion_is_acknowledged_but_nothing_is_written(pa, wire):
    w = wire(_row(9, "note", json.dumps({"note": "x", "type": "manual_review"})))
    res = await pa.install_approved_suggestion(w.hass, 9)
    assert res["ok"] is True and res["installed"] is False
    assert "advisory" in res["reason"]
    assert w.analyzer.approved == 9
    assert w.analyzer.installed is None
    assert w.seen["calls"] == 0


async def test_a_suggestion_that_does_not_exist_is_reported(pa, wire):
    w = wire(None)
    res = await pa.install_approved_suggestion(w.hass, 404)
    assert res["ok"] is False and "not found" in res["error"]


async def test_a_failed_write_is_reported_and_the_suggestion_stays_pending(pa, wire):
    w = wire(_porch_row(), {"success": False, "error": "disk full"})
    res = await pa.install_approved_suggestion(w.hass, 3)
    assert res["ok"] is False and res["installed"] is False
    assert "disk full" in res["reason"] and "disk full" in res["error"]
    assert w.analyzer.approved is None
    assert w.analyzer.installed is None


async def test_an_automation_added_since_is_caught_before_installing(
        pa, wire, load, monkeypatch):
    w = wire(_porch_row(14, "porch", "porch on", "light.porch"))
    existing = types.SimpleNamespace(
        entity_id="automation.porch", name="Existing porch",
        referenced_entities=("light.porch",),
        raw_config={
            "triggers": [{"trigger": "time", "at": "18:00:00"}],
            "conditions": [],
            "actions": [{"action": "light.turn_on", "entity_id": "light.porch"}],
        })
    monkeypatch.setattr(
        load("automation_inventory"), "get_inventory",
        lambda hass: types.SimpleNamespace(records=lambda: [existing]))
    result = await pa.install_approved_suggestion(w.hass, 14)
    assert result["installed"] is False
    assert "already automated" in result["reason"]
    assert w.analyzer.covered == 14
    assert w.analyzer.approved is None
    assert w.seen["calls"] == 0
