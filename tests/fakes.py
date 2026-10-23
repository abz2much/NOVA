"""Small stand-ins for the parts of Home Assistant that Nova's modules touch.

Each fake covers only what the code under test calls, so unit tests stay
quick and give the same result every run. Nothing here imports Home
Assistant.
"""
from __future__ import annotations

import types
from datetime import datetime, timezone

_DEFAULT_REPLY = '{"speak": false, "reason": "routine"}'


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _no_op() -> None:
    return None


class FakeState:
    """One entity's state as Nova reads it.

    With no times given, last_changed and last_updated are the moment the
    state was made, as when Home Assistant first sets a state. Tests about
    staleness pass their own times."""

    __slots__ = ("entity_id", "state", "attributes", "last_changed", "last_updated")

    def __init__(self, entity_id: str, state, attributes: dict | None = None,
                 last_changed=None, last_updated=None):
        made_at = _utc_now()
        self.entity_id, self.state = entity_id, state
        self.attributes = attributes or {}
        self.last_changed = last_changed or made_at
        self.last_updated = last_updated or last_changed or made_at

    def __repr__(self):  # pragma: no cover - only for reading test failures
        return f"<FakeState {self.entity_id}={self.state!r} {self.attributes}>"


class FakeStates:
    """hass.states: set, get, remove, and async_all filtered by domain the
    way Home Assistant does it (by the "domain." prefix)."""

    def __init__(self):
        self._d: dict[str, FakeState] = {}

    def _store(self, state: FakeState) -> FakeState:
        self._d[state.entity_id] = state
        return state

    def set(self, entity_id: str, state, last_changed=None, last_updated=None,
            **attributes) -> FakeState:
        """Test helper: attributes as keyword arguments."""
        return self._store(FakeState(entity_id, state, attributes,
                                     last_changed=last_changed, last_updated=last_updated))

    def async_set(self, entity_id: str, state, attributes: dict | None = None,
                  last_changed=None, last_updated=None) -> FakeState:
        """Home Assistant's own signature: attributes as one dict, copied so
        the caller's dict can change afterwards."""
        return self._store(FakeState(entity_id, state, dict(attributes or {}),
                                     last_changed=last_changed, last_updated=last_updated))

    def remove(self, entity_id) -> None:
        if entity_id in self._d:
            del self._d[entity_id]

    def get(self, entity_id):
        """The state, or None for an unknown entity."""
        return self._d.get(entity_id, None)

    def async_all(self, domain=None):
        prefix = "" if domain is None else f"{domain}."
        return [st for eid, st in self._d.items() if eid.startswith(prefix)]


class _Services:
    """hass.services. Calls are written to a shared list, never run, so a
    test can check that Nova asked for something without any device."""

    def __init__(self, sink: list) -> None:
        self._sink, self._registered = sink, {}   # domain -> {service name: None}

    async def async_call(self, domain, service, data=None, blocking=False,
                         **kwargs) -> None:
        self._sink.append((domain, service, {**(data or {})}))

    def register(self, domain, service) -> None:
        """Test helper: make async_services() and has_service() list it."""
        self._registered.setdefault(domain, {}).update({service: None})

    def async_services(self) -> dict:
        """Every registered service, by domain."""
        return self._registered

    def has_service(self, domain: str, service: str) -> bool:
        return service in self._registered.get(domain, {})


class FakeRegistryEntry:
    """The entity registry entry fields Nova reads when it finds entities
    by the integration that owns them (host_health.py and others)."""

    __slots__ = ("entity_id", "platform", "unique_id", "translation_key",
                 "disabled_by", "device_id", "area_id",
                 "original_unit_of_measurement", "original_state_class")

    def __init__(self, entity_id: str, platform: str, unique_id: str = "",
                 translation_key: str | None = None, disabled_by=None,
                 device_id: str | None = None, area_id: str | None = None,
                 original_unit_of_measurement: str | None = None,
                 original_state_class: str | None = None):
        given = dict(locals())
        for field in self.__slots__:
            setattr(self, field, given[field])


class FakeEntityRegistry:
    """The entity registry: an `entities` mapping and async_get()."""

    def __init__(self):
        self.entities: dict[str, FakeRegistryEntry] = {}

    def add(self, entry: FakeRegistryEntry) -> FakeRegistryEntry:
        self.entities[entry.entity_id] = entry
        return entry

    def async_get(self, entity_id: str):
        return self.entities.get(entity_id)


class _Bus:
    """hass.bus. Fired events are kept in `fired` as (type, data)."""

    def __init__(self) -> None:
        self.fired: list = []

    def async_listen(self, *args, **kwargs):
        return _no_op   # what Home Assistant gives back to unsubscribe

    def async_fire(self, event_type, event_data=None, **kwargs) -> None:
        self.fired.append((event_type, {**(event_data or {})}))


