"""Shared set-up for Nova's unit tests.

Order matters. Nova's modules import from Home Assistant when they load, so
the stand-in Home Assistant modules below go into sys.modules first, as soon
as pytest imports this file and before it collects any test.

Nova's modules are then loaded as members of a made-up package called `jc`,
whose search path is the component folder. Loading the real package would
run its __init__.py and set up the whole integration.
"""
from __future__ import annotations

import datetime as _dt
import enum
import importlib.util
import os
import pathlib
import sys
import types

# fakes.py sits next to this file.
sys.path.insert(0, os.path.dirname(__file__))


# ── 1. Stand-in Home Assistant modules ──────────────────────────────────────

def _module(name: str, **members) -> types.ModuleType:
    made = types.ModuleType(name)
    for key, value in members.items():
        setattr(made, key, value)
    return made


def _ha_core() -> types.ModuleType:
    class HomeAssistant:          # used only in type hints
        pass

    class Event:
        def __init__(self, event_type="", data=None):
            self.event_type, self.data = event_type, data or {}

    class State:
        def __init__(self, entity_id, state, attributes=None):
            self.entity_id, self.state = entity_id, state
            self.attributes = attributes or {}

    class ServiceCall:            # used in camera.py's type hints
        def __init__(self, domain="", service="", data=None):
            self.domain, self.service = domain, service
            self.data = data or {}

    return _module("homeassistant.core", HomeAssistant=HomeAssistant, Event=Event,
                   State=State, ServiceCall=ServiceCall, callback=lambda func: func)


def _tool_input_init(self, tool_name="", tool_args=None):
    self.tool_name = tool_name
    self.tool_args = tool_args or {}


async def _no_camera_image(hass, entity_id, timeout=10):
    return None


def _ha_modules() -> dict[str, types.ModuleType]:
    """Every stand-in module, by the name it is imported as."""
    dt = _module(
        "homeassistant.util.dt",
        utcnow=lambda: _dt.datetime.now(_dt.timezone.utc),
        now=lambda tz=None: _dt.datetime.now(tz),
        as_local=lambda value: value,
        parse_datetime=lambda text: None)
    # Empty registries. Tests that need areas or devices patch the Nova
    # helper that reads them instead.
    registries = {
        "entity_registry": _module(
            "homeassistant.helpers.entity_registry",
            async_get=lambda hass: types.SimpleNamespace(
                entities={}, async_get=lambda entity_id: None)),
        "device_registry": _module(
            "homeassistant.helpers.device_registry",
            async_get=lambda hass: types.SimpleNamespace(devices={})),
        "area_registry": _module(
            "homeassistant.helpers.area_registry",
            async_get=lambda hass: types.SimpleNamespace(
                async_get_area=lambda area_id: None, async_list_areas=lambda: [])),
        "aiohttp_client": _module(
            "homeassistant.helpers.aiohttp_client",
            async_get_clientsession=lambda hass: None),
        "network": _module(
            "homeassistant.helpers.network",
            get_url=lambda hass, **kw: "http://127.0.0.1:8123"),
        # Enough of Repairs for repair_notices to import and be patched.
        "issue_registry": _module(
            "homeassistant.helpers.issue_registry",
            async_create_issue=lambda *a, **k: None,
            async_delete_issue=lambda *a, **k: None,
            IssueSeverity=types.SimpleNamespace(
                ERROR="error", WARNING="warning", CRITICAL="critical")),
    }
    # agent.py imports the llm helper when it loads. Without this, whether
    # agent imports would depend on which test file ran first.
    llm = _module("homeassistant.helpers.llm",
                  async_get_api=lambda *a, **k: None,
                  ToolInput=type("ToolInput", (), {"__init__": _tool_input_init}))
    helpers = _module("homeassistant.helpers", **registries)

    # runtime.current_runtime() compares an entry's state with LOADED.
    entry_state = enum.Enum("ConfigEntryState", {
        "LOADED": "loaded", "SETUP_IN_PROGRESS": "setup_in_progress",
        "SETUP_ERROR": "setup_error", "SETUP_RETRY": "setup_retry",
        "NOT_LOADED": "not_loaded", "FAILED_UNLOAD": "failed_unload",
        "UNLOAD_IN_PROGRESS": "unload_in_progress",
    })
    config_entries = _module("homeassistant.config_entries",
                             ConfigEntry=type("ConfigEntry", (), {}),
                             ConfigEntryState=entry_state)

    camera = _module("homeassistant.components.camera", async_get_image=_no_camera_image)
    components = _module("homeassistant.components", camera=camera)
    # Any constant Nova asks for is simply its own name.
    const = _module("homeassistant.const", __getattr__=lambda name: name)

    core = _ha_core()
    util = _module("homeassistant.util", dt=dt)
    root = _module("homeassistant", core=core, util=util, helpers=helpers,
                   config_entries=config_entries, const=const)

    found = {mod.__name__: mod for mod in (
        root, core, util, dt, helpers, llm, config_entries, const, components, camera)}
    found.update({mod.__name__: mod for mod in registries.values()})
    return found


