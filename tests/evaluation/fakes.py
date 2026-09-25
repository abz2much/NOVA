"""Synthetic Home Assistant and provider stand-ins for the evaluation harness.

Everything here is in memory. Service calls are recorded, never executed
against a real device; a "responsive" entity simply has its fake state
updated the way a working device would report back, so verification paths
can be exercised both ways.
"""
from __future__ import annotations

import types

from fakes import FakeHass

# The state a working device reports after each service.
_EFFECTS = {
    "turn_on": "on",
    "turn_off": "off",
    "lock": "locked",
    "unlock": "unlocked",
    "open_cover": "open",
    "close_cover": "closed",
}


class RegistryEntry(types.SimpleNamespace):
    """The entity registry fields the evaluated code reads."""


class EvalRegistry:
    def __init__(self):
        self.entities: dict[str, RegistryEntry] = {}

    def add(self, entity_id, *, device_id=None, entity_category=None, area_id=None,
            platform="evaluation"):
        self.entities[entity_id] = RegistryEntry(
            entity_id=entity_id, device_id=device_id, entity_category=entity_category,
            area_id=area_id, platform=platform, disabled_by=None)

    def async_get(self, entity_id):
        return self.entities.get(entity_id)

    def entries_for_device(self, device_id):
        return sorted((e for e in self.entities.values() if e.device_id == device_id),
                      key=lambda e: e.entity_id)


class _EvalServices:
    def __init__(self, hass):
        self._hass = hass
        self._registered: dict = {}

    async def async_call(self, domain, service, data=None, blocking=False, **kwargs):
        data = dict(data or {})
        self._hass.service_calls.append((domain, service, data))
        self._hass.apply_effect(domain, service, data)

    def async_services(self) -> dict:
        return self._registered

    def has_service(self, domain, service) -> bool:
        return service in self._registered.get(domain, {})


class EvalHass(FakeHass):
    """FakeHass plus an entity registry, voice satellites and a simple device
    model. Background coroutines are collected, never run, and closed by the
    sandbox; their count is part of the observation."""

    def __init__(self):
        super().__init__()
        self.registry = EvalRegistry()
        self.unresponsive: set[str] = set()
        self._services = _EvalServices(self)
        self.config = types.SimpleNamespace(
            time_zone="UTC", path=lambda *p: "/nonexistent-evaluation-config/" + "/".join(p))

    def apply_effect(self, domain, service, data):
        ids = data.get("entity_id")
        for eid in ([ids] if isinstance(ids, str) else list(ids or [])):
            st = self.states.get(eid)
            if st is None or eid in self.unresponsive:
                continue
            attrs = dict(st.attributes)
            if service == "toggle":
                new = {"on": "off", "off": "on"}.get(st.state, st.state)
            else:
                new = _EFFECTS.get(service)
            if new is None:
                continue
            if domain == "light" and service == "turn_on" and "brightness_pct" in data:
                attrs["brightness"] = round(float(data["brightness_pct"]) * 255 / 100)
            self.states.set(eid, new, last_changed=st.last_changed, **attrs)

    @property
    def pending_tasks(self) -> int:
        return len(self._tasks)


def build_hass(state: dict) -> EvalHass:
    """Build an EvalHass from a scenario's synthetic `state` block."""
    from datetime import datetime, timezone
    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    hass = EvalHass()
    for ent in state.get("entities", []):
        eid = ent["entity_id"]
        hass.states.set(eid, ent.get("state", "unknown"), last_changed=fixed,
                        **dict(ent.get("attributes", {})))
        if ent.get("responsive") is False:
            hass.unresponsive.add(eid)
        if any(k in ent for k in ("device_id", "entity_category", "area_id", "registered")):
            hass.registry.add(eid, device_id=ent.get("device_id"),
                              entity_category=ent.get("entity_category"),
                              area_id=ent.get("area_id"))
    for sat in state.get("satellites", []):
        hass.states.set(sat["entity_id"], "idle", last_changed=fixed)
        hass.registry.add(sat["entity_id"], device_id=sat["device_id"])
    return hass


class ScriptedProvider:
    """A provider whose every reply is written in the scenario. It never
    reaches a network: `chat` returns the next scripted reply or raises the
    scripted error."""

    def __init__(self, replies=None, error: str | None = None):
        self.replies = list(replies or [])
        self.error = error
        self.calls = 0

    def chat(self, messages, temperature=0.4, max_tokens=200, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise ConnectionError(self.error)
        text = self.replies.pop(0) if self.replies else '{"speak": false, "reason": "no scripted reply"}'
        return {"text": text, "tool_calls": [], "raw": None}
