"""The integration entry point (__init__.py), checked against its spec (7e).

Setup builds one NovaRuntime per config entry and wires the listeners and
periodic ticks that live as long as the entry: camera auto-analysis (Nest,
Frigate, Eufy), package and mail checks, health, hazard, document, sleep and
purge sweeps, and the clock-time briefings. Unload tears it all down in a
fixed order, and a setup that fails late reuses unload before re-raising.

The module is loaded on its own with every sibling it imports replaced by a
recording stub, so these tests see exactly what it calls, in which order and
with which arguments. The real runtime.py is used, so the runtime accessors
behave as in production. The PHACC suite (tests/integration) drives the same
paths against a real Home Assistant.
"""
from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import logging
import pathlib
import sys
import time
import types
from contextlib import contextmanager
from datetime import timedelta

import pytest

from fakes import FakeHass, FakeState

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


# ── The recording world ─────────────────────────────────────────────────────

class World:
    """Everything the stubs read from and write to."""

    def __init__(self):
        self.log: list = []              # (name, args, kwargs) in call order
        self.fail: dict = {}             # stub name -> exception to raise
        self.eff = {"llm_provider": "ollama", "model": "m1",
                    "api_key": "k1", "base_url": "http://llm"}
        self.cfg: dict = {}              # nova_config.get values
        self.panel: dict = {}            # nova_config.get_all
        self.last_load_error = None
        self.eufy_roles: dict = {}
        self.pkg_sensors: dict = {}
        self.nest_map: dict = {}
        self.away = False
        self.sched_ok = True
        self.unload_ok = True
        self.purged = (0, 0)
        self.docs = (0, 0)
        self.hazard = {"fired": False}
        self.time_changes: list = []
        self.state_tracks: list = []
        self.schedules: list = []
        self.resources = None

    def names(self) -> list[str]:
        return [n for n, _a, _k in self.log]

    def calls(self, name: str) -> list:
        return [(a, k) for n, a, k in self.log if n == name]

    def _check(self, name):
        if name in self.fail:
            raise self.fail[name]


