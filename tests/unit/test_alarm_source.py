"""Security alarm selection must be explicit, public and fail safe."""
from types import SimpleNamespace


def _registry(monkeypatch, entries):
    import sys

    registry = SimpleNamespace(
        entities={
            entity_id: SimpleNamespace(entity_id=entity_id, platform=platform)
            for entity_id, platform in entries.items()
        },
        async_get=lambda entity_id: None,
    )
    er = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(er, "async_get", lambda hass: registry)


def test_explicit_alarm_source_wins(load, fake_hass, monkeypatch):
    alarm_source = load("alarm_source")
    _registry(monkeypatch, {
        "alarm_control_panel.alarmo": "alarmo",
        "alarm_control_panel.ring": "ring_mqtt",
    })
    fake_hass.states.set("alarm_control_panel.alarmo", "disarmed")
    fake_hass.states.set("alarm_control_panel.ring", "armed_home")

    assert alarm_source.entity_ids(
        fake_hass, {"security_alarm_entity": "alarm_control_panel.ring"}
    ) == ("alarm_control_panel.ring",)


def test_single_alarmo_source_is_discovered(load, fake_hass, monkeypatch):
    alarm_source = load("alarm_source")
    _registry(monkeypatch, {
        "alarm_control_panel.alarmo": "alarmo",
        "alarm_control_panel.homebase": "eufy_security",
    })
    fake_hass.states.set("alarm_control_panel.alarmo", "disarmed")
    fake_hass.states.set("alarm_control_panel.homebase", "armed_home")

    assert alarm_source.entity_ids(fake_hass, {}) == (
        "alarm_control_panel.alarmo",
    )
    assert alarm_source.is_selected(
        fake_hass, "alarm_control_panel.homebase", {}
    ) is False


def test_runtime_clear_reenables_alarmo_auto_detection(
        load, fake_hass, monkeypatch):
    alarm_source = load("alarm_source")
    _registry(monkeypatch, {
        "alarm_control_panel.alarmo": "alarmo",
        "alarm_control_panel.ring": "ring_mqtt",
    })
    fake_hass.states.set("alarm_control_panel.alarmo", "disarmed")
    fake_hass.states.set("alarm_control_panel.ring", "armed_home")
    fake_hass.data["nova"] = {
        "entry": {"runtime_config": {"security_alarm_entity": ""}},
    }

    assert alarm_source.entity_ids(
        fake_hass, {"security_alarm_entity": "alarm_control_panel.ring"}
    ) == ("alarm_control_panel.alarmo",)


def test_multiple_alarmo_sources_require_user_choice(load, fake_hass, monkeypatch):
    alarm_source = load("alarm_source")
    _registry(monkeypatch, {
        "alarm_control_panel.upstairs": "alarmo",
        "alarm_control_panel.downstairs": "alarmo",
    })
    fake_hass.states.set("alarm_control_panel.upstairs", "armed_home")
    fake_hass.states.set("alarm_control_panel.downstairs", "disarmed")

    assert alarm_source.entity_ids(fake_hass, {}) == ()
    assert alarm_source.states(fake_hass, {}) == []


def test_staggered_multiple_alarmo_startup_remains_ambiguous(
        load, fake_hass, monkeypatch):
    """A second registered partition may not have published state yet."""
    alarm_source = load("alarm_source")
    _registry(monkeypatch, {
        "alarm_control_panel.upstairs": "alarmo",
        "alarm_control_panel.downstairs": "alarmo",
    })
    fake_hass.states.set("alarm_control_panel.upstairs", "armed_home")

    assert alarm_source.entity_ids(fake_hass, {}) == ()
    assert alarm_source.states(fake_hass, {}) == []


def test_no_alarmo_source_fails_quietly(load, fake_hass, monkeypatch):
    alarm_source = load("alarm_source")
    _registry(monkeypatch, {
        "alarm_control_panel.homebase": "eufy_security",
        "alarm_control_panel.ring": "ring_mqtt",
    })
    fake_hass.states.set("alarm_control_panel.homebase", "armed_home")
    fake_hass.states.set("alarm_control_panel.ring", "armed_away")

    assert alarm_source.entity_ids(fake_hass, {}) == ()


def test_observer_drops_unselected_alarm_panel(load, fake_hass, monkeypatch):
    observer = load("observer")
    _registry(monkeypatch, {
        "alarm_control_panel.alarmo": "alarmo",
        "alarm_control_panel.homebase": "eufy_security",
    })
    observer._STATE.hass = fake_hass
    observer._STATE.config = {}
    old = SimpleNamespace(state="disarmed", attributes={})
    new = SimpleNamespace(state="armed_home", attributes={})
    event = SimpleNamespace(data={
        "entity_id": "alarm_control_panel.homebase",
        "old_state": old,
        "new_state": new,
    })

    assert observer._should_pre_filter(event) is True


def test_security_consumers_ignore_unselected_armed_panel(
        load, fake_hass, monkeypatch):
    _registry(monkeypatch, {
        "alarm_control_panel.alarmo": "alarmo",
        "alarm_control_panel.homebase": "eufy_security",
    })
    fake_hass.states.set("alarm_control_panel.alarmo", "disarmed")
    fake_hass.states.set("alarm_control_panel.homebase", "armed_away")

    core = load("cognitive_core")
    safety = core.SafetyManager(fake_hass, {})
    lockdown = core.LockdownManager(fake_hass, {})

    assert safety._alarm_armed() is False
    assert safety._residents_away() is False
    assert lockdown._alarm_armed() is False
    assert core._alarm_state_view(fake_hass) == (False, True, False)


def test_home_summary_lists_only_selected_alarm(load, fake_hass, monkeypatch):
    _registry(monkeypatch, {
        "alarm_control_panel.alarmo": "alarmo",
        "alarm_control_panel.homebase": "eufy_security",
    })
    fake_hass.states.set("alarm_control_panel.alarmo", "disarmed")
    fake_hass.states.set("alarm_control_panel.homebase", "armed_home")

    summary = load("home_state")._build_summary(fake_hass)

    assert summary.count("Alarm:") == 1
    assert "Alarm: disarmed" in summary
    assert "Alarm: armed_home" not in summary
