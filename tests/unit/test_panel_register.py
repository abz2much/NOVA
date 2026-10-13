"""Acceptance tests for panel_register, written from its behaviour spec.

Home Assistant's frontend, panel_custom and http modules are replaced with
recording fakes, the module is loaded fresh under the synthetic package, and
its frontend directory is pointed at a temporary folder where needed.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import sys
import types
from dataclasses import dataclass

import pytest

MOD_KEY = "jc.panel_register"
LOGGER_NAME = MOD_KEY


# ── Fakes ─────────────────────────────────────────────────────────────────


@dataclass
class FakeStaticPathConfig:
    url_path: str
    path: str
    cache_headers: bool = True


class Recorder:
    def __init__(self):
        self.calls = []          # ordered (kind, args, kwargs)
        self.remove_raises = None
        self.register_raises = None
        self.static_raises = None


class FakeHttp:
    def __init__(self, rec):
        self.rec = rec

    async def async_register_static_paths(self, configs):
        self.rec.calls.append(("static", (list(configs),), {}))
        if self.rec.static_raises:
            raise self.rec.static_raises


class FakeHass:
    def __init__(self, rec, http=True):
        self.rec = rec
        self.http = FakeHttp(rec) if http else None

    async def async_add_executor_job(self, func, *args):
        self.rec.calls.append(("executor", (func,) + args, {}))
        return func(*args)


@pytest.fixture
def env(load, monkeypatch):
    rec = Recorder()

    frontend = types.ModuleType("homeassistant.components.frontend")

    def async_remove_panel(hass, *args, **kwargs):
        rec.calls.append(("remove", (hass,) + args, kwargs))
        if rec.remove_raises:
            raise rec.remove_raises
    frontend.async_remove_panel = async_remove_panel

    panel_custom = types.ModuleType("homeassistant.components.panel_custom")

    async def async_register_panel(hass, *args, **kwargs):
        rec.calls.append(("register", (hass,) + args, kwargs))
        if rec.register_raises:
            raise rec.register_raises
    panel_custom.async_register_panel = async_register_panel

    http = types.ModuleType("homeassistant.components.http")
    http.StaticPathConfig = FakeStaticPathConfig

    for name, mod in (("frontend", frontend), ("panel_custom", panel_custom),
                      ("http", http)):
        monkeypatch.setitem(sys.modules, f"homeassistant.components.{name}", mod)
    monkeypatch.delitem(sys.modules, MOD_KEY, raising=False)
    mod = load("panel_register")
    # Bind fakes explicitly too, in case the module was cached elsewhere.
    monkeypatch.setattr(mod, "frontend", frontend)
    monkeypatch.setattr(mod, "panel_custom", panel_custom)
    monkeypatch.setattr(mod, "StaticPathConfig", FakeStaticPathConfig)
    yield types.SimpleNamespace(mod=mod, rec=rec, hass=FakeHass(rec), mp=monkeypatch)
    sys.modules.pop(MOD_KEY, None)


def run(coro):
    return asyncio.run(coro)


def make_frontend(env, tmp_path, *, js=b"console.log('panel');", with_dir=True):
    """Point the module at tmp_path/frontend and return that directory."""
    root = tmp_path / "pkg"
    root.mkdir()
    fdir = root / "frontend"
    if with_dir:
        fdir.mkdir()
        if js is not None:
            (fdir / "nova-panel.js").write_bytes(js)
    env.mp.setattr(env.mod, "__file__", str(root / "panel_register.py"))
    return fdir


def kinds(rec):
    return [c[0] for c in rec.calls]


def short_hash(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:10]


# ── Public interface ──────────────────────────────────────────────────────


def test_constants_have_exact_values(env):
    m = env.mod
    assert m.PANEL_URL_PATH == "nova"
    assert m.PANEL_TITLE == "Nova"
    assert m.PANEL_ICON == "mdi:robot-outline"
    assert m.PANEL_WEBCOMPONENT == "nova-panel"
    assert m.PANEL_STATIC_URL == "/nova_panel_static"
    assert m.PANEL_JS_FILENAME == "nova-panel.js"
    assert m.CMD_URL_PATH == "nova-command"
    assert m.CMD_TITLE == "Command Center"
    assert m.CMD_ICON == "mdi:hexagon-multiple-outline"
    assert m.CMD_WEBCOMPONENT == "nova-command"
    assert m.CMD_JS_FILENAME == "nova-command.js"


def test_function_shapes(env):
    m = env.mod
    assert inspect.iscoroutinefunction(m.async_register_panel)
    assert inspect.iscoroutinefunction(m._register_one)
    assert not inspect.iscoroutinefunction(m.async_unregister_panel)  # Q11
    assert not inspect.iscoroutinefunction(m._hash_file)
    assert list(inspect.signature(m.async_register_panel).parameters) == ["hass"]
    assert list(inspect.signature(m.async_unregister_panel).parameters) == ["hass"]
    sig = inspect.signature(m._register_one)
    params = sig.parameters
    assert list(params) == ["hass", "panel_dir", "webcomponent", "url_path",
                            "title", "icon", "js_filename", "config"]
    for name in ("webcomponent", "url_path", "title", "icon", "js_filename", "config"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["config"].default is None
    assert isinstance(m._LOGGER, logging.Logger) and m._LOGGER.name == LOGGER_NAME


def test_shipped_frontend_dir_contains_panel_js(env):
    fdir = os.path.join(os.path.dirname(env.mod.__file__), "frontend")
    assert os.path.isfile(os.path.join(fdir, "nova-panel.js"))
    assert not os.path.exists(os.path.join(fdir, "nova-command.js"))  # Q10


# ── _hash_file (rules 1-3) ───────────────────────────────────────────────


def test_hash_is_sha1_prefix_of_content(env, tmp_path):
    f = tmp_path / "a.js"
    f.write_bytes(b"abc")
    out = env.mod._hash_file(str(f))
    assert out == short_hash(b"abc")
    assert len(out) == 10 and out == out.lower()


def test_hash_falls_back_to_mtime_when_read_fails(env, tmp_path, monkeypatch):
    f = tmp_path / "a.js"
    f.write_bytes(b"abc")
    os.utime(f, (1_700_000_000.9, 1_700_000_000.9))
    import builtins
    real_open = builtins.open

    def failing_open(path, *a, **k):
        if os.fspath(path) == str(f):
            raise PermissionError("denied")
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", failing_open)
    assert env.mod._hash_file(str(f)) == "1700000000"


def test_hash_falls_back_to_current_time_when_file_missing(env, tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: 1234.99)
    assert env.mod._hash_file(str(tmp_path / "missing.js")) == "1234"


# ── async_register_panel: preconditions (rules 4-5) ──────────────────────


def test_missing_frontend_dir_returns_false_without_ha_calls(env, tmp_path, caplog):
    make_frontend(env, tmp_path, with_dir=False)
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert run(env.mod.async_register_panel(env.hass)) is False
    assert env.rec.calls == []
    assert any(r.levelno == logging.ERROR and "frontend dir" in r.getMessage()
               for r in caplog.records)


def test_missing_js_returns_false_without_ha_calls(env, tmp_path, caplog):
    make_frontend(env, tmp_path, js=None)
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert run(env.mod.async_register_panel(env.hass)) is False
    assert env.rec.calls == []
    assert any(r.levelno == logging.ERROR and "JS file not found" in r.getMessage()
               for r in caplog.records)


def test_js_path_that_is_a_directory_counts_as_missing(env, tmp_path):
    fdir = make_frontend(env, tmp_path, js=None)
    (fdir / "nova-panel.js").mkdir()
    assert run(env.mod.async_register_panel(env.hass)) is False
    assert env.rec.calls == []


# ── async_register_panel: success path (rules 6-9, 11-15) ────────────────


def test_success_path_calls_in_order_with_exact_arguments(env, tmp_path):
    content = b"export const x = 1;"
    fdir = make_frontend(env, tmp_path, js=content)
    hass = env.hass
    assert run(env.mod.async_register_panel(hass)) is True

    assert kinds(env.rec) == ["static", "remove", "executor", "remove", "register"]
    static, legacy, executor, preclear, register = env.rec.calls

    (configs,), kw = static[1], static[2]
    assert kw == {}
    assert configs == [FakeStaticPathConfig("/nova_panel_static", str(fdir), False)]

    assert legacy[1] == (hass, "nova-command") and legacy[2] == {}

    assert executor[1][0] is env.mod._hash_file
    assert executor[1][1:] == (os.path.join(str(fdir), "nova-panel.js"),)

    assert preclear[1] == (hass, "nova") and preclear[2] == {}

    assert register[1] == (hass,)
    assert register[2] == {
        "webcomponent_name": "nova-panel",
        "frontend_url_path": "nova",
        "sidebar_title": "Nova",
        "sidebar_icon": "mdi:robot-outline",
        "module_url": f"/nova_panel_static/nova-panel.js?v={short_hash(content)}",
        "embed_iframe": False,
        "require_admin": False,
        "config": None,
    }


def test_real_shipped_frontend_registers(env):
    """With the module's own frontend directory the call succeeds."""
    assert run(env.mod.async_register_panel(env.hass)) is True
    reg = [c for c in env.rec.calls if c[0] == "register"][0]
    real_js = os.path.join(os.path.dirname(env.mod.__file__), "frontend", "nova-panel.js")
    with open(real_js, "rb") as fh:
        expected = short_hash(fh.read())
    assert reg[2]["module_url"] == f"/nova_panel_static/nova-panel.js?v={expected}"