def _mod(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _install(monkeypatch, load, world: World):
    def sync(name, fn=None):
        def f(*a, **k):
            world.log.append((name, a, k))
            world._check(name)
            return fn(*a, **k) if fn else None
        return f

    def asyn(name, fn=None):
        async def f(*a, **k):
            world.log.append((name, a, k))
            world._check(name)
            return fn(*a, **k) if fn else None
        return f

    # Home Assistant helpers the module imports at load time.
    def _track_time(hass, cb, hour=None, minute=None, second=None):
        world.time_changes.append((cb, hour, minute, second))
        world._check("track_time")
        return sync("unsub.time")

    def _track_state(hass, ids, cb):
        world.state_tracks.append((list(ids), cb))
        return sync("unsub.state")

    ha = {
        "homeassistant.helpers.config_validation": _mod(
            "cv", config_entry_only_config_schema=lambda d: ("entry_only", d)),
        "homeassistant.helpers.event": _mod(
            "ev", async_track_time_change=_track_time,
            async_track_state_change_event=_track_state),
        "homeassistant.helpers.typing": _mod("typing", ConfigType=dict),
    }
    for key, mod in ha.items():
        monkeypatch.setitem(sys.modules, key, mod)

    load("const")
    rt = load("runtime")

    class Scheduler:
        def __init__(self, hass):
            world.log.append(("NovaScheduler", (), {}))

        def add(self, name, interval, cb):
            world.schedules.append((name, interval, cb))
            return world.sched_ok

    class Resources:
        def __init__(self):
            self.unsubs, self.closeables, self.async_closeables = [], [], []
            world.resources = self

        def add_unsub(self, u):
            self.unsubs.append(u)

        def add_unsubs(self, us):
            self.unsubs.extend(us or [])

        def add_closeable(self, c):
            self.closeables.append(c)

        def add_async_closeable(self, c):
            self.async_closeables.append(c)

        async def async_close_all(self):
            world.log.append(("resources.close_all", (), {}))
            world._check("resources.close_all")
            for u in self.unsubs:
                u()
            return {"closed": len(self.unsubs)}

    class Providers:
        def __init__(self, hass):
            world.log.append(("ProviderManager", (), {}))

        async def async_acquire(self, spec, binding, factory):
            world.log.append(("providers.acquire", (spec,), {"binding": binding}))
            return factory()

        async def async_close(self):
            world.log.append(("providers.close", (), {}))

    class Tracker:
        inventory = None

        def __init__(self):
            world.log.append(("AutomationContextTracker", (), {}))

        def record_trigger(self, event):
            world.log.append(("record_trigger", (event,), {}))

    class Inventory:
        def __init__(self, hass):
            world._check("AutomationInventory")

        def start(self):
            world.log.append(("inventory.start", (), {}))

    class Owner:
        """Sentinel / reminder watcher stand-in."""
        kind = ""

        def __init__(self, *a, **k):
            world.log.append((self.kind, a, k))
            self.args, self.kwargs = a, k

        async def async_start(self):
            world.log.append((f"{self.kind}.start", (), {}))
            world._check(f"{self.kind}.start")

        async def async_stop(self):
            world.log.append((f"{self.kind}.stop", (), {}))

    Sentinel = type("Sentinel", (Owner,), {"kind": "NovaSentinel"})
    Reminders = type("Reminders", (Owner,), {"kind": "ReminderWatcher"})

    def _create_provider(name, key, model, url):
        world._check("create_provider")
        return types.SimpleNamespace(name=name, key=key, model=model, url=url)

    def _get(key, default=None):
        world._check("nova_config.get")
        return world.cfg.get(key, default)

    stubs = {
        "paths": _mod("paths", configure=sync("paths.configure")),
        "services": _mod(
            "services", async_setup_services=sync("async_setup_services"),
            _get_tts=lambda hass, entry, context=None: f"tts.{context}",
            _get_speakers=lambda hass, entry: ["media_player.one"],
            _live_honorific=lambda hass: "boss"),
        "output_gate": _mod("output_gate", load_mutes=sync("output_gate.load_mutes")),
        "face_roster": _mod("face_roster", load=sync("face_roster.load")),
        "nova_config": _mod(
            "nova_config", configure=sync("nova_config.configure"),
            effective_config=sync("effective_config", lambda e: dict(world.eff)),
            effective_config_with_runtime=sync(
                "effective_config_with_runtime", lambda e, rc: {"rc": rc}),
            get=_get, init_from_entry=sync("init_from_entry"),
            get_all=sync("get_all", lambda: dict(world.panel)),
            _config_path=lambda: "cfgdir/nova/config.json"),
        "ha_secrets": _mod(
            "ha_secrets", _read_secrets=sync("ha_secrets._read_secrets"),
            relocate_plaintext_credentials=asyn("relocate_plaintext_credentials"),
            split_shared_credential=asyn("split_shared_credential")),
        "modes": _mod("modes", _load=sync("modes._load")),
        "intrusion": _mod("intrusion", _load_log=sync("intrusion._load_log"),
                          migrate_legacy_snapshots=sync("migrate_legacy_snapshots")),
        "reasoning_cache": _mod("reasoning_cache", load=sync("reasoning_cache.load")),
        "habituation": _mod("habituation", _load=sync("habituation._load")),
        "scene_memory": _mod("scene_memory", prune=sync("scene_memory.prune")),
        "persistence": _mod("persistence"),
        "persistence.sqlite": _mod("sqlite", upgrade_existing=sync("upgrade_existing")),
        "spoken_history": _mod("spoken_history",
                               configure=sync("spoken_history.configure"),
                               hydrate=sync("spoken_history.hydrate")),
        "action_log": _mod("action_log", configure=sync("action_log.configure")),
        "scheduler": _mod("scheduler", NovaScheduler=Scheduler),
        "resources": _mod("resources", NovaResources=Resources),
        "automation": _mod("automation"),
        "automation.attribution": _mod("attribution", AutomationContextTracker=Tracker),
        "automation.trials": _mod("trials", async_handle_triggered=asyn("trials.handle")),
        "automation.inventory": _mod("inventory", AutomationInventory=Inventory),
        "providers": _mod("providers"),
        "providers.manager": _mod("manager", ProviderManager=Providers),
        "providers.routing": _mod("routing", ProviderSpec=types.SimpleNamespace),
        "llm_provider": _mod(
            "llm_provider", create_provider=_create_provider,
            resolve_provider_credential=lambda eff, name: eff.get("api_key"),
            resolve_provider_endpoint=lambda eff, name: eff.get("base_url")),
        "migrations": _mod(
            "migrations", CURRENT_SCHEMA_VERSION=5,
            migrate_config=sync("migrate_config", lambda d, o, v: (d, o, 5)),
            schema_version_is_valid=lambda v: isinstance(v, int) and v >= 1),
        "camera": _mod(
            "camera",
            register_event_listeners=sync("register_event_listeners",
                                          lambda hass: [sync("unsub.camera")]),
            async_auto_analyze_on_event=asyn("auto_analyze"),
            async_visitor_observation=asyn("visitor_observation"),
            _nest_device_to_camera=lambda hass, dev: world.nest_map.get(dev),
            _camera_friendly_name=lambda hass, eid: "Porch"),
        "briefing": _mod("briefing", async_briefing=asyn("async_briefing")),
        "routines": _mod("routines", list_routines=lambda: ["wake", "sleep"]),
        "reminders": _mod("reminders", ReminderWatcher=Reminders),
        "recognition": _mod("recognition", register_recognition_listener=asyn(
            "register_recognition_listener", lambda hass: [sync("unsub.recognition")])),
        "sentinel": _mod("sentinel", NovaSentinel=Sentinel),
        "database": _mod("database", purge_old_records=sync(
            "purge_old_records", lambda days: world.purged[0])),
        "panel_register": _mod("panel_register",
                               async_register_panel=asyn("register_panel"),
                               async_unregister_panel=sync("unregister_panel")),
        "websocket": _mod("websocket", async_register=sync("register_ws"),
                          PANEL_WRITABLE_KEYS=("model", "camera_auto_analyze")),
        "proactive_audio": _mod(
            "proactive_audio",
            async_setup_proactive_audio=asyn("setup_proactive_audio"),
            async_unload_proactive_audio=asyn("unload_proactive_audio")),
        "eufy": _mod("eufy", all_camera_roles=lambda hass: world.eufy_roles),
        "package_monitor": _mod(
            "package_monitor",
            discover_trigger_sensors=lambda hass: world.pkg_sensors,
            note_from_eufy=asyn("note_from_eufy"),
            note_from_mailbox=asyn("note_from_mailbox"),
            note_from_motion=asyn("note_from_motion"),
            periodic_check=asyn("periodic_check", lambda *a, **k: "report"),
            restore_from_situations=sync("restore_from_situations")),
        "diagnostics": _mod("diagnostics", run_service_health=asyn(
            "run_service_health", lambda hass: {"summary": "fine"})),
        "host_health": _mod("host_health", TICK_INTERVAL_SECONDS=120,
                            tick=asyn("host_health.tick")),
        "hazard_monitor": _mod("hazard_monitor", periodic_check=asyn(
            "hazard.check", lambda hass, h: world.hazard)),
        "documents": _mod(
            "documents",
            auto_ingest_new=asyn("docs.ingest", lambda hass: {"new_files": world.docs[0]}),
            scan_watch_folders=asyn("docs.watch", lambda hass: {"new_files": world.docs[1]})),
        "sleep_detection": _mod("sleep_detection",
                                maybe_prompt_sleep=asyn("maybe_prompt_sleep")),
        "knowledge": _mod("knowledge", purge_expired=sync(
            "purge_expired", lambda: world.purged[1])),
        "household": _mod("household", everyone_away=sync(
            "everyone_away", lambda hass: world.away)),
        "adaptive_awareness": _mod("adaptive_awareness", async_listen=sync(
            "adaptive.listen", lambda hass: sync("unsub.adaptive"))),
        "situations": _mod("situations", async_start=asyn("situations.start"),
                           stop=sync("situations.stop")),
        "observer": _mod("observer", start=asyn("observer.start"),
                         stop=asyn("observer.stop")),
        "cognitive_core": _mod(
            "cognitive_core", ensure_lockdown=asyn("ensure_lockdown"),
            start=asyn("core.start"), stop=asyn("core.stop"),
            release_runtime=sync("core.release")),
        "bootstrap": _mod("bootstrap", schedule_bootstrap=sync(
            "schedule_bootstrap", lambda hass: "bootstrap-handle")),
        "voice_recognition": _mod("voice_recognition",
                                  register=sync("voice_recognition.register"),
                                  unregister=sync("voice_recognition.unregister")),
        "voice_enrol": _mod("voice_enrol", clear=sync("voice_enrol.clear")),
        "voice_learn": _mod("voice_learn", clear=sync("voice_learn.clear")),
        "identity": _mod("identity", clear_voice_carry=sync("identity.clear_voice_carry")),
        "delivery": _mod("delivery", DeliveryRequest=types.SimpleNamespace,
                         async_deliver=asyn("deliver")),
        "persona": _mod("persona", lead_in=lambda h, text: f"{h}: {text}"),
        "doorbell_training": _mod("doorbell_training", log_event=sync("log_event")),
        "camera_semantic": _mod("camera_semantic", record_event=asyn("record_event")),
    }
    for key, mod in stubs.items():
        monkeypatch.setitem(sys.modules, f"jc.{key}", mod)
    for parent, child in (("automation", "attribution"), ("automation", "trials"),
                          ("automation", "inventory"), ("providers", "manager"),
                          ("providers", "routing"), ("persistence", "sqlite")):
        setattr(stubs[parent], child, stubs[f"{parent}.{child}"])
    return rt


# ── Fakes for hass and the entry ────────────────────────────────────────────

class _ConfigEntries:
    def __init__(self, world):
        self.world = world

    def async_update_entry(self, entry, data=None, options=None):
        self.world.log.append(("async_update_entry", (), {"data": data, "options": options}))

    async def async_forward_entry_setups(self, entry, platforms):
        self.world.log.append(("forward", (list(platforms),), {}))
        self.world._check("forward")

    async def async_unload_platforms(self, entry, platforms):
        self.world.log.append(("unload_platforms", (list(platforms),), {}))
        return self.world.unload_ok

    async def async_reload(self, entry_id):
        self.world.log.append(("reload", (entry_id,), {}))


class Hass(FakeHass):
    def __init__(self, world):
        super().__init__()
        self.world = world
        self.listeners: dict = {}
        self.background: list = []
        self.config = types.SimpleNamespace(path=lambda *p: "cfgdir",
                                            time_zone="UTC")
        self.config_entries = _ConfigEntries(world)
        self.bus.async_listen = self._listen

    def _listen(self, event_type, cb):
        self.world._check(f"listen.{event_type}")
        self.listeners[event_type] = cb
        return lambda: self.world.log.append((f"unsub.{event_type}", (), {}))

    def async_create_background_task(self, coro, name=None):
        task = asyncio.get_running_loop().create_task(coro)
        self.background.append((name, task))
        return task


class Entry:
    def __init__(self, data=None, options=None):
        self.entry_id = "entry-1"
        self.data = data if data is not None else {"schema_version": 5}
        self.options = options or {}
        self.on_unload: list = []
        self.update_listener = None
        self.state = sys.modules["homeassistant.config_entries"].ConfigEntryState.SETUP_IN_PROGRESS

    def async_on_unload(self, fn):
        self.on_unload.append(fn)

    def add_update_listener(self, fn):
        self.update_listener = fn
        return "update-unsub"


@pytest.fixture
def world():
    return World()


@pytest.fixture
def env(load, monkeypatch, world):
    rt = _install(monkeypatch, load, world)
    # A plain (non-package) spec, so `from .x import` resolves inside jc.
    path = str(COMP / "__init__.py")
    loader = importlib.machinery.SourceFileLoader("jc.nova_entry", path)
    spec = importlib.machinery.ModuleSpec("jc.nova_entry", loader, origin=path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hass = Hass(world)
    entry = Entry()
    yield types.SimpleNamespace(nova=mod, rt=rt, hass=hass, entry=entry, world=world)
    hass.close_pending()


async def _setup(env) -> bool:
    return await env.nova.async_setup_entry(env.hass, env.entry)


def _loaded(env):
    env.entry.state = sys.modules["homeassistant.config_entries"].ConfigEntryState.LOADED


@contextmanager
def _at(monkeypatch, seconds):
    """Pin the monotonic clock the cooldowns read."""
    with monkeypatch.context() as m:
        m.setattr(time, "monotonic", lambda: seconds)
        yield


def _event(data):
    return types.SimpleNamespace(data=data)


def _tick(world, name):
    return next(cb for n, _i, cb in world.schedules if n == name)


# ── Module surface ──────────────────────────────────────────────────────────

def test_module_constants(env):
    assert env.nova.PLATFORMS == ["conversation", "stt"]
    assert env.nova.CONFIG_SCHEMA == ("entry_only", "nova")


def test_names_tests_patch_on_the_package_are_module_globals(env):
    for name in ("create_provider", "NovaRuntime", "async_auto_analyze_on_event",
                 "register_recognition_listener", "async_register_panel",
                 "async_unload_proactive_audio", "_get_speakers", "_get_tts",
                 "_live_honorific", "async_setup_services"):
        assert hasattr(env.nova, name), name


# ── async_setup and the prewarm ─────────────────────────────────────────────

async def test_async_setup_order(env):
    assert await env.nova.async_setup(env.hass, {}) is True
    assert env.world.names() == ["paths.configure", "async_setup_services",
                                 "output_gate.load_mutes", "face_roster.load"]


def test_prewarm_runs_every_step_even_when_each_fails(env):
    w = env.world
    steps = ["ha_secrets._read_secrets", "modes._load", "intrusion._load_log",
             "reasoning_cache.load", "habituation._load", "scene_memory.prune"]
    for s in steps:
        w.fail[s] = RuntimeError(s)
    env.nova._prewarm_persisted_state()
    assert w.names() == steps
    assert w.calls("ha_secrets._read_secrets") == [((), {"force": True})]


# ── Migration ───────────────────────────────────────────────────────────────

async def test_current_schema_is_not_migrated(env):
    await _setup(env)
    assert "migrate_config" not in env.world.names()


async def test_older_schema_is_migrated_and_saved(env, caplog):
    env.entry.data = {"schema_version": 2, "honorific": "chief"}
    env.entry.options = {"x": 1}
    with caplog.at_level(logging.INFO):
        await _setup(env)
    (args, _k), = env.world.calls("migrate_config")
    assert args[2] == 2
    (_a, saved), = env.world.calls("async_update_entry")
    assert saved["data"]["schema_version"] == 5 and saved["options"] == {"x": 1}
    assert "Nova: migrated config from schema v2 to v5" in caplog.text


@pytest.mark.parametrize("version", [None, "two", 0])
async def test_invalid_schema_goes_to_migrate_but_is_never_saved(env, version, caplog):
    env.entry.data = {"schema_version": version}
    await _setup(env)
    assert env.world.calls("migrate_config")
    assert not env.world.calls("async_update_entry")
    assert "migrated config" not in caplog.text


async def test_missing_schema_counts_as_version_one(env):
    env.entry.data = {}
    await _setup(env)
    (args, _k), = env.world.calls("migrate_config")
    assert args[2] == 1


# ── Provider ────────────────────────────────────────────────────────────────

async def test_provider_is_acquired_from_effective_config(env, caplog):
    with caplog.at_level(logging.INFO):
        await _setup(env)
    (spec,), kw = env.world.calls("providers.acquire")[0]
    assert (spec.provider, spec.model, spec.api_key, spec.base_url) == (
        "ollama", "m1", "k1", "http://llm")
    assert kw == {"binding": "primary"}
    runtime = env.entry.runtime_data
    assert runtime.client.name == "ollama" and runtime.llm_provider_name == "ollama"
    assert "Nova: LLM provider 'ollama' initialised (model=m1)" in caplog.text


async def test_provider_defaults(env):
    env.world.eff = {}
    await _setup(env)
    (spec,), _k = env.world.calls("providers.acquire")[0]
    assert (spec.provider, spec.model) == ("groq", "openai/gpt-oss-120b")


async def test_provider_failure_returns_false_and_builds_nothing(env, caplog):
    env.world.fail["create_provider"] = RuntimeError("no key")
    assert await _setup(env) is False
    names = env.world.names()
    assert "providers.close" in names
    assert "NovaSentinel" not in names and "NovaScheduler" not in names
    assert not hasattr(env.entry, "runtime_data")
    assert any(r.levelno == logging.ERROR and
               r.getMessage() == "Nova: LLM provider init failed: no key"
               for r in caplog.records)


# ── Happy path ──────────────────────────────────────────────────────────────

async def test_setup_builds_the_runtime_and_wires_everything(env, caplog):
    with caplog.at_level(logging.INFO):
        assert await _setup(env) is True
    w, rt = env.world, env.entry.runtime_data
    assert isinstance(rt, env.rt.NovaRuntime)
    assert rt.sentinel.args[2] == "sir" and rt.sentinel.kwargs == {"entry": env.entry}
    assert rt.providers in w.resources.async_closeables
    assert rt.scheduler in w.resources.closeables
    assert rt.automation_contexts in w.resources.closeables
    assert "bootstrap-handle" in w.resources.closeables
    assert rt.automation_inventory is not None
    assert rt.automation_contexts.inventory is rt.automation_inventory
    assert w.calls("forward") == [((["conversation", "stt"],), {})]
    assert env.entry.update_listener is env.nova._async_update_listener
    assert env.entry.on_unload == ["update-unsub"]
    assert set(env.hass.listeners) == {"nest_event", "frigate_event", "automation_triggered"}
    assert "Nova online. Good day, sir. Routines available: wake, sleep" in caplog.text
    assert "Nova Observer mode disabled. Enable via addon config → observer_enabled=true" \
        in caplog.text


async def test_setup_order(env):
    await _setup(env)
    order = ["paths.configure", "nova_config.configure", "effective_config",
             "ha_secrets._read_secrets", "upgrade_existing",
             "spoken_history.configure", "spoken_history.hydrate",
             "action_log.configure", "ProviderManager", "providers.acquire",
             "NovaSentinel", "register_event_listeners", "NovaScheduler",
             "AutomationContextTracker", "register_recognition_listener",
             "adaptive.listen", "ReminderWatcher", "inventory.start",
             "init_from_entry", "get_all", "relocate_plaintext_credentials",
             "split_shared_credential", "migrate_legacy_snapshots", "forward",
             "NovaSentinel.start", "ReminderWatcher.start", "situations.start",
             "restore_from_situations", "effective_config_with_runtime",
             "ensure_lockdown", "core.start", "register_panel", "register_ws",
             "setup_proactive_audio", "schedule_bootstrap",
             "voice_recognition.register"]
    names = env.world.names()
    assert [n for n in names if n in order] == order


async def test_honorific_prefers_options_then_data(env, caplog):
    env.entry.data = {"schema_version": 5, "honorific": "chief"}
    env.entry.options = {"honorific": "captain"}
    with caplog.at_level(logging.INFO):
        await _setup(env)
    assert env.entry.runtime_data.sentinel.args[2] == "captain"
    assert "Good day, captain." in caplog.text


async def test_scheduler_ticks_in_order_with_intervals(env, caplog):
    with caplog.at_level(logging.INFO):
        await _setup(env)
    got = [(n, i) for n, i, _cb in env.world.schedules]
    assert got == [("package", timedelta(minutes=15)), ("health", timedelta(hours=1)),
                   ("host_health", timedelta(seconds=120)),
                   ("hazard", timedelta(minutes=10)), ("documents", timedelta(minutes=10)),
                   ("sleep_prompt", timedelta(minutes=10)), ("db_purge", timedelta(hours=24))]
    for line in ("Nova: package/mail detection active (porch sweep every 15 min)",
                 "Nova: hourly service-health sweep active",
                 "Nova: host-health sampling active (every 0:02:00)",
                 "Nova: multi-hazard monitor active (sweep every 10 min)",
                 "Nova: document auto-ingest active (scan every 10 min)",
                 "Nova: nightly sleep-state prompt active (checked every 10 min)",
                 "Nova: automatic database purge active (daily, 30-day retention)"):
        assert line in caplog.text


async def test_refused_ticks_log_nothing(env, caplog):
    env.world.sched_ok = False
    with caplog.at_level(logging.INFO):
        await _setup(env)
    assert "sweep active" not in caplog.text and "purge active" not in caplog.text


async def test_non_fatal_steps_warn_and_setup_carries_on(env, caplog):
    w = env.world
    for name in ("upgrade_existing", "spoken_history.hydrate", "action_log.configure",
                 "AutomationInventory", "relocate_plaintext_credentials",
                 "split_shared_credential", "situations.start", "register_panel",
                 "register_ws", "setup_proactive_audio", "schedule_bootstrap",
                 "voice_recognition.register"):
        w.fail[name] = RuntimeError("x")
    with caplog.at_level(logging.WARNING):
        assert await _setup(env) is True
    warned = {r.getMessage() for r in caplog.records if r.levelno == logging.WARNING}
    assert warned == {
        "Nova: storage upgrade skipped (non-fatal): x",
        "Nova: spoken history hydrate failed (non-fatal): x",
        "Nova: action log configure failed (non-fatal): x",
        "Nova automation inventory unavailable (non-fatal): x",
        "Credential relocation failed — credential(s) remain in config.json, "
        "not moved to secrets.yaml: x",
        "Credential provider-split migration failed — the shared credential "
        "remains in place, unmigrated: x",
        "Nova situations restore failed (non-fatal): x",
        "Nova panel registration failed (non-fatal): x",
        "Nova WS command registration failed (non-fatal): x",
        "Nova proactive-audio setup failed (non-fatal): x",
        "Nova bootstrap scheduling failed (non-fatal): x",
        "Nova voice recognition registration failed (non-fatal): x",
    }
    assert env.entry.runtime_data.automation_inventory is None


async def test_debug_only_steps_never_fail_setup(env):
    for name in ("migrate_legacy_snapshots", "listen.automation_triggered",
                 "listen.nest_event", "track_time", "init_from_entry"):
        env.world.fail[name] = RuntimeError("x")
    env.world.fail["adaptive.listen"] = RuntimeError("x")
    assert await _setup(env) is True


# ── Panel settings restore ──────────────────────────────────────────────────

async def test_restores_only_known_panel_keys_in_place(env, caplog):
    env.world.panel = {"model": "m9", "broadcast_group": "g", "bedroom_areas": ["b"],
                       "movie_media_player": "media_player.tv", "secret": "no",
                       "observer_quiet_start": "22:00", "observer_quiet_end": "07:00"}
    with caplog.at_level(logging.INFO):
        await _setup(env)
    rc = env.entry.runtime_data.runtime_config
    assert rc == {"model": "m9", "broadcast_group": "g", "bedroom_areas": ["b"],
                  "movie_media_player": "media_player.tv",
                  "observer_quiet_start": "22:00", "observer_quiet_end": "07:00"}
    assert "Restored 6 panel settings from nova_config (7 total keys in file)" in caplog.text
    (args, _k), = env.world.calls("init_from_entry")
    assert args == ({"schema_version": 5}, {})


async def test_nothing_to_restore_logs_nothing(env, caplog):
    with caplog.at_level(logging.INFO):
        await _setup(env)
    assert "panel settings" not in caplog.text


async def test_corrupt_config_raises_a_notification(env, monkeypatch):
    monkeypatch.setattr(sys.modules["jc.nova_config"], "last_load_error",
                        "bad JSON at line 3", raising=False)
    await _setup(env)
    (domain, service, data), = [c[:3] for c in env.hass.service_calls]
    assert (domain, service) == ("persistent_notification", "create")
    assert data == {
        "title": "Nova: config.json was invalid",
        "message": "bad JSON at line 3. Nova started with defaults. Fix the JSON "
                   "in the preserved file and copy it back to "
                   "cfgdir/nova/config.json, then restart.",
        "notification_id": "nova_config_corrupt"}


# ── Late failures reuse unload ──────────────────────────────────────────────

@pytest.mark.parametrize("step", ["forward", "NovaSentinel.start", "ReminderWatcher.start"])
async def test_late_failure_unloads_clears_and_reraises(env, step):
    env.world.fail[step] = RuntimeError("late")
    with pytest.raises(RuntimeError, match="late"):
        await _setup(env)
    names = env.world.names()
    assert "unload_platforms" in names and "resources.close_all" in names
    assert not hasattr(env.entry, "runtime_data")
    assert "situations.start" not in names and "core.start" not in names


async def test_observer_enabled_starts_with_effective_config(env, caplog):
    env.world.eff["observer_enabled"] = True
    with caplog.at_level(logging.INFO):
        await _setup(env)
    (args, kw), = env.world.calls("observer.start")
    assert args[1]["observer_enabled"] is True and kw == {"entry": env.entry}
    assert env.entry.runtime_data.observer_running is True
    assert "Nova Observer mode ENABLED — watching for interesting events" in caplog.text


async def test_observer_start_failure_stops_it_and_reraises(env):
    env.world.eff["observer_enabled"] = True
    env.world.fail["observer.start"] = RuntimeError("obs")
    with pytest.raises(RuntimeError, match="obs"):
        await _setup(env)
    assert "observer.stop" in env.world.names()
    assert not hasattr(env.entry, "runtime_data")
    assert "core.start" not in env.world.names()


# ── Lockdown and the cognitive core ─────────────────────────────────────────

async def test_lockdown_gets_a_snapshot_and_the_core_its_config(env):
    env.world.panel = {"model": "m2"}
    await _setup(env)
    (args, _k), = env.world.calls("ensure_lockdown")
    assert args[1] == {"rc": {"model": "m2"}}
    (core_args, core_kw), = env.world.calls("core.start")
    assert core_args[1] is args[1] and core_kw == {"entry": env.entry}


async def test_lockdown_failure_falls_back_to_effective_config(env, caplog):
    env.world.fail["ensure_lockdown"] = RuntimeError("lk")
    with caplog.at_level(logging.WARNING):
        await _setup(env)
    (core_args, _k), = env.world.calls("core.start")
    assert core_args[1] == env.world.eff
    assert "Nova lockdown wiring failed (non-fatal): lk" in caplog.text


async def test_core_failure_is_an_error_not_fatal(env, caplog):
    env.world.fail["core.start"] = RuntimeError("core")
    assert await _setup(env) is True
    assert any(r.levelno == logging.ERROR and
               r.getMessage() == "Nova cognitive core failed to start: core"
               for r in caplog.records)


# ── Unload ──────────────────────────────────────────────────────────────────

async def test_unload_order_and_release(env):
    env.world.eff["observer_enabled"] = True
    await _setup(env)
    env.world.log.clear()
    assert await env.nova.async_unload_entry(env.hass, env.entry) is True
    names = [n for n in env.world.names() if not n.startswith("unsub.")]
    assert names == ["unregister_panel", "NovaSentinel.stop", "ReminderWatcher.stop",
                     "observer.stop", "core.stop", "core.release", "situations.stop",
                     "voice_recognition.unregister", "resources.close_all",
                     "unload_proactive_audio", "unload_platforms",
                     "voice_enrol.clear", "voice_learn.clear",
                     "identity.clear_voice_carry"]
    assert not hasattr(env.entry, "runtime_data")


async def test_unload_closes_every_listener(env):
    env.world.eufy_roles = {"camera.porch": {"ringing": "binary_sensor.ring"}}
    env.world.pkg_sensors = {"binary_sensor.porch_motion": "motion"}
    await _setup(env)
    env.world.log.clear()
    await env.nova.async_unload_entry(env.hass, env.entry)
    unsubs = [n for n in env.world.names() if n.startswith("unsub.")]
    assert sorted(unsubs) == sorted([
        "unsub.camera", "unsub.nest_event", "unsub.frigate_event", "unsub.state",
        "unsub.state", "unsub.time", "unsub.time", "unsub.recognition",
        "unsub.automation_triggered", "unsub.adaptive"])


async def test_failed_platform_unload_keeps_the_runtime(env):
    await _setup(env)
    env.world.unload_ok = False
    env.world.log.clear()
    assert await env.nova.async_unload_entry(env.hass, env.entry) is False
    assert hasattr(env.entry, "runtime_data")
    assert "voice_enrol.clear" not in env.world.names()
    assert "observer.stop" not in env.world.names()


async def test_failed_observer_stop_is_not_shown_as_running(env):
    """Fixed (bugs.md #14): a failing observer stop on unload still clears
    the running flag."""
    env.world.eff["observer_enabled"] = True
    await _setup(env)
    env.world.fail["observer.stop"] = RuntimeError("stuck")
    env.world.unload_ok = False
    await env.nova.async_unload_entry(env.hass, env.entry)
    assert env.entry.runtime_data.observer_running is False


async def test_unload_without_runtime_still_tears_down_the_domain(env):
    assert await env.nova.async_unload_entry(env.hass, env.entry) is True
    assert env.world.names() == [
        "unregister_panel", "core.stop", "core.release", "situations.stop",
        "voice_recognition.unregister", "unload_proactive_audio", "unload_platforms",
        "voice_enrol.clear", "voice_learn.clear", "identity.clear_voice_carry"]


async def test_unload_survives_every_teardown_error(env):
    await _setup(env)
    for name in ("unregister_panel", "core.stop", "core.release", "situations.stop",
                 "voice_recognition.unregister", "resources.close_all",
                 "unload_proactive_audio", "voice_enrol.clear", "voice_learn.clear",
                 "identity.clear_voice_carry"):
        env.world.fail[name] = RuntimeError("x")
    assert await env.nova.async_unload_entry(env.hass, env.entry) is True
    assert not hasattr(env.entry, "runtime_data")


async def test_update_listener_reloads_the_entry(env):
    await env.nova._async_update_listener(env.hass, env.entry)
    assert env.world.calls("reload") == [(("entry-1",), {})]


# ── Live flags ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, fires", [
    (True, True), (False, False), ("on", True), ("YES", True), ("1", True),
    ("off", False), (0, False), (1, True)])