if "homeassistant.core" not in sys.modules:
    sys.modules.update(_ha_modules())


# ── 2. The `jc` package and two stand-in Nova modules ───────────────────────

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMP = ROOT.joinpath("custom_components", "nova")


class _JCPackage(types.ModuleType):
    """A plain ModuleType lets Python's real import machinery permanently
    stick a submodule onto it as an attribute the first time ANY code does a
    genuine `from . import X` for a name not yet in sys.modules (this happens
    inside component modules themselves, e.g. audio_routing.py's function-
    local `from . import nova_config` — not just via our own _load() below).
    That stale attribute then shadows sys.modules for every later `from .
    import X`, which resolves via getattr(jc_pkg, "X") first and only falls
    back to sys.modules if unset — silently defeating any test that patches
    sys.modules["jc.X"] directly (a documented, previously-hit fragility;
    see _load()'s docstring). Making sys.modules the single source of truth
    here, for every access, closes the whole class of bug at its root.

    Test files must NOT also monkeypatch.setattr(jc_pkg, name, ...) alongside
    monkeypatch.setitem(sys.modules, f"jc.{name}", ...) — with this override,
    monkeypatch's own getattr-based "old value" snapshot for the setattr call
    reads back through sys.modules (already mutated by the setitem moments
    earlier in the same test), so its snapshot is wrong and its teardown
    restore leaves a stale, unreachable-but-real entry in jc_pkg.__dict__
    that can resurface if sys.modules[key] is later removed elsewhere.
    sys.modules alone is sufficient with this class in place — plain
    monkeypatch.setitem(sys.modules, f"jc.{name}", stub) is the only patch
    a test needs."""
    def __getattribute__(self, name):
        if not name.startswith("_"):
            mod = sys.modules.get(f"jc.{name}")
            if mod is not None:
                return mod
        return super().__getattribute__(name)


def _install_jc_package() -> None:
    package = _JCPackage("jc")
    package.__path__ = [str(COMP)]
    sys.modules.update({
        "jc": package,
        # The panel log: Nova writes to it in passing, so it does nothing here.
        "jc.websocket": _module("jc.websocket", nova_log=lambda *a, **k: None),
        # The real module loads the config entry chain to build prompts;
        # reasoning_loop only needs this one function.
        "jc.directive_helper": _module(
            "jc.directive_helper",
            build_system_prompt=lambda *a, **k: "You are Nova. Respond with JSON only."),
    })


if "jc" not in sys.modules:
    _install_jc_package()


# ── Keep tests out of the real /config ────────────────────────────────────────
# Nova's modules default their stores to hard-coded /config paths. For each
# unit test, every loaded module's /config path constants point into that
# test's own temporary directory, restored afterwards. Tests that set a path
# themselves still win, because their monkeypatch runs after this one.
_REDIRECT = {"patch": None, "root": None, "writes": None}


def _config_write_hook(event, args):
    writes = _REDIRECT["writes"]
    if writes is None or not args:
        return
    target = args[0]
    if not isinstance(target, (str, bytes, os.PathLike)):
        return
    path = os.fsdecode(target)
    if not (path == "/config" or path.startswith("/config/")):
        return
    if event == "open":
        mode = args[1] if len(args) > 1 else "r"
        flags = args[2] if len(args) > 2 else 0
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
            writes.append(path)
    elif event in ("sqlite3.connect", "os.mkdir", "os.rename", "os.replace", "os.remove"):
        writes.append(path)


sys.addaudithook(_config_write_hook)


def _config_targets(mod):
    """(attribute names, functions with /config defaults) for one module
    object, found once and cached on it."""
    cached = mod.__dict__.get("__nova_config_targets__")
    if cached is not None:
        return cached
    names = [n for n, v in vars(mod).items() if _is_config(v)]
    funcs = [v for v in vars(mod).values() if isinstance(v, types.FunctionType)]
    for cls in (v for v in vars(mod).values() if isinstance(v, type)):
        funcs += [v for v in vars(cls).values() if isinstance(v, types.FunctionType)]
    funcs = [fn for fn in funcs if getattr(fn, "__module__", None) == mod.__name__ and (
        (fn.__defaults__ and any(_is_config(d) for d in fn.__defaults__))
        or (fn.__kwdefaults__ and any(_is_config(d) for d in fn.__kwdefaults__.values())))]
    cached = (names, funcs)
    mod.__dict__["__nova_config_targets__"] = cached
    return cached


