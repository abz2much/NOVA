"""Test harness for the Nova integration.

The linchpin is sequencing: the cores do `from homeassistant.core import ...`
at import time, so faithful stubs must be installed into sys.modules BEFORE any
test imports the integration. conftest.py is imported before collection, so the
module-level work here runs first.

The integration modules are loaded under a SYNTHETIC package `jc` whose __path__
points at the component directory — never the real `nova_component` package,
whose __init__.py would drag in the whole integration (config flow, setup, …).
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import os
import pathlib
import sys
import types

sys.path.insert(0, os.path.dirname(__file__))  # make fakes.py importable here


# ── 1. Install faithful-but-minimal Home Assistant stubs ──────────────────────
def _install_ha_stubs() -> None:
    if "homeassistant.core" in sys.modules:
        return

    core = types.ModuleType("homeassistant.core")

    class HomeAssistant:  # only a type reference for annotations
        pass

    class Event:
        def __init__(self, event_type="", data=None):
            self.event_type = event_type
            self.data = data or {}

    class State:
        def __init__(self, entity_id, state, attributes=None):
            self.entity_id = entity_id
            self.state = state
            self.attributes = attributes or {}

    class ServiceCall:  # type reference only (camera.py annotations)
        def __init__(self, domain="", service="", data=None):
            self.domain = domain
            self.service = service
            self.data = data or {}

    def callback(func):
        return func

    core.HomeAssistant = HomeAssistant
    core.Event = Event
    core.State = State
    core.ServiceCall = ServiceCall
    core.callback = callback

    dt = types.ModuleType("homeassistant.util.dt")
    dt.utcnow = lambda: _dt.datetime.now(_dt.timezone.utc)
    dt.now = lambda tz=None: _dt.datetime.now(tz)
    dt.as_local = lambda v: v
    dt.parse_datetime = lambda s: None
    util = types.ModuleType("homeassistant.util")
    util.dt = dt

    er = types.ModuleType("homeassistant.helpers.entity_registry")
    dr = types.ModuleType("homeassistant.helpers.device_registry")
    ar = types.ModuleType("homeassistant.helpers.area_registry")
    er.async_get = lambda hass: types.SimpleNamespace(
        entities={}, async_get=lambda eid: None)
    dr.async_get = lambda hass: types.SimpleNamespace(devices={})
    # audio_routing.py / residence_graph.py import this at module level; empty
    # registry is fine — tests that care about area resolution monkeypatch the
    # specific helper functions that call into it (e.g. _breach_area, _motion_key).
    ar.async_get = lambda hass: types.SimpleNamespace(
        async_get_area=lambda area_id: None, async_list_areas=lambda: [])
    ac = types.ModuleType("homeassistant.helpers.aiohttp_client")
    ac.async_get_clientsession = lambda hass: None
    net = types.ModuleType("homeassistant.helpers.network")
    net.get_url = lambda hass, **kw: "http://127.0.0.1:8123"
    # agent.py imports this at module level; without the stub, agent only
    # loads if a file that happens to stub it runs first (order-dependent
    # partial-module trap — the loader caches half-executed modules).
    llm_mod = types.ModuleType("homeassistant.helpers.llm")
    llm_mod.async_get_api = lambda *a, **k: None
    llm_mod.ToolInput = type("ToolInput", (), {
        "__init__": lambda self, tool_name="", tool_args=None:
            (setattr(self, "tool_name", tool_name),
             setattr(self, "tool_args", tool_args or {}), None)[-1]})
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.entity_registry = er
    helpers.device_registry = dr
    helpers.area_registry = ar
    helpers.aiohttp_client = ac
    helpers.network = net

    # Repairs: enough surface for repair_notices to import + be monkeypatched.
    ireg = types.ModuleType("homeassistant.helpers.issue_registry")
    ireg.async_create_issue = lambda *a, **k: None
    ireg.async_delete_issue = lambda *a, **k: None
    ireg.IssueSeverity = types.SimpleNamespace(
        ERROR="error", WARNING="warning", CRITICAL="critical")
    helpers.issue_registry = ireg

    cfg = types.ModuleType("homeassistant.config_entries")
    cfg.ConfigEntry = type("ConfigEntry", (), {})
    # runtime.current_runtime() compares an entry's state with LOADED.
    import enum as _enum
    cfg.ConfigEntryState = _enum.Enum("ConfigEntryState", {
        "LOADED": "loaded", "SETUP_IN_PROGRESS": "setup_in_progress",
        "SETUP_ERROR": "setup_error", "SETUP_RETRY": "setup_retry",
        "NOT_LOADED": "not_loaded", "FAILED_UNLOAD": "failed_unload",
        "UNLOAD_IN_PROGRESS": "unload_in_progress",
    })

    components = types.ModuleType("homeassistant.components")
    comp_camera = types.ModuleType("homeassistant.components.camera")

    async def _stub_get_image(hass, entity_id, timeout=10):
        return None
    comp_camera.async_get_image = _stub_get_image
    components.camera = comp_camera

    const = types.ModuleType("homeassistant.const")
    const.__getattr__ = lambda name: name  # any HA const → its own name

    ha = types.ModuleType("homeassistant")
    ha.core, ha.util, ha.helpers, ha.config_entries, ha.const = (
        core, util, helpers, cfg, const)

    for name, mod in {
        "homeassistant": ha,
        "homeassistant.core": core,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity_registry": er,
        "homeassistant.helpers.device_registry": dr,
        "homeassistant.helpers.area_registry": ar,
        "homeassistant.helpers.aiohttp_client": ac,
        "homeassistant.helpers.network": net,
        "homeassistant.helpers.llm": llm_mod,
        "homeassistant.helpers.issue_registry": ireg,
        "homeassistant.config_entries": cfg,
        "homeassistant.const": const,
        "homeassistant.components": components,
        "homeassistant.components.camera": comp_camera,
    }.items():
        sys.modules[name] = mod


_install_ha_stubs()


# ── 2. Synthetic package + stubs for the I/O siblings ─────────────────────────
ROOT = pathlib.Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components" / "nova"

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


if "jc" not in sys.modules:
    _pkg = _JCPackage("jc")
    _pkg.__path__ = [str(COMP)]
    sys.modules["jc"] = _pkg

    # websocket.nova_log is called lazily for logging — no-op it.
    _ws = types.ModuleType("jc.websocket")
    _ws.nova_log = lambda *a, **k: None
    sys.modules["jc.websocket"] = _ws

    # directive_helper pulls in const/config_entries to build prompts; stub the
    # one function reasoning_loop imports so we don't load that whole chain.
    _dh = types.ModuleType("jc.directive_helper")
    _dh.build_system_prompt = lambda *a, **k: "You are Nova. Respond with JSON only."
    sys.modules["jc.directive_helper"] = _dh


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
    """Import a component module under the synthetic `jc` package, so its
    relative imports (`from .websocket import …`, `from . import reasoning_cache`)
    resolve to our `jc.*` stubs and to single shared instances.

    Note: we deliberately do NOT pass submodule_search_locations. Doing so would
    make each module a *package* whose relative imports resolve under its own
    name (jc.reasoning_loop.reasoning_cache), creating duplicate module copies
    that defeat monkeypatching. As plain modules their __package__ is "jc", so
    `from . import X` resolves to jc.X via the jc package __path__."""
    key = f"jc.{modname}"
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


# ── 3. Fixtures ───────────────────────────────────────────────────────────────
import pytest  # noqa: E402
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


@pytest.fixture
def load():
    """Return the component-module loader (call as load('cognitive_core'))."""
    return _load


@pytest.fixture
def fake_hass():
    """A fresh fake Home Assistant core (named `fake_hass` to avoid colliding
    with pytest-homeassistant-custom-component's real `hass` fixture used in
    tests/integration/)."""
    return FakeHass()


@pytest.fixture
def provider_factory():
    return FakeProvider


@pytest.fixture
def cognitive_core():
    return _load("cognitive_core")


@pytest.fixture
def reasoning_loop():
    return _load("reasoning_loop")


@pytest.fixture
def connectivity():
    return _load("connectivity")


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