async def test_flags_accept_text_and_numbers(env, monkeypatch, value, fires):
    env.world.nest_map = {"d1": "camera.door"}
    await _setup(env)
    env.entry.runtime_data.runtime_config["camera_auto_analyze"] = value
    with _at(monkeypatch, 1000.0):
        env.hass.listeners["nest_event"](_event({"device_id": "d1", "type": "doorbell_chime"}))
    assert bool(env.hass._tasks) is fires


async def test_flag_on_a_loaded_entry_without_runtime_raises(env):
    await _setup(env)
    _loaded(env)
    del env.entry.runtime_data
    with pytest.raises(env.rt.NovaRuntimeUnavailable):
        env.hass.listeners["nest_event"](_event({"device_id": "d1", "type": "chime"}))


# ── Nest ────────────────────────────────────────────────────────────────────

async def test_nest_chime_fires_full_analysis_once_per_twelve_seconds(env, monkeypatch):
    env.world.nest_map = {"d1": "camera.door"}
    await _setup(env)
    cb = env.hass.listeners["nest_event"]
    for t in (100.0, 111.9, 112.0):
        with _at(monkeypatch, t):
            cb(_event({"nest_device_id": "d1", "event_type": "DoorBell_Chime"}))
    await env.hass.drain()
    calls = env.world.calls("auto_analyze")
    assert len(calls) == 2
    args, kw = calls[0]
    assert args[2:] == ("boss", "tts.doorbell", ["media_player.one"], "camera.door",
                        "Someone is at the front door")
    assert kw == {"doorbell": True}