def _redirect_config_paths(mod) -> None:
    patch, root = _REDIRECT["patch"], _REDIRECT["root"]
    if patch is None:
        return
    names, funcs = _config_targets(mod)
    for name in names:
        value = mod.__dict__.get(name)
        if _is_config(value):
            patch.setattr(mod, name, _moved(value, root))
    # Defaults such as `db_path: str = DB_PATH` were fixed at definition time.
    for fn in funcs:
        if fn.__defaults__ and any(_is_config(d) for d in fn.__defaults__):
            patch.setattr(fn, "__defaults__",
                          tuple(_moved(d, root) if _is_config(d) else d for d in fn.__defaults__))
        if fn.__kwdefaults__ and any(_is_config(d) for d in fn.__kwdefaults__.values()):
            patch.setattr(fn, "__kwdefaults__",
                          {k: _moved(d, root) if _is_config(d) else d
                           for k, d in fn.__kwdefaults__.items()})


def _is_config(value) -> bool:
    if not isinstance(value, (str, pathlib.PurePath)):
        return False
    text = str(value)
    return text == "/config" or text.startswith("/config/")


def _moved(value, root: str):
    new = root + str(value)[len("/config"):]
    return type(value)(new) if isinstance(value, pathlib.PurePath) else new


class _RedirectingLoader:
    """Wraps a Nova module's loader so a module first imported during a test
    (for example lazily, from inside another module's function) gets the
    same /config redirect as one loaded through _load()."""

    def __init__(self, loader):
        self._loader = loader

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        _redirect_config_paths(module)

    def __getattr__(self, name):
        return getattr(self._loader, name)


class _RedirectingFinder:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if not fullname.startswith("jc.") or _REDIRECT["patch"] is None:
            return None
        from importlib.machinery import PathFinder
        spec = PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = _RedirectingLoader(spec.loader)
        return spec


sys.meta_path.insert(0, _RedirectingFinder)


def _load(modname: str):
    """Nova's `modname`, loaded once as `jc.<modname>` and shared by every
    test. Its relative imports then find the `jc` stand-ins and the same
    shared modules.

    A top level module is loaded as a plain module, not a package: as a
    package its relative imports would land under its own name and make
    second copies that a monkeypatch would miss."""
    key = "jc." + modname
    if "." in modname:
        # A package submodule (e.g. "automation.patterns"): import it through
        # the normal machinery so its parent package and its relative imports
        # resolve under the same synthetic package as everything else.
        mod = importlib.import_module(key)
        _redirect_config_paths(mod)
        return mod
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, COMP / f"{modname}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    mod = sys.modules[key]
    _redirect_config_paths(mod)
    # Keep the `jc` package's own attribute in sync with sys.modules. A plain
    # `from . import X` resolves via getattr(jc_pkg, "X") first and only
    # falls back to sys.modules if that's unset — so a real (non-_load())
    # import of X elsewhere in the session can permanently stick a stale
    # module onto the package object, which later shadows a fresh reload
    # here even after sys.modules.pop("jc.X") + a new _load("X").
    setattr(sys.modules["jc"], modname, mod)
    return mod


# ── 3. Fixtures ───────────────────────────────────────────────────────────

import pytest  # noqa: E402  (after the stand-ins on purpose)
from fakes import FakeHass, FakeProvider  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_config(request, tmp_path_factory):
    # Evaluation scenarios sandbox /config themselves; integration tests run
    # the real package under Home Assistant, whose own paths are outside
    # this redirect.
    if {"evaluation", "integration"} & set(pathlib.Path(str(request.fspath)).parts):
        yield
        return
    mp = pytest.MonkeyPatch()
    _REDIRECT["patch"] = mp
    _REDIRECT["root"] = str(tmp_path_factory.mktemp("config"))
    try:
        seen = set()
        loaded = [m for k, m in list(sys.modules.items()) if k.startswith("jc.") and m is not None]
        # A test may pop a module from sys.modules; the jc package can still
        # hold the old object, which `from . import x` then returns.
        pkg = sys.modules.get("jc")
        if pkg is not None:
            loaded += [v for v in vars(pkg).values() if isinstance(v, types.ModuleType)]
        for mod in loaded:
            if id(mod) not in seen:
                seen.add(id(mod))
                _redirect_config_paths(mod)
        _REDIRECT["writes"] = writes = []
        yield
        _REDIRECT["writes"] = None
        if writes:
            pytest.fail("test touched the real /config: " + ", ".join(sorted(set(writes))))
    finally:
        _REDIRECT["writes"] = None
        _REDIRECT["patch"] = None
        _REDIRECT["root"] = None
        mp.undo()


