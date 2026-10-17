"""Setting the banter level by voice: "full banter", "dry banter" or
"plain banter" is answered by the local engine. The level is saved where the
panel saves it (the live runtime config and config.json); a mode with its
own tone still wins until it ends, and the reply says so."""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass


class _Store:
    def __init__(self):
        self.cfg = {}
        self.rc = {}
        self.mode = "normal"
        self.forced = None
        self.save_ok = True


@pytest.fixture
def store(monkeypatch):
    st = _Store()

    def cfg_set(key, value):
        st.cfg[key] = value
        return st.save_ok

    monkeypatch.setitem(sys.modules, "jc.nova_config", types.SimpleNamespace(
        get=lambda key, default=None: st.cfg.get(key, default), set=cfg_set))
    monkeypatch.setitem(sys.modules, "jc.modes", types.SimpleNamespace(
        mode_banter_level=lambda: st.forced, active_mode=lambda: st.mode))
    monkeypatch.setitem(sys.modules, "jc.runtime", types.SimpleNamespace(
        domain_runtime_config=lambda hass: st.rc))
    return st


@pytest.fixture
def le(load, monkeypatch, store):
    mod = load("local_engine")
    monkeypatch.setattr(mod, "_read_aliases", lambda: {})
    return mod


@pytest.mark.parametrize("text, level, reply", [
    ("full banter", 2, "Full banter, sir. Do try to keep up."),
    ("Dry banter.", 1, "Dry banter, sir. As you were."),
    ("Nova, plain banter", 0, "Plain banter, sir. Facts only from here."),
    ("hey nova plain banter please", 0, "Plain banter, sir. Facts only from here."),
])
async def test_each_level_is_saved_and_reported(le, store, text, level, reply):
    store.cfg["banter_level"] = 1 if level != 1 else 2
    out = await le.try_local(FakeHass(), text, honorific="sir")
    assert out.text == reply and out.success
    assert store.cfg["banter_level"] == level
    assert store.rc["banter_level"] == level


async def test_no_honorific(le, store):
    out = await le.try_local(FakeHass(), "full banter", honorific="")
    assert out.text == "Full banter. Do try to keep up."


async def test_already_at_that_level(le, store):
    store.cfg["banter_level"] = 2
    out = await le.try_local(FakeHass(), "full banter", honorific="sir")
    assert out.text == "Already on full banter, sir."
    assert store.rc == {}


async def test_mode_forcing_the_level_is_reported_and_setting_still_saved(le, store):
    store.cfg["banter_level"] = 1
    store.mode, store.forced = "movie", 0
    out = await le.try_local(FakeHass(), "full banter", honorific="sir")
    assert out.text == "Saved, sir. Movie mode keeps me on plain banter until it ends."
    assert store.cfg["banter_level"] == 2 and store.rc["banter_level"] == 2


async def test_mode_forcing_the_same_level_is_a_normal_reply(le, store):
    store.cfg["banter_level"] = 1
    store.mode, store.forced = "party", 2
    out = await le.try_local(FakeHass(), "full banter", honorific="sir")
    assert out.text == "Full banter, sir. Do try to keep up."
    assert store.cfg["banter_level"] == 2


async def test_failed_save_is_said(le, store):
    store.save_ok = False
    out = await le.try_local(FakeHass(), "plain banter", honorific="sir")
    assert out.text.endswith("I couldn't save it, so it resets on restart.")


@pytest.mark.parametrize("text", [
    "banter", "full house", "less banter", "full banter in the kitchen",
    "what is full banter", "turn off full banter mode",
])
async def test_near_misses_do_not_change_anything(le, store, text):
    await le.try_local(FakeHass(), text, honorific="sir")
    assert "banter_level" not in store.cfg