async def test_nest_person_is_silent_visitor_learning(env, monkeypatch):
    env.world.nest_map = {"d1": "camera.door"}
    await _setup(env)
    cb = env.hass.listeners["nest_event"]
    for t in (100.0, 279.0, 280.0):
        with _at(monkeypatch, t):
            cb(_event({"device_id": "d1", "type": "camera_person"}))
    await env.hass.drain()
    calls = env.world.calls("visitor_observation")
    assert len(calls) == 2 and calls[0][0][2:] == ("boss", "camera.door")
    assert not env.world.calls("auto_analyze")


async def test_nest_ignores_motion_unknown_devices_and_learning_off(env, monkeypatch):
    env.world.nest_map = {"d1": "camera.door"}
    await _setup(env)
    cb = env.hass.listeners["nest_event"]
    with _at(monkeypatch, 100.0):
        cb(_event({"device_id": "d1", "type": "camera_motion"}))
        cb(_event({"type": "doorbell_chime"}))
        cb(_event({"device_id": "d9", "type": "doorbell_chime"}))
        env.entry.runtime_data.runtime_config["visitor_learning"] = False
        cb(_event({"device_id": "d1", "type": "camera_person"}))
    assert env.hass._tasks == []


# ── Frigate ─────────────────────────────────────────────────────────────────

