"""Saved mutes: entity mutes, category mutes and the blanket shush survive a
restart through output_mutes.json, loaded once at setup (never at import).
Critical alerts still bypass every mute, including a loaded blanket shush."""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib

import pytest

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


@pytest.fixture
def og(load, monkeypatch, tmp_path):
    mod = load("output_gate")
    path = tmp_path / "nova" / "output_mutes.json"
    monkeypatch.setattr(mod, "_mutes_path", lambda: str(path))
    mod._STATE.muted_entities.clear()
    mod._STATE.muted_categories.clear()
    mod._STATE.mute_all = False
    mod._STATE.history.clear()
    mod._STATE.recent_messages.clear()
    yield mod
    mod._STATE.muted_entities.clear()
    mod._STATE.muted_categories.clear()
    mod._STATE.mute_all = False


class _Hass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _path(og) -> pathlib.Path:
    return pathlib.Path(og._mutes_path())


def _forget(og):
    og._STATE.muted_entities.clear()
    og._STATE.muted_categories.clear()
    og._STATE.mute_all = False


async def test_round_trip(og):
    og.shush(entity_id="light.hall")
    og.shush(category="appliance")
    og.shush(all=True)
    await og.async_save_mutes(_Hass())
    assert json.loads(_path(og).read_text()) == {
        "entities": ["light.hall"], "categories": ["appliance"], "all": True}
    _forget(og)
    og.load_mutes()
    assert og._STATE.muted_entities == {"light.hall"}
    assert og._STATE.muted_categories == {"appliance"}
    assert og._STATE.mute_all is True


def test_missing_file_starts_empty(og):
    assert not _path(og).exists()
    og.load_mutes()
    assert og.status()["muted_entities"] == [] and og._STATE.mute_all is False


@pytest.mark.parametrize("content", [
    "", "   ", "{not json", "[]", "null", '"text"',
    '{"entities": "light.hall", "categories": 5, "all": "yes"}',
    '{"entities": [1, null, ""], "categories": [{}], "all": 1}',
])
def test_empty_or_corrupt_file_never_raises(og, content):
    _path(og).parent.mkdir(parents=True)
    _path(og).write_text(content)
    og._STATE.muted_entities.add("stale")
    og.load_mutes()          # must not raise
    # unreadable or non object content leaves the gate as it was; a bad
    # shaped object loads as no mutes
    assert og._STATE.mute_all is False
    assert og._STATE.muted_categories == set()
    assert "light.hall" not in og._STATE.muted_entities


async def test_shush_and_unshush_persist(og):
    hass = _Hass()
    og.shush(entity_id="light.hall")
    og.shush(category="security")
    await og.async_save_mutes(hass)
    og.unshush(entity_id="light.hall")
    await og.async_save_mutes(hass)
    assert json.loads(_path(og).read_text()) == {
        "entities": [], "categories": ["security"], "all": False}
    og.unshush()             # no arguments clears everything
    await og.async_save_mutes(hass)
    assert json.loads(_path(og).read_text()) == {
        "entities": [], "categories": [], "all": False}


async def test_mute_all_persists_and_clears(og):
    hass = _Hass()
    og.shush(all=True)
    await og.async_save_mutes(hass)
    assert json.loads(_path(og).read_text())["all"] is True
    og.unshush()
    await og.async_save_mutes(hass)
    assert json.loads(_path(og).read_text())["all"] is False


def test_critical_bypasses_a_loaded_blanket_mute(og):
    _path(og).parent.mkdir(parents=True)
    _path(og).write_text(json.dumps(
        {"entities": ["sensor.smoke"], "categories": ["safety"], "all": True}))
    og.load_mutes()
    assert og._STATE.mute_all is True
    ok, why = og.can_announce(entity_id="sensor.smoke", category="safety",
                              urgency="critical", message="smoke detected")
    assert ok is True and why == "critical bypass"
    ok, why = og.can_announce(entity_id="light.x", category="lights",
                              urgency="high", message="hello there")
    assert ok is False and "blanket" in why


def test_can_announce_order_unchanged(og):
    og._STATE.muted_entities.add("e1")
    og._STATE.muted_categories.add("c1")
    assert og.can_announce(entity_id="e1", category="x", urgency="low", message="m")[1] \
        == "entity e1 is muted"
    assert og.can_announce(entity_id="e2", category="c1", urgency="low", message="m")[1] \
        == "category c1 is muted"
    og._STATE.mute_all = True
    assert og.can_announce(entity_id="e1", category="c1", urgency="low", message="m")[1] \
        == "blanket shush active"


def test_status_reports_blanket_shush(og):
    assert og.status()["mute_all"] is False
    og.shush(all=True)
    assert og.status()["mute_all"] is True


async def test_save_failure_never_raises(og, monkeypatch):
    def boom(_data):
        raise OSError("disk full")
    monkeypatch.setattr(og, "_write_mutes", boom)
    og.shush(entity_id="light.hall")
    await og.async_save_mutes(_Hass())     # logged, not raised


async def test_concurrent_saves_leave_the_newest_state(og):
    class _Slow(_Hass):
        async def async_add_executor_job(self, func, *args):
            await asyncio.sleep(0)
            return func(*args)
    hass = _Slow()
    og.shush(entity_id="a")
    first = asyncio.ensure_future(og.async_save_mutes(hass))
    og.shush(entity_id="b")
    second = asyncio.ensure_future(og.async_save_mutes(hass))
    await asyncio.gather(first, second)
    assert json.loads(_path(og).read_text())["entities"] == ["a", "b"]


def test_import_does_not_read_the_file(load, monkeypatch, tmp_path):
    """Importing the module never touches the saved file; loading is an
    explicit call made at setup."""
    src = (COMP / "output_gate.py").read_text()
    tree = ast.parse(src)
    top_level_calls = [n for n in tree.body if isinstance(n, ast.Expr)]
    assert not any("load_mutes" in ast.unparse(n) for n in top_level_calls)
    assert "load_mutes()" not in src.split("def load_mutes")[0]


def test_load_happens_in_async_setup_after_paths_configure():
    tree = ast.parse((COMP / "__init__.py").read_text())
    setup = next(n for n in tree.body
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup")
    stmts = [ast.unparse(s) for s in setup.body]
    configure = stmts.index("paths.configure(hass)")
    load_idx = next(i for i, s in enumerate(stmts) if "output_gate.load_mutes" in s)
    assert load_idx > configure
    assert "async_add_executor_job" in stmts[load_idx]   # off the event loop


def test_services_save_after_every_shush_and_unshush():
    src = (COMP / "services.py").read_text()
    for fn, call in (("_shush", "output_gate.shush("), ("_unshush", "output_gate.unshush(")):
        body = src.split(f"async def {fn}(")[1].split("\n    async def ")[0].split("\n    _register(")[0]
        assert call in body
        assert body.index(call) < body.index("await output_gate.async_save_mutes(hass)")