class FakeHass:
    """The core object. What it offers:

    - states (FakeStates) and services (calls land in `service_calls`)
    - async_add_executor_job: runs the function at once, on this thread
    - async_create_task and async_create_background_task: the coroutines
      are kept in `_tasks`; drain() runs them, close_pending() drops them
    - bus (events land in bus.fired), data (a plain dict), config
    """

    def __init__(self) -> None:
        self.states, self.bus, self.data = FakeStates(), _Bus(), {}
        self.config = types.SimpleNamespace(time_zone="America/New_York")
        self.service_calls: list = []
        self._services = _Services(self.service_calls)
        self._tasks: list = []

    @property
    def services(self):
        """hass.services, swappable by subclasses through `_services`."""
        return self._services

    async def async_add_executor_job(self, func, *args):
        # No thread pool: the same order every run.
        result = func(*args)
        return result

    def _keep(self, coro):
        self._tasks.append(coro)
        return coro

    def async_create_task(self, coro, name=None):
        """Kept, not started."""
        return self._keep(coro)

    def async_create_background_task(self, coro, name=None):
        """Kept, not started."""
        return self._keep(coro)

    async def drain(self) -> None:
        """Await every kept coroutine, oldest first."""
        queued, self._tasks = self._tasks, []
        for job in queued:
            await job

    def close_pending(self) -> None:
        """Drop kept coroutines without running them, so Python does not
        warn that they were never awaited."""
        queued, self._tasks = self._tasks, []
        for job in queued:
            job.close()


class FakeProvider:
    """A model client for reasoning_loop.decide().

    chat() answers with the next queued reply (or a quiet "routine" verdict
    once they run out), or raises `exc` when one is given. Use an error
    that is not retried, so decide() gives up without waiting."""

    def __init__(self, replies: list[str] | None = None,
                 exc: BaseException | None = None) -> None:
        self.replies, self.exc = list(replies or []), exc
        self.calls, self.last_messages = 0, None

    def chat(self, messages, temperature=0.4, max_tokens=200,
             **kwargs) -> dict:
        self.calls, self.last_messages = self.calls + 1, messages
        if self.exc is not None:
            raise self.exc
        reply = self.replies.pop(0) if self.replies else _DEFAULT_REPLY
        return {"text": reply, "tool_calls": [], "raw": None}


class FakeAutomationInventory:
    """Home Assistant's loaded automations, read from an automations.yaml
    at the last reload. ``load_new=False`` stands for a reload that quietly
    leaves out the automations Nova just wrote."""

    def __init__(self, path, *, load_new: bool = True):
        self.path = path
        self.load_new = load_new
        self.refreshes = 0
        self._records: list = []
        self.reload()

    @staticmethod
    def _read_items(path) -> list:
        import yaml
        try:
            with open(path, encoding="utf-8") as handle:
                parsed = yaml.safe_load(handle)
        except (FileNotFoundError, yaml.YAMLError):
            return []
        return parsed if isinstance(parsed, list) else []

    def reload(self) -> None:
        kept = [item for item in self._read_items(self.path)
                if self.load_new or not str(item.get("id", "")).startswith("nova_auto_")]
        self._records = [
            types.SimpleNamespace(
                entity_id=f"automation.{item.get('id')}", unique_id=str(item.get("id")),
                name=str(item.get("alias", "")), raw_config=item, referenced_entities=())
            for item in kept
        ]

    def refresh(self) -> list:
        self.refreshes += 1
        return self.records()

    def records(self) -> list:
        return list(self._records)


class FakeUserInput:
    """A live conversation turn: text, language, device_id and the user's
    context. A run without one is treated as scheduled work."""

    def __init__(self, text: str = "", *, device_id=None, user_id="user-1",
                 language: str = "en", conversation_id=None):
        self.text = text
        self.language = language
        self.device_id = device_id
        self.conversation_id = conversation_id
        self.context = types.SimpleNamespace(user_id=user_id)


class FakeConfigEntry:
    """A loaded config entry; its runtime sits in entry.runtime_data."""

    def __init__(self, runtime, domain: str = "nova", entry_id: str = "nova-test"):
        self.domain = domain
        self.entry_id = entry_id
        self.runtime_data = runtime
        self.state = None


class FakeConfigEntries:
    def __init__(self, entries=()):
        self._entries = list(entries)

    def async_entries(self, domain=None):
        return [entry for entry in self._entries if domain in (None, entry.domain)]


def make_runtime(load, **overrides):
    """A NovaRuntime whose setup-built parts are empty placeholders."""
    parts = {"client": None, "llm_provider_name": "test", "sentinel": None,
             "reminder_watcher": None, "scheduler": None, "resources": None,
             "automation_contexts": None, **overrides}
    return load("runtime").NovaRuntime(**parts)


def attach_runtime(hass, runtime):
    """Load `runtime` as this hass's Nova entry, so the runtime lookup finds
    it the same way it does in production."""
    entry = FakeConfigEntry(runtime)
    hass.config_entries = FakeConfigEntries([entry])
    return entry