def test_success_logs_info(env, tmp_path, caplog):
    make_frontend(env, tmp_path)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        run(env.mod.async_register_panel(env.hass))
    assert any(r.levelno == logging.INFO and "/nova" in r.getMessage()
               for r in caplog.records)


def test_module_url_changes_with_js_content(env, tmp_path):
    fdir = make_frontend(env, tmp_path, js=b"one")
    run(env.mod.async_register_panel(env.hass))
    (fdir / "nova-panel.js").write_bytes(b"two")
    run(env.mod.async_register_panel(env.hass))
    urls = [c[2]["module_url"] for c in env.rec.calls if c[0] == "register"]
    assert urls == [f"/nova_panel_static/nova-panel.js?v={short_hash(b'one')}",
                    f"/nova_panel_static/nova-panel.js?v={short_hash(b'two')}"]


def test_reload_repeats_every_step_and_succeeds(env, tmp_path):
    """Q2/Q4: idempotency is remove-then-register; static path retried."""
    make_frontend(env, tmp_path)
    assert run(env.mod.async_register_panel(env.hass)) is True
    assert run(env.mod.async_register_panel(env.hass)) is True
    assert kinds(env.rec) == ["static", "remove", "executor", "remove", "register"] * 2


# ── Error containment (rules 6, 7, 13, 16-18) ────────────────────────────


