"""Isolation for one evaluation scenario.

A Sandbox swaps Nova's I/O seams for in-memory stand-ins, resets the module
state a decision depends on, and records anything that tries to leave the
process: a network connection, a path under /config, an SQLite file, or a
file write outside the sandbox's own temporary directory. Everything is put
back on exit, whatever happened inside.

These are test-only adapters. None of them changes how Nova decides: the
policy, gate, resolver and reasoning code under evaluation runs unmodified.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
import types

# ── Process-wide audit hook (installed once, only records while active) ────

_ACTIVE: list = []   # the Sandbox currently recording, if any


def _audit(event, args):
    if not _ACTIVE:
        return
    box = _ACTIVE[-1]
    try:
        if event == "sqlite3.connect":
            target = str(args[0]) if args else ""
            if target != ":memory:" and not target.startswith("file::memory:"):
                box.violations.append(f"sqlite:{target}")
        elif event == "open" and args:
            path, mode = args[0], args[1] if len(args) > 1 else "r"
            flags = args[2] if len(args) > 2 else 0
            if not isinstance(path, (str, bytes, os.PathLike)):
                return
            path = os.fsdecode(path)
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
            if path.startswith("/config"):
                box.violations.append(f"config:{path}")
            elif writing and not path.startswith(box.tmp) and "__pycache__" not in path:
                box.violations.append(f"write:{path}")
        elif event in ("os.mkdir", "os.rename", "os.remove") and args:
            path = os.fsdecode(args[0]) if isinstance(args[0], (str, bytes, os.PathLike)) else ""
            if path and not path.startswith(box.tmp) and "__pycache__" not in path:
                box.violations.append(f"{event}:{path}")
    except Exception:   # an audit hook must never raise into the code under test
        pass


if not getattr(sys, "_nova_evaluation_audit", False):
    sys.addaudithook(_audit)
    sys._nova_evaluation_audit = True


class NetworkBlocked(ConnectionError):
    """Raised when evaluated code tries to open a network connection."""


class Clock:
    """A fixed, advanceable clock so time based gates are deterministic."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


# ── Stand-ins for Nova's I/O modules ────────────────────────────────────────

def _config_module(values: dict) -> types.ModuleType:
    mod = types.ModuleType("jc.nova_config")
    store = dict(values)
    mod.get = lambda key, default=None: store.get(key, default)
    mod.set = lambda key, value: store.__setitem__(key, value)
    mod.all = lambda: dict(store)
    mod.load = lambda *a, **k: dict(store)
    mod.configure = lambda *a, **k: None
    mod.last_load_error = None
    return mod


def _action_log_module(rec: dict) -> types.ModuleType:
    mod = types.ModuleType("jc.action_log")
    counter = {"n": 0}

    def _next():
        counter["n"] += 1
        return counter["n"]

    mod.new_request_id = lambda: f"eval-{counter['n'] + 1}"

    def start(request_id, action, source, **kw):
        rec["action_log"].append(("start", action, source))
        return _next()

    def start_many(request_id, action, source, targets, **kw):
        rec["action_log"].append(("start_many", action, source))
        return {t["key"]: _next() for t in targets}

    mod.start = start
    mod.start_many = start_many
    mod.mark_awaiting_approval = lambda *a, **k: True
    mod.set_approval = lambda aid, result, **k: rec["action_log"].append(("approval", result))
    mod.set_execution = lambda aid, outcome, **k: rec["action_log"].append(("execution", outcome))
    mod.configure = lambda *a, **k: None
    return mod


def _database_module(rec: dict) -> types.ModuleType:
    mod = types.ModuleType("jc.database")

    def save_activity(**kw):
        rec["activity"].append((kw.get("category"), kw.get("urgency"), kw.get("was_spoken", True)))

    mod.save_activity = save_activity
    mod.save_message = lambda *a, **k: None
    mod.save_sentinel_event = lambda *a, **k: None
    mod.get_recent_activity = lambda *a, **k: []
    mod.get_recent_messages = lambda *a, **k: []
    return mod


def _provider_activity_module(rec: dict) -> types.ModuleType:
    """Provider Activity samples are kept in memory: the activity boundary
    still runs and records, but nothing is written to disk."""
    mod = types.ModuleType("jc.provider_activity")
    mod.db_path_for = lambda hass: "evaluation-in-memory"

    def record(provider, model, role, location, data_category, success, *a, **k):
        rec["provider_activity"].append((provider, model, role, location, data_category, success))

    mod.record = record
    mod.list_days = lambda *a, **k: []
    return mod