async def _frigate(env, monkeypatch, t, data):
    with _at(monkeypatch, t):
        env.hass.listeners["frigate_event"](_event(data))


async def test_frigate_is_dormant_without_the_motion_opt_in(env, monkeypatch):
    env.hass.states.set("camera.yard", "idle")
    await _setup(env)
    await _frigate(env, monkeypatch, 1.0, {"type": "new", "after": {"camera": "Yard",
                                                                    "label": "person"}})
    assert env.hass._tasks == []


async def test_frigate_detection_rules(env, monkeypatch):
    env.hass.states.set("camera.yard", "idle")
    await _setup(env)
    env.entry.runtime_data.runtime_config["camera_auto_analyze_motion"] = True
    await _frigate(env, monkeypatch, 1.0, {"type": "update", "after": {"camera": "yard"}})
    await _frigate(env, monkeypatch, 1.0, {"type": "new", "after": {"camera": "gone"}})
    await _frigate(env, monkeypatch, 1.0, {"type": "new", "after": {"camera": "yard",
                                                                    "label": "bird"}})
    await _frigate(env, monkeypatch, 1.0, {"type": "new", "before": {"camera": "Yard",
                                                                     "label": "Dog"}})
    await _frigate(env, monkeypatch, 120.9, {"type": "new", "after": {"camera": "yard"}})
    await _frigate(env, monkeypatch, 121.0, {"type": "new", "after": {"camera": "yard"}})
    await env.hass.drain()
    got = [(a[3], a[5], a[6], k) for a, k in env.world.calls("auto_analyze")]
    assert got == [("tts.camera", "camera.yard", "Dog detected", {"doorbell": False}),
                   ("tts.camera", "camera.yard", "Motion detected", {"doorbell": False})]


