"""Read-only automation inventory: runtime-first, safe fallback, no raw leaks."""
from __future__ import annotations

import sys
import types

from fakes import FakeHass, FakeRegistryEntry


def _module(load):
    return load("automation_inventory")


class _Automation:
    def __init__(self, entity_id, unique_id, name, raw_config=None, *, blueprint=None):
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.name = name
        self.raw_config = raw_config
        self.referenced_blueprint = blueprint
        self.referenced_entities = {"light.kitchen", "sensor.outdoor"}
        self.referenced_devices = {"device-1"}
        self.referenced_areas = {"kitchen"}
        self.is_on = True


def _runtime(monkeypatch, hass, entities):
    key = object()
    mod = types.ModuleType("homeassistant.components.automation")
    mod.DATA_COMPONENT = key
    monkeypatch.setitem(sys.modules, "homeassistant.components.automation", mod)
    hass.data[key] = types.SimpleNamespace(entities=entities)


def test_runtime_inventory_covers_existing_nova_and_blueprint(load, monkeypatch):
    inv_mod = _module(load)
    hass = FakeHass()
    hass.states.set("automation.evening", "on", friendly_name="Evening",
                    last_triggered="2026-09-24T18:00:00+00:00")
    hass.states.set("automation.nova_porch", "off", friendly_name="Nova porch")
    _runtime(monkeypatch, hass, [
        _Automation("automation.evening", "evening", "Evening", {
            "id": "evening", "alias": "Evening", "triggers": [], "actions": []}),
        _Automation("automation.nova_porch", "nova_auto_porch", "Nova · Porch", {
            "id": "nova_auto_porch", "alias": "Nova · Porch",
            "use_blueprint": {"path": "motion_light.yaml"}},
            blueprint="automation/motion_light.yaml"),
    ])

    inv = inv_mod.AutomationInventory(hass)
    records = inv.refresh()

    assert [r.entity_id for r in records] == [
        "automation.evening", "automation.nova_porch"]
    assert records[0].understanding == "full"
    assert records[0].last_triggered == "2026-09-24T18:00:00+00:00"
    assert records[1].origin == "nova"
    assert records[1].understanding == "partial"
    assert records[1].enabled is False


def test_public_inventory_never_exposes_raw_config(load, monkeypatch):
    inv_mod = _module(load)
    hass = FakeHass()
    hass.states.set("automation.secret", "on")
    _runtime(monkeypatch, hass, [
        _Automation("automation.secret", "secret", "Secret", {
            "trigger": {"webhook_id": "do-not-leak"},
            "action": {"data": {"token": "also-secret"}},
        }),
    ])
    inv = inv_mod.AutomationInventory(hass)
    inv.refresh()

    public = inv.public_items()[0]
    assert "raw_config" not in public
    assert "do-not-leak" not in repr(public)
    assert "also-secret" not in repr(public)


def test_metadata_fallback_is_honest(load, monkeypatch):
    inv_mod = _module(load)
    hass = FakeHass()
    hass.states.set("automation.package_rule", "on", friendly_name="Package rule")
    # No runtime component means fallback, not an empty inventory.
    monkeypatch.delitem(sys.modules, "homeassistant.components.automation", raising=False)

    records = inv_mod.AutomationInventory(hass).refresh()

    assert len(records) == 1
    assert records[0].entity_id == "automation.package_rule"
    assert records[0].understanding == "metadata_only"


def test_reload_refreshes_and_close_unsubscribes(load, monkeypatch):
    inv_mod = _module(load)
    hass = FakeHass()
    current = [_Automation("automation.first", "first", "First", {})]
    _runtime(monkeypatch, hass, current)
    callbacks = {}
    unsubscribed = []

    def listen(event_type, callback):
        callbacks[event_type] = callback
        return lambda: unsubscribed.append(event_type)

    hass.bus.async_listen = listen
    inv = inv_mod.AutomationInventory(hass)
    inv.start()
    assert inv.get("automation.first") is not None

    current.append(_Automation("automation.second", "second", "Second", {}))
    callbacks["automation_reloaded"](types.SimpleNamespace())
    assert inv.get("automation.second") is not None

    inv.close()
    assert unsubscribed == ["automation_reloaded"]
    assert inv.records() == []


def test_context_tracker_attributes_automation_user_and_external(load):
    inv_mod = _module(load)
    inventory = types.SimpleNamespace(get=lambda entity_id: types.SimpleNamespace(
        origin="nova" if entity_id == "automation.nova" else "existing"))
    tracker = inv_mod.AutomationContextTracker(inventory, ttl=60, max_entries=64)

    tracker.record_trigger(types.SimpleNamespace(
        data={"entity_id": "automation.nova"},
        context=types.SimpleNamespace(id="ctx-nova")), now=10)
    tracker.record_trigger(types.SimpleNamespace(
        data={"entity_id": "automation.existing"},
        context=types.SimpleNamespace(id="ctx-existing")), now=10)

    nova = tracker.resolve_state(types.SimpleNamespace(context=types.SimpleNamespace(
        id="ctx-nova", parent_id=None, user_id=None)), now=11)
    existing = tracker.resolve_state(types.SimpleNamespace(context=types.SimpleNamespace(
        id="child", parent_id="ctx-existing", user_id=None)), now=11)
    user = tracker.resolve_state(types.SimpleNamespace(context=types.SimpleNamespace(
        id="user-ctx", parent_id=None, user_id="user-1")), now=11)
    external = tracker.resolve_state(types.SimpleNamespace(context=types.SimpleNamespace(
        id="external", parent_id=None, user_id=None)), now=11)

    assert (nova.kind, nova.entity_id) == ("nova_automation", "automation.nova")
    assert (existing.kind, existing.entity_id) == (
        "automation", "automation.existing")
    assert user.kind == "user"
    assert external.kind == "device_or_integration"


def test_context_tracker_is_bounded_and_expires(load):
    inv_mod = _module(load)
    tracker = inv_mod.AutomationContextTracker(ttl=60, max_entries=64)
    for idx in range(80):
        tracker.record_trigger(types.SimpleNamespace(
            data={"entity_id": f"automation.a{idx}"},
            context=types.SimpleNamespace(id=f"ctx-{idx}")), now=idx)
    assert len(tracker._contexts) <= 64
    expired = tracker.resolve_state(types.SimpleNamespace(context=types.SimpleNamespace(
        id="ctx-79", parent_id=None, user_id=None)), now=200)
    assert expired.kind == "device_or_integration"