class Sandbox:
    """Context manager: `with Sandbox(load, scenario) as box: ...`."""

    def __init__(self, load, *, config: dict | None = None,
                 confirmations: dict | None = None, satellites=None):
        self.load = load
        self.config = dict(config or {})
        self.confirmations = dict(confirmations or {})
        self.satellites = list(satellites or [])
        self.clock = Clock()
        self.records = {"action_log": [], "activity": [], "confirmations": [], "network": [],
                        "config_probes": [], "provider_activity": []}
        self.violations: list[str] = []
        self._undo: list = []
        self.tmp = ""

    # ── patch helpers ──
    def setattr(self, obj, name, value):
        missing = object()
        old = getattr(obj, name, missing)
        self._undo.append((obj, name, old, missing))
        setattr(obj, name, value)

    def setmodule(self, key, module):
        old = sys.modules.get(key)
        self._undo.append((sys.modules, key, old, None))
        sys.modules[key] = module

    def forget_module(self, key):
        """Drop `key` from sys.modules on exit if it isn't loaded yet, so a
        module first imported inside the sandbox (possibly binding a stand-in
        at import time) never leaks into later tests."""
        if key not in sys.modules:
            self._undo.append((sys.modules, key, None, None))
            if key.startswith("jc."):
                # the loader also sets it on the package; drop that too
                absent = object()
                self._undo.append((sys.modules["jc"], key[3:], absent, absent))

    def _restore(self):
        while self._undo:
            obj, name, old, missing = self._undo.pop()
            if obj is sys.modules:
                if old is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old
            elif old is missing:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
            else:
                setattr(obj, name, old)

    # ── lifecycle ──
    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="nova-eval-")
        self._threads_before = threading.active_count()
        try:
            self._install()
        except BaseException:
            self._restore()
            shutil.rmtree(self.tmp, ignore_errors=True)
            raise
        _ACTIVE.append(self)
        return self

    def __exit__(self, *exc):
        _ACTIVE.remove(self)
        self._restore()
        leftovers = sorted(os.listdir(self.tmp)) if os.path.isdir(self.tmp) else []
        shutil.rmtree(self.tmp, ignore_errors=True)
        if leftovers:
            self.violations.append(f"tmp-leftovers:{','.join(leftovers)}")
        if threading.active_count() > self._threads_before:
            self.violations.append("thread-leak")
        return False

    def _install(self):
        load = self.load
        rec = self.records
        # I/O modules first, so every function-local `from . import X` in
        # the evaluated code resolves to an in-memory stand-in.
        self.setmodule("jc.nova_config", _config_module(self.config))
        self.setmodule("jc.action_log", _action_log_module(rec))
        self.setmodule("jc.database", _database_module(rec))
        self.setmodule("jc.provider_activity", _provider_activity_module(rec))

        # No network, and no /config, for anything evaluated here.
        def _blocked(*a, **k):
            rec["network"].append(str(a[1:2] or a[:1]))
            self.violations.append("network")
            raise NetworkBlocked("network access is blocked during evaluation")

        self.setattr(socket.socket, "connect", _blocked)
        self.setattr(socket.socket, "connect_ex", _blocked)
        self.setattr(socket, "create_connection", _blocked)
        self.setattr(socket, "getaddrinfo", _blocked)
        real_exists, real_isfile = os.path.exists, os.path.isfile

        def _guard(fn):
            def wrapper(path, *a, **k):
                p = os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else ""
                if p.startswith("/config"):
                    # Answered "absent" without touching the filesystem. A
                    # few production paths probe hard coded /config files;
                    # the probe is recorded, and nothing real is read.
                    rec["config_probes"].append(p)
                    return False
                return fn(path, *a, **k)
            return wrapper

        self.setattr(os.path, "exists", _guard(real_exists))
        self.setattr(os.path, "isfile", _guard(real_isfile))

        async def _no_sleep(*_a, **_k):
            return None

        # Home Assistant registry helper stub → the scenario's own registry.
        er = sys.modules["homeassistant.helpers.entity_registry"]
        self.setattr(er, "async_get", lambda hass: getattr(hass, "registry", None)
                     or types.SimpleNamespace(entities={}, async_get=lambda eid: None))
        self.setattr(er, "async_entries_for_device",
                     lambda reg, device_id, include_disabled_entities=False:
                     reg.entries_for_device(device_id))

        og = load("output_gate")
        self.setattr(og, "_STATE", og.GateState())
        self.setattr(og, "_now", self.clock)
        self.setattr(og, "_BUDGET_CACHE", {"ts": 0.0, "mult": 1.0})

        cs = load("camera_semantic")
        cs.reset_dedup_state()
        self.setattr(cs, "_now", self.clock)

        conn = load("connectivity")
        conn.reset()

        rc = load("reasoning_cache")
        self.setattr(rc, "_cache", {})
        self.setattr(rc, "_loaded", True)
        self.setattr(rc, "save", lambda: None)
        self.setattr(rc, "CACHE_PATH", __import__("pathlib").Path(self.tmp) / "cache.json")

        lm = load("local_mind")
        self.setattr(lm, "_connect", lambda: None)   # no history database
        self.setattr(lm, "_recent_events", {})
        self.setattr(lm, "_hist_cache", {})
        self.setattr(lm, "_days_cache", (0.0, 0.0))
        self.setattr(lm, "_stats", {"decisions": 0, "spoke": 0, "silent": 0})

        ev = load("entity_verify")
        self.setattr(ev, "_SLEEP", _no_sleep)

        agent = load("agent")
        self.setattr(agent, "_VERIFY_SLEEP", _no_sleep)
        self.setattr(agent, "_LEARN_FILE", os.path.join(self.tmp, "absent", "learned.json"))

        le = load("local_engine")
        self.setattr(le, "_CTX", le._ConvCtx())
        self.setattr(le, "random", __import__("random").Random(0))

        load("cognitive_core")   # what's-open reads its lockdown exemptions

        # Confirmation: the policy and voice_confirm logic run for real; only
        # the human's answer is scripted, and every question asked is recorded.
        vc = load("voice_confirm")
        answers = self.confirmations

        async def _phone(hass, question, timeout=None):
            rec["confirmations"].append("phone")
            return answers.get("phone", "expired")

        async def _spoken(hass, satellite, question, timeout=None):
            rec["confirmations"].append("spoken")
            return answers.get("spoken", "expired")

        first_sat = self.satellites[0]["entity_id"] if self.satellites else None
        self.setattr(vc, "_confirm_via_notification", _phone)
        self.setattr(vc, "_confirm_native", _spoken)
        self.setattr(vc, "_confirm_gated", _spoken)
        self.setattr(vc, "_satellite_for_entity", lambda hass, eid: first_sat)