@pytest.mark.parametrize("exc", [RuntimeError("dup route"), ValueError("x"), OSError("y")])
def test_static_path_failure_is_swallowed(env, tmp_path, caplog, exc):
    make_frontend(env, tmp_path)
    env.rec.static_raises = exc
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert run(env.mod.async_register_panel(env.hass)) is True
    assert kinds(env.rec) == ["static", "remove", "executor", "remove", "register"]
    assert any(r.levelno == logging.DEBUG and str(exc) in r.getMessage()
               for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records
                   if r.name == LOGGER_NAME)


def test_missing_http_component_is_tolerated(env, tmp_path):
    make_frontend(env, tmp_path)
    hass = FakeHass(env.rec, http=False)
    assert run(env.mod.async_register_panel(hass)) is True
    assert kinds(env.rec) == ["remove", "executor", "remove", "register"]


def test_remove_panel_failures_are_swallowed(env, tmp_path):
    make_frontend(env, tmp_path)
    env.rec.remove_raises = KeyError("nope")
    assert run(env.mod.async_register_panel(env.hass)) is True
    assert kinds(env.rec) == ["static", "remove", "executor", "remove", "register"]


def test_value_error_from_registration_counts_as_success(env, tmp_path, caplog):
    make_frontend(env, tmp_path)
    env.rec.register_raises = ValueError("Overwriting panel nova")
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert run(env.mod.async_register_panel(env.hass)) is True
    assert not any(r.levelno >= logging.WARNING for r in caplog.records
                   if r.name == LOGGER_NAME)