async def test_frigate_and_nest_keep_their_own_camera_cooldown(env, monkeypatch):
    """Fixed (bugs.md #12): Frigate detections, Nest person events and Eufy
    strangers each have their own per-camera cooldown, so one never silences
    the others."""
    env.hass.states.set("camera.door", "idle")
    env.world.nest_map = {"d1": "camera.door"}
    await _setup(env)
    env.entry.runtime_data.runtime_config["camera_auto_analyze_motion"] = True
    await _frigate(env, monkeypatch, 10.0, {"type": "new", "after": {"camera": "door"}})
    with _at(monkeypatch, 50.0):
        env.hass.listeners["nest_event"](_event({"device_id": "d1", "type": "person"}))
    await env.hass.drain()
    assert len(env.world.calls("auto_analyze")) == 1
    assert len(env.world.calls("visitor_observation")) == 1


# ── Eufy ────────────────────────────────────────────────────────────────────

ROLES = {"camera.porch": {"ringing": "binary_sensor.ring", "stranger": "binary_sensor.stranger",
                          "person": "binary_sensor.person", "vehicle": "switch.vehicle",
                          "pet": "binary_sensor.pet", "package_delivered": "binary_sensor.pkg",
                          "motion": "binary_sensor.ignored"}}


async def _eufy_env(env, caplog=None):
    env.world.eufy_roles = ROLES
    if caplog is not None:
        with caplog.at_level(logging.INFO):
            await _setup(env)
    else:
        await _setup(env)
    ids, cb = env.world.state_tracks[0]
    return ids, cb


def _change(sensor, state="on", old="off"):
    return _event({"entity_id": sensor, "new_state": FakeState(sensor, state),
                   "old_state": FakeState(sensor, old) if old else None})


async def test_eufy_watches_only_the_known_roles(env, caplog):
    ids, _cb = await _eufy_env(env, caplog)
    assert sorted(ids) == sorted(["binary_sensor.ring", "binary_sensor.stranger",
                                  "binary_sensor.person", "switch.vehicle",
                                  "binary_sensor.pet", "binary_sensor.pkg"])
    assert "Nova: Eufy native-sensor watch active (1 camera(s), 6 sensor(s))" in caplog.text


async def test_no_eufy_sensors_means_no_listener(env):
    await _setup(env)
    assert all("binary_sensor.ring" not in ids for ids, _ in env.world.state_tracks)


async def test_eufy_ringing(env, monkeypatch):
    _ids, cb = await _eufy_env(env)
    for t in (5.0, 10.0, 17.0):
        with _at(monkeypatch, t):
            cb(_change("binary_sensor.ring"))
    with _at(monkeypatch, 100.0):
        cb(_change("binary_sensor.ring", state="off"))
        cb(_change("binary_sensor.unknown"))
    await env.hass.drain()
    got = [(a[6], k) for a, k in env.world.calls("auto_analyze")]
    assert got == [("Someone is ringing the doorbell", {"doorbell": True})] * 2


async def test_eufy_stranger(env, monkeypatch):
    _ids, cb = await _eufy_env(env)
    for t in (5.0, 100.0, 185.0):
        with _at(monkeypatch, t):
            cb(_change("binary_sensor.stranger"))
    await env.hass.drain()
    assert len(env.world.calls("visitor_observation")) == 2
    _a, kw = env.world.calls("record_event")[0]
    assert kw == {"label": "person", "camera_entity": "camera.porch", "source": "eufy",
                  "attribute": False, "detail": "stranger"}


async def test_eufy_known_person_is_logged_not_analysed(env, monkeypatch):
    _ids, cb = await _eufy_env(env)
    with _at(monkeypatch, 5.0):
        cb(_change("binary_sensor.person"))
        cb(_change("binary_sensor.person"))
    await env.hass.drain()
    (args, _k), = env.world.calls("log_event")
    assert args == ("Porch", "camera.porch", "eufy",
                    {"summary": "", "analysis": "", "category": "known_resident",
                     "notable": False})
    (_a, kw), = env.world.calls("record_event")
    assert kw == {"label": "person", "camera_entity": "camera.porch", "source": "eufy",
                  "detail": "known_person"}
    assert not env.world.calls("visitor_observation")


