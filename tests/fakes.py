"""Hand-rolled fakes for unit-testing Nova modules without a Home Assistant
runtime. These implement only the narrow surface the modules under test actually
call — keeping them fast, deterministic, and easy to keep faithful.

Pure Python; no Home Assistant import required.
"""
from __future__ import annotations

import types
from datetime import datetime, timezone


class FakeState:
    """Mirrors the handful of attributes the cores read off a hass state.

    last_changed/last_updated default to "now" (like real HA setting a
    state for the first time) so existing tests that never pass them keep
    working unchanged; a test that cares about staleness passes an explicit
    value (see host_health tests)."""
    __slots__ = ("entity_id", "state", "attributes", "last_changed", "last_updated")

    def __init__(self, entity_id: str, state, attributes: dict | None = None,
                 last_changed=None, last_updated=None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}
        now = datetime.now(timezone.utc)
        self.last_changed = last_changed or now
        self.last_updated = last_updated or last_changed or now

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<FakeState {self.entity_id}={self.state!r} {self.attributes}>"


class FakeStates:
    """Implements the slice of hass.states the cores use: set/get/remove and
    async_all(domain) with the same domain-prefix semantics as Home Assistant."""

    def __init__(self):
        self._d: dict[str, FakeState] = {}

    def set(self, entity_id: str, state, last_changed=None, last_updated=None,
            **attributes) -> FakeState:
        st = FakeState(entity_id, state, attributes,
                       last_changed=last_changed, last_updated=last_updated)
        self._d[entity_id] = st
        return st

    def async_set(self, entity_id: str, state, attributes: dict | None = None,
                  last_changed=None, last_updated=None) -> FakeState:
        """Same shape as real HA's hass.states.async_set(entity_id, state,
        attributes_dict) — a dict, not **kwargs, so callers that build the
        attributes dict themselves (camera_semantic.py) work unchanged."""
        st = FakeState(entity_id, state, dict(attributes or {}),
                       last_changed=last_changed, last_updated=last_updated)
        self._d[entity_id] = st
        return st

    def remove(self, entity_id: str) -> None:
        self._d.pop(entity_id, None)

    def get(self, entity_id: str):
        return self._d.get(entity_id)

    def async_all(self, domain: str | None = None):
        values = list(self._d.values())
        if domain is None:
            return values
        prefix = domain + "."
        return [s for s in values if s.entity_id.startswith(prefix)]


class _Services:
    def __init__(self, sink: list):
        self._sink = sink
        self._registered: dict = {}   # domain -> {service_name: None}

    async def async_call(self, domain, service, data=None, blocking=False, **kwargs):
        # Record the intent instead of executing it, so tests can assert
        # "Nova tried to lock the door" without touching real devices.
        self._sink.append((domain, service, dict(data or {})))

    def register(self, domain: str, service: str) -> None:
        """Test helper: register a service so async_services() lists it."""
        self._registered.setdefault(domain, {})[service] = None

    def async_services(self) -> dict:
        return self._registered

    def has_service(self, domain: str, service: str) -> bool:
        return service in self._registered.get(domain, {})


class FakeRegistryEntry:
    """Mirrors the handful of homeassistant.helpers.entity_registry.RegistryEntry
    fields host_health.py (and anything else doing integration-ownership-based
    discovery) actually reads."""
    __slots__ = ("entity_id", "platform", "unique_id", "translation_key",
                "disabled_by", "device_id", "area_id",
                "original_unit_of_measurement", "original_state_class")

    def __init__(self, entity_id: str, platform: str, unique_id: str = "",
                translation_key: str | None = None, disabled_by=None,
                device_id: str | None = None, area_id: str | None = None,
                original_unit_of_measurement: str | None = None,
                original_state_class: str | None = None):
        self.entity_id = entity_id
        self.platform = platform
        self.unique_id = unique_id
        self.translation_key = translation_key
        self.disabled_by = disabled_by
        self.device_id = device_id
        self.area_id = area_id
        self.original_unit_of_measurement = original_unit_of_measurement
        self.original_state_class = original_state_class


class FakeEntityRegistry:
    """Mirrors the slice of homeassistant.helpers.entity_registry.EntityRegistry
    used for platform-ownership discovery: entities (dict-like, .values()
    iterable) and async_get(entity_id)."""

    def __init__(self):
        self.entities: dict[str, FakeRegistryEntry] = {}

    def add(self, entry: FakeRegistryEntry) -> FakeRegistryEntry:
        self.entities[entry.entity_id] = entry
        return entry

    def async_get(self, entity_id: str):
        return self.entities.get(entity_id)