def test_other_registration_error_returns_false_and_logs_error(env, tmp_path, caplog):
    make_frontend(env, tmp_path)
    env.rec.register_raises = RuntimeError("boom")
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert run(env.mod.async_register_panel(env.hass)) is False
    errs = [r.getMessage() for r in caplog.records
            if r.name == LOGGER_NAME and r.levelno == logging.ERROR]
    assert errs and "boom" in errs[0] and "nova" in errs[0]


def test_base_exception_from_registration_propagates(env, tmp_path):
    make_frontend(env, tmp_path)
    env.rec.register_raises = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run(env.mod.async_register_panel(env.hass))


def test_hash_fallback_is_used_in_module_url(env, tmp_path, monkeypatch):
    """Q5: unreadable JS still registers, with an mtime-based version."""
    fdir = make_frontend(env, tmp_path)
    js = fdir / "nova-panel.js"
    os.utime(js, (1_600_000_000, 1_600_000_000))
    import builtins
    real_open = builtins.open

    def failing_open(path, *a, **k):
        if os.fspath(path) == str(js):
            raise PermissionError("denied")
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", failing_open)
    assert run(env.mod.async_register_panel(env.hass)) is True
    reg = [c for c in env.rec.calls if c[0] == "register"][0]
    assert reg[2]["module_url"] == "/nova_panel_static/nova-panel.js?v=1600000000"


# ── _register_one directly (rules 10-17) ─────────────────────────────────


def test_register_one_missing_file_returns_false(env, tmp_path, caplog):
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        ok = run(env.mod._register_one(
            env.hass, str(tmp_path), webcomponent="w-c", url_path="p",
            title="T", icon="mdi:x", js_filename="absent.js"))
    assert ok is False
    assert env.rec.calls == []
    assert any("JS file not found" in r.getMessage() for r in caplog.records)


def test_register_one_passes_config_and_uses_shared_static_prefix(env, tmp_path):
    (tmp_path / "other.js").write_bytes(b"z")
    cfg = {"k": 1}
    ok = run(env.mod._register_one(
        env.hass, str(tmp_path), webcomponent="w-c", url_path="p",
        title="T", icon="mdi:x", js_filename="other.js", config=cfg))
    assert ok is True
    assert kinds(env.rec) == ["executor", "remove", "register"]
    assert env.rec.calls[1][1] == (env.hass, "p")
    kw = env.rec.calls[2][2]
    assert kw["config"] is cfg
    assert kw["module_url"] == f"/nova_panel_static/other.js?v={short_hash(b'z')}"
    assert (kw["webcomponent_name"], kw["frontend_url_path"], kw["sidebar_title"],
            kw["sidebar_icon"]) == ("w-c", "p", "T", "mdi:x")
    assert kw["embed_iframe"] is False and kw["require_admin"] is False


# ── async_unregister_panel (rules 19-20) ─────────────────────────────────


def test_unregister_removes_main_panel_only(env, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert env.mod.async_unregister_panel(env.hass) is None
    assert env.rec.calls == [("remove", (env.hass, "nova"), {})]
    assert any(r.levelno == logging.INFO and "unregistered" in r.getMessage()
               for r in caplog.records)


def test_unregister_swallows_errors(env, caplog):
    env.rec.remove_raises = RuntimeError("gone")
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert env.mod.async_unregister_panel(env.hass) is None
    mine = [r for r in caplog.records if r.name == LOGGER_NAME]
    assert any(r.levelno == logging.DEBUG and "gone" in r.getMessage() for r in mine)
    assert not any(r.levelno >= logging.INFO for r in mine)


def test_unregister_then_register_again(env, tmp_path):
    make_frontend(env, tmp_path)
    env.mod.async_unregister_panel(env.hass)
    assert run(env.mod.async_register_panel(env.hass)) is True
    assert kinds(env.rec) == ["remove", "static", "remove", "executor", "remove", "register"]