async def test_eufy_person_and_stranger_need_visitor_learning(env, monkeypatch):
    _ids, cb = await _eufy_env(env)
    env.entry.runtime_data.runtime_config["visitor_learning"] = "off"
    with _at(monkeypatch, 5.0):
        cb(_change("binary_sensor.person"))
        cb(_change("binary_sensor.stranger"))
    assert env.hass._tasks == []


async def test_eufy_vehicle_announces_through_delivery(env, monkeypatch):
    _ids, cb = await _eufy_env(env)
    for t in (5.0, 304.0, 305.0):
        with _at(monkeypatch, t):
            cb(_change("switch.vehicle"))
    await env.hass.drain()
    calls = env.world.calls("deliver")
    assert len(calls) == 2
    req = calls[0][0][1]
    assert vars(req) == {"source": "vehicle", "entity_id": "camera.porch",
                         "category": "vehicle",
                         "message": "boss: a vehicle was detected at Porch.",
                         "speakers": ("media_player.one",), "tts_entity": "tts.camera",
                         "tts_context": "camera"}
    _a, kw = env.world.calls("record_event")[0]
    assert kw["label"] == "vehicle" and kw["detail"] == "motion_detection_type_vehicle"


async def test_eufy_pet_is_only_recorded(env, monkeypatch):
    _ids, cb = await _eufy_env(env)
    for t in (5.0, 6.0, 305.0):
        with _at(monkeypatch, t):
            cb(_change("binary_sensor.pet"))
    await env.hass.drain()
    calls = env.world.calls("record_event")
    assert len(calls) == 2 and calls[0][1] == {
        "label": "animal", "camera_entity": "camera.porch", "source": "eufy",
        "detail": "pet_detected"}


async def test_eufy_camera_watch_off_still_allows_packages(env, monkeypatch):
    _ids, cb = await _eufy_env(env)
    rc = env.entry.runtime_data.runtime_config
    rc["camera_auto_analyze"] = False
    with _at(monkeypatch, 5.0):
        cb(_change("binary_sensor.ring"))
        cb(_change("switch.vehicle"))
        cb(_change("binary_sensor.pkg"))
    assert env.hass._tasks == []          # package watch is off by default
    rc["package_detection"] = True
    with _at(monkeypatch, 6.0):
        cb(_change("binary_sensor.pkg"))
    await env.hass.drain()
    (args, _k), = env.world.calls("note_from_eufy")
    assert args[1:] == ("boss", "tts.package", ["media_player.one"], "camera.porch",
                        "package_delivered")


async def test_eufy_setup_error_is_not_fatal(env, monkeypatch):
    monkeypatch.setattr(sys.modules["jc.eufy"], "all_camera_roles",
                        lambda hass: (_ for _ in ()).throw(RuntimeError("eufy")))
    assert await _setup(env) is True


# ── Package early-check sensors ─────────────────────────────────────────────

async def test_package_sensors_only_react_to_a_real_off_to_on(env, caplog):
    env.world.pkg_sensors = {"binary_sensor.porch_motion": "motion",
                             "binary_sensor.mailbox": "mailbox"}
    with caplog.at_level(logging.INFO):
        await _setup(env)
    assert "Nova: package early-check sensors active (1 motion, 1 mailbox)" in caplog.text
    ids, cb = env.world.state_tracks[0]
    assert sorted(ids) == ["binary_sensor.mailbox", "binary_sensor.porch_motion"]
    cb(_change("binary_sensor.mailbox"))          # package watch off
    env.entry.runtime_data.runtime_config["package_detection"] = True
    cb(_change("binary_sensor.mailbox", old="unavailable"))
    cb(_change("binary_sensor.mailbox", old=None))
    cb(_change("binary_sensor.mailbox", state="off"))
    cb(_change("binary_sensor.other"))
    assert env.hass.background == []
    cb(_change("binary_sensor.mailbox"))
    cb(_change("binary_sensor.porch_motion"))
    assert [n for n, _t in env.hass.background] == ["nova_package_sensor_trigger"] * 2
    await asyncio.gather(*(t for _n, t in env.hass.background))
    (margs, _k), = env.world.calls("note_from_mailbox")
    assert margs[3] == "binary_sensor.mailbox"
    assert margs[2]() == ("boss", "tts.package", ["media_player.one"])
    (moargs, _k), = env.world.calls("note_from_motion")
    assert moargs[3] == "binary_sensor.porch_motion"


async def test_package_trigger_errors_are_swallowed(env):
    env.world.pkg_sensors = {"binary_sensor.mailbox": "mailbox"}
    env.world.fail["note_from_mailbox"] = RuntimeError("x")
    await _setup(env)
    env.entry.runtime_data.runtime_config["package_detection"] = True
    _ids, cb = env.world.state_tracks[0]
    cb(_change("binary_sensor.mailbox"))
    (_n, task), = env.hass.background
    await task
    assert task.exception() is None


async def test_unload_cancels_running_package_triggers(env, monkeypatch):
    env.world.pkg_sensors = {"binary_sensor.porch_motion": "motion"}
    gate = asyncio.Event()

    async def _slow(*a):
        await gate.wait()
    monkeypatch.setattr(sys.modules["jc.package_monitor"], "note_from_motion", _slow)
    await _setup(env)
    env.entry.runtime_data.runtime_config["package_detection"] = True
    _ids, cb = env.world.state_tracks[0]
    cb(_change("binary_sensor.porch_motion"))
    (_n, task), = env.hass.background
    await asyncio.sleep(0)
    await env.nova.async_unload_entry(env.hass, env.entry)
    with pytest.raises(asyncio.CancelledError):
        await task


# ── Ticks ───────────────────────────────────────────────────────────────────

async def test_package_tick_is_gated_and_sweeps(env):
    await _setup(env)
    tick = _tick(env.world, "package")
    await tick(None)
    assert not env.world.calls("periodic_check")
    env.entry.runtime_data.runtime_config["package_detection"] = "true"
    await tick(None)
    (args, kw), = env.world.calls("periodic_check")
    assert args[2:] == ("boss", "tts.package", ["media_player.one"])
    assert kw == {"configured_camera": None}


@pytest.mark.parametrize("name, stub", [
    ("package", "periodic_check"), ("health", "run_service_health"),
    ("host_health", "host_health.tick"), ("hazard", "hazard.check"),
    ("documents", "docs.ingest"), ("sleep_prompt", "maybe_prompt_sleep"),
    ("db_purge", "purge_old_records")])
async def test_every_tick_swallows_its_errors(env, name, stub):
    await _setup(env)
    env.entry.runtime_data.runtime_config["package_detection"] = True
    env.world.fail[stub] = RuntimeError("x")
    await _tick(env.world, name)(None)
    assert env.world.calls(stub)


@pytest.mark.parametrize("name, stub", [("host_health", "host_health.tick"),
                                        ("sleep_prompt", "maybe_prompt_sleep")])