@pytest.fixture(autouse=True)
def _reset_package_monitor_holds():
    """package_monitor keeps its speech cooldown and trigger debounce stamps in
    module level dicts next to _STATE, and _load() caches the module across
    tests. Clear them around every test so one test's announcement cannot
    silence the next one's."""
    def _clear():
        for key in ("jc.package_monitor", "custom_components.nova.package_monitor"):
            mod = sys.modules.get(key)
            for name in ("_LAST_SPOKEN", "_TRIGGER_LAST"):
                held = getattr(mod, name, None) if mod is not None else None
                if isinstance(held, dict):
                    held.clear()
    _clear()
    yield
    _clear()


@pytest.fixture
def load():
    """Return the component-module loader (call as load('cognitive_core'))."""
    return _load


def route_delivery(load, monkeypatch, *, quiet=lambda h: False,
                   announcements=lambda h: True, asleep=lambda h: False,
                   notify_only=lambda h: False, pushed=None, spoken=None):
    """Point delivery.py (8.44.0) at a sender test's own switches: quiet
    hours, sleep, "Notifications only" and the Announcements switch come
    from the given functions
    (read at delivery time). Pushes are collected in `pushed` and speech in
    `spoken` instead of reaching a phone or a speaker."""
    dl = load("delivery")
    monkeypatch.setattr(dl, "conditions", lambda hass, req: dl.Conditions(
        quiet_hours=bool(quiet(hass)), announcements_on=bool(announcements(hass)),
        asleep=bool(asleep(hass)), notify_only=bool(notify_only(hass))))
    if pushed is not None:
        async def _push(hass, req, request_id):
            pushed.append(req.message)
            return ["phone"]
        monkeypatch.setattr(dl, "_push", _push)
    if spoken is not None:
        async def _speak(hass, req, message, targets, request_id):
            spoken.append(message)
            return True
        monkeypatch.setattr(dl, "_speak", _speak)
    return dl


_READABLE = {"lock": "unlocked", "cover": "open", "alarm_control_panel": "disarmed"}


def readable_devices(hass, *entity_ids):
    """Give each lock, cover or alarm a readable state that is not already
    secure, for tests written before the live check (8.46.0) that act on a
    device they never set. A device with no state is refused by that check."""
    for eid in entity_ids:
        domain = eid.split(".", 1)[0]
        if hass.states.get(eid) is None and domain in _READABLE:
            hass.states.set(eid, _READABLE[domain])


@pytest.fixture
def fake_hass():
    """A new FakeHass for each test. Not called `hass`, which is the real
    Home Assistant fixture the integration tests use."""
    hass = FakeHass()
    return hass


@pytest.fixture
def provider_factory():
    """FakeProvider itself, to build a model client with set replies."""
    factory = FakeProvider
    return factory


def _module_fixture(modname: str):
    """A fixture named after a Nova module that hands the test that module."""
    @pytest.fixture(name=modname)
    def _fixture():
        return _load(modname)
    return _fixture


cognitive_core = _module_fixture("cognitive_core")
reasoning_loop = _module_fixture("reasoning_loop")
connectivity = _module_fixture("connectivity")


def _install_nova_runtime(hass, runtime_config=None, *, entry_id="e1", **fields):
    """Give `hass` one LOADED Nova config entry that owns a NovaRuntime, the
    way async_setup_entry leaves it, and return that entry.

    Production code reads panel settings from entry.runtime_data (never the
    hass.data bridge), so this is how a unit test sets them. The runtime's
    runtime_config is `runtime_config` itself (not a copy), so a test can
    change it in place later, like a panel write."""
    rt = _load("runtime")
    cfg = sys.modules["homeassistant.config_entries"]
    kwargs = dict(
        client=object(), llm_provider_name="groq", sentinel=object(),
        reminder_watcher=object(), scheduler=object(), resources=object(),
        automation_contexts=object(),
        runtime_config={} if runtime_config is None else runtime_config,
    )
    kwargs.update(fields)
    entry = types.SimpleNamespace(
        entry_id=entry_id, state=cfg.ConfigEntryState.LOADED,
        options={}, data={}, runtime_data=rt.NovaRuntime(**kwargs))
    hass.config_entries = types.SimpleNamespace(
        async_entries=lambda domain=None: [entry])
    return entry


@pytest.fixture
def nova_runtime():
    """install(hass, runtime_config=None, **fields) -> entry. See
    _install_nova_runtime."""
    return _install_nova_runtime