class _Bus:
    def __init__(self):
        self.fired: list = []   # (event_type, data) recorded for assertions

    def async_listen(self, *args, **kwargs):
        return lambda: None  # returns an unsubscribe callable, like HA

    def async_fire(self, event_type, event_data=None, **kwargs):
        self.fired.append((event_type, dict(event_data or {})))


class FakeHass:
    """A fake Home Assistant core exposing only the surfaces enumerated from
    cognitive_core.py and reasoning_loop.py:

        states.async_all / states.get / states.set(test helper)
        services.async_call           (recorded into .service_calls)
        async_add_executor_job        (runs the callable synchronously)
        async_create_task / async_create_background_task  (collected; drain())
        bus.async_listen
        data                          (plain dict)
    """

    def __init__(self):
        self.states = FakeStates()
        self.data: dict = {}
        self.service_calls: list = []
        self._tasks: list = []
        self.bus = _Bus()
        self.config = types.SimpleNamespace(time_zone="America/New_York")
        self._services = _Services(self.service_calls)

    @property
    def services(self):
        return self._services

    async def async_add_executor_job(self, func, *args):
        # The caller awaits this; running synchronously is deterministic and
        # avoids a real thread pool.
        return func(*args)

    def async_create_task(self, coro, name=None):
        self._tasks.append(coro)
        return coro

    def async_create_background_task(self, coro, name=None):
        self._tasks.append(coro)
        return coro

    async def drain(self):
        """Run any coroutines that were spawned via create_task, so tests can
        exercise (or simply close) the announcement side-effects."""
        pending, self._tasks = self._tasks, []
        for coro in pending:
            await coro

    def close_pending(self):
        """Close collected coroutines without running them (avoids
        'coroutine was never awaited' warnings when the effect is irrelevant)."""
        for coro in self._tasks:
            coro.close()
        self._tasks = []


class FakeProvider:
    """The single network seam for reasoning_loop.decide().

    provider.chat is synchronous and returns {"text": <str>} on success. Pass
    `exc` to simulate a failure; use a non-transient message so decide() fails
    fast without retry/backoff sleeps.
    """

    def __init__(self, replies: list[str] | None = None, exc: BaseException | None = None):
        self.replies = list(replies or [])
        self.exc = exc
        self.calls = 0
        self.last_messages = None

    def chat(self, messages, temperature=0.4, max_tokens=200, **kwargs):
        self.calls += 1
        self.last_messages = messages
        if self.exc is not None:
            raise self.exc
        text = self.replies.pop(0) if self.replies else '{"speak": false, "reason": "routine"}'
        return {"text": text, "tool_calls": [], "raw": None}


class FakeAutomationInventory:
    """Home Assistant's loaded automations, as read from an automations.yaml
    at the last reload. ``load_new=False`` models a reload that silently
    skips Nova's new automation."""

    def __init__(self, path, *, load_new: bool = True):
        self.path = path
        self.load_new = load_new
        self._records: list = []
        self.refreshes = 0
        self.reload()

    def reload(self) -> None:
        import yaml
        try:
            with open(self.path, encoding="utf-8") as handle:
                items = yaml.safe_load(handle) or []
        except (FileNotFoundError, yaml.YAMLError):
            items = []
        if not isinstance(items, list):
            items = []
        self._records = [
            types.SimpleNamespace(
                entity_id=f"automation.{item.get('id')}", unique_id=str(item.get("id")),
                name=str(item.get("alias", "")), raw_config=item,
                referenced_entities=())
            for item in items
            if self.load_new or not str(item.get("id", "")).startswith("nova_auto_")
        ]

    def refresh(self) -> list:
        self.refreshes += 1
        return self.records()

    def records(self) -> list:
        return list(self._records)


class FakeUserInput:
    """A live conversation turn's ConversationInput: the parts the agent reads
    (text, language, device_id and the requesting user's context). The agent
    treats a run with no user_input as headless scheduled work."""

    def __init__(self, text: str = "", *, device_id=None, user_id="user-1",
                 language: str = "en", conversation_id=None):
        import types as _types
        self.text = text
        self.language = language
        self.device_id = device_id
        self.conversation_id = conversation_id
        self.context = _types.SimpleNamespace(user_id=user_id)