async def test_runtime_ticks_use_a_fresh_snapshot(env, name, stub):
    await _setup(env)
    rc = env.entry.runtime_data.runtime_config
    rc["k"] = 1
    env.world.log.clear()
    await _tick(env.world, name)(None)
    (args, _k), = env.world.calls("effective_config_with_runtime")
    assert args[1] == {"k": 1} and args[1] is not rc
    (targs, _k), = env.world.calls(stub)
    assert targs[1] == {"rc": {"k": 1}}


@pytest.mark.parametrize("name, label", [("host_health", "host-health"),
                                         ("sleep_prompt", "sleep-prompt")])
async def test_runtime_ticks_warn_when_the_runtime_is_gone(env, caplog, name, label):
    await _setup(env)
    _loaded(env)
    del env.entry.runtime_data
    with caplog.at_level(logging.WARNING):
        await _tick(env.world, name)(None)
    (rec,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert rec.getMessage().startswith(f"Nova {label} tick skipped: Nova runtime is not available")


async def test_hazard_tick_passes_live_honorific(env):
    await _setup(env)
    env.world.hazard = {"fired": True}
    await _tick(env.world, "hazard")(None)
    (args, _k), = env.world.calls("hazard.check")
    assert args[1] == "boss"


async def test_documents_tick_logs_only_new_files(env, caplog):
    await _setup(env)
    with caplog.at_level(logging.INFO):
        await _tick(env.world, "documents")(None)
        assert "auto-ingested" not in caplog.text
        env.world.docs = (2, 3)
        await _tick(env.world, "documents")(None)
    assert "Nova: auto-ingested 5 new document(s)" in caplog.text
    assert len(env.world.calls("docs.watch")) == 2


async def test_db_purge_tick(env, caplog):
    await _setup(env)
    with caplog.at_level(logging.INFO):
        await _tick(env.world, "db_purge")(None)
        assert "daily purge" not in caplog.text
        env.world.purged = (4, 0)
        await _tick(env.world, "db_purge")(None)
    assert "Nova daily purge: 4 conversation record(s), 0 expired fact(s)" in caplog.text
    assert env.world.calls("purge_old_records")[0][0] == (30,)


# ── Briefings ───────────────────────────────────────────────────────────────

def _briefing_cb(world, hour):
    return next(cb for cb, h, _m, _s in world.time_changes if h == hour)


async def test_briefings_scheduled_at_default_clock_times(env, caplog):
    with caplog.at_level(logging.INFO):
        await _setup(env)
    assert [(h, m, s) for _cb, h, m, s in env.world.time_changes] == [(7, 30, 0), (19, 30, 0)]
    assert "Nova: briefings scheduled (morning 07:30, evening 19:30)" in caplog.text


@pytest.mark.parametrize("morning, expected", [
    ("6:05", (6, 5)), ("23:59:10", (23, 59)), ("24:00", (7, 30)), ("noon", (7, 30)),
    ("7:60", (7, 30)), (None, (7, 30))])
async def test_briefing_time_parsing(env, morning, expected):
    env.world.cfg["briefing_morning_time"] = morning
    await _setup(env)
    _cb, h, m, _s = env.world.time_changes[0]
    assert (h, m) == expected


async def test_briefing_off_by_default(env):
    await _setup(env)
    await _briefing_cb(env.world, 7)(None)
    assert not env.world.calls("async_briefing")


async def test_morning_briefing_content(env, caplog):
    env.world.cfg.update({"briefing_morning_enabled": True,
                          "briefing_include_energy": False})
    env.entry.options = {"honorific": "chief"}
    await _setup(env)
    with caplog.at_level(logging.INFO):
        await _briefing_cb(env.world, 7)(None)
    (args, _k), = env.world.calls("async_briefing")
    # Fixed (bugs.md #13): the presence aware honorific, not the configured one.
    assert args[2:] == (env.entry.runtime_data.client, "boss", "tts.briefing",
                        ["media_player.one"])
    assert args[1].data == {"announce": True, "include_weather": True,
                            "include_calendar": True, "include_presence": True,
                            "include_events": True, "include_energy": False,
                            "include_hazards": True, "hours": 12}
    assert "Nova: delivered morning briefing" in caplog.text


async def test_evening_briefing_looks_back_fourteen_hours(env):
    env.world.cfg["briefing_evening_enabled"] = True
    await _setup(env)
    await _briefing_cb(env.world, 19)(None)
    (args, _k), = env.world.calls("async_briefing")
    assert args[1].data["hours"] == 14


async def test_briefing_skips_an_empty_house_unless_allowed(env):
    env.world.cfg["briefing_morning_enabled"] = True
    env.world.away = True
    await _setup(env)
    await _briefing_cb(env.world, 7)(None)
    assert not env.world.calls("async_briefing")
    env.world.cfg["briefing_require_home"] = False
    await _briefing_cb(env.world, 7)(None)
    assert env.world.calls("async_briefing")


async def test_briefing_presence_error_fails_open(env):
    env.world.cfg["briefing_morning_enabled"] = True
    env.world.fail["everyone_away"] = RuntimeError("x")
    await _setup(env)
    await _briefing_cb(env.world, 7)(None)
    assert env.world.calls("async_briefing")


async def test_briefing_failure_is_a_warning(env, caplog):
    env.world.cfg["briefing_evening_enabled"] = True
    env.world.fail["async_briefing"] = RuntimeError("tts down")
    await _setup(env)
    with caplog.at_level(logging.WARNING):
        await _briefing_cb(env.world, 19)(None)
    assert "Nova evening briefing failed: tts down" in caplog.text


async def test_briefing_config_errors_fall_back_to_defaults(env):
    env.world.cfg["briefing_morning_enabled"] = True
    await _setup(env)
    env.world.fail["nova_config.get"] = RuntimeError("x")
    await _briefing_cb(env.world, 7)(None)
    assert not env.world.calls("async_briefing")   # enabled falls back to False


async def test_briefing_text_false_counts_as_off(env):
    """Fixed (bugs.md #11): briefing settings are read like the camera flags,
    so a text "false" or "off" is off and "on" is on."""
    env.world.cfg.update({"briefing_morning_enabled": "false"})
    await _setup(env)
    await _briefing_cb(env.world, 7)(None)
    assert not env.world.calls("async_briefing")
    env.world.cfg.update({"briefing_morning_enabled": "on",
                          "briefing_include_weather": "off"})
    await _briefing_cb(env.world, 7)(None)
    (args, _k), = env.world.calls("async_briefing")
    assert args[1].data["include_weather"] is False


# ── Automation probation ────────────────────────────────────────────────────

async def test_automation_triggered_records_then_queues_trials(env):
    await _setup(env)
    event = _event({"entity_id": "automation.a"})
    env.world.log.clear()
    env.hass.listeners["automation_triggered"](event)
    assert env.world.names() == ["record_trigger"]
    await env.hass.drain()
    assert env.world.calls("trials.handle") == [((env.hass, event), {})]


async def test_reminder_watcher_getters_are_live(env):
    await _setup(env)
    kw = env.entry.runtime_data.reminder_watcher.kwargs
    assert kw["honorific_getter"]() == "boss"
    assert kw["tts_getter"]() == "tts.reminder"
    assert kw["speakers_getter"]() == ["media_player.one"]
