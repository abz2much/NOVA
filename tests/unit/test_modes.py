"""Fresh behaviour tests for modes.py: Nova's operational modes (one active
mode, persisted, resolved against normal, never gating safety).

State lives in a per-test file; in-memory state is reset and restored by
monkeypatch. Configuration is supplied by replacing the module's _cfg."""
from __future__ import annotations

import json
import os
import time

import pytest


@pytest.fixture
def modes(load, tmp_path, monkeypatch):
    m = load("modes")
    monkeypatch.setattr(m, "MODE_STATE_PATH", str(tmp_path / "state" / "mode_state.json"))
    monkeypatch.setattr(m, "_state", {"mode": "normal", "since": 0.0, "reason": ""})
    monkeypatch.setattr(m, "_loaded", False)
    monkeypatch.setattr(m, "_cfg", lambda k, d=None: d)
    return m


def _config(modes, monkeypatch, **values):
    monkeypatch.setattr(modes, "_cfg", lambda k, d=None: values.get(k, d))


def _disk(modes):
    with open(modes.MODE_STATE_PATH) as f:
        return json.load(f)


BUILTIN = {
    "normal": dict(proactive=True, banter=None, announce_scope="notable",
                   auto_actions=True, quiet=False),
    "party": dict(proactive=False, banter=2, announce_scope="critical",
                  auto_actions=False, quiet=False),
    "lab": dict(proactive=False, banter=1, announce_scope="critical",
                auto_actions=False, quiet=True),
    "movie": dict(proactive=False, banter=0, announce_scope="critical",
                  auto_actions=False, quiet=True),
    "guest": dict(proactive=True, banter=1, announce_scope="notable",
                  auto_actions=False, quiet=False),
    "away": dict(proactive=False, banter=None, announce_scope="notable",
                 auto_actions=True, quiet=False),
    "focus": dict(proactive=False, banter=1, announce_scope="critical",
                  auto_actions=True, quiet=True),
}


# ── registry ───────────────────────────────────────────────────────────────

def test_default_mode_name(modes):
    assert modes.DEFAULT_MODE == "normal"


def test_builtin_names(modes):
    assert set(modes._BUILTIN_MODES) == set(BUILTIN)


@pytest.mark.parametrize("name", sorted(BUILTIN))
def test_builtin_profiles_resolve(modes, name):
    assert modes._resolve(name) == BUILTIN[name]


def test_builtin_descriptions(modes):
    d = {n: s["description"] for n, s in modes._BUILTIN_MODES.items()}
    assert d["normal"] == "Default operation — balanced proactivity and voice."
    assert d["movie"] == ("Movie/theater mode — near-silent, only critical alerts, "
                          "no convenience chatter.")
    assert d["focus"] == "Deep-focus/work — hold non-critical interrupts, terse tone."


def test_path_default(modes, load, monkeypatch):
    monkeypatch.setattr(modes, "MODE_STATE_PATH", None)
    assert modes._mode_state_path() == load("paths").nova_path("mode_state.json")


# ── _cfg ───────────────────────────────────────────────────────────────────

def test_cfg_reads_nova_config(load, monkeypatch):
    import sys
    import types
    m = load("modes")
    fake = types.ModuleType("jc.nova_config")
    fake.get = lambda k, d=None: {"x": 5, "nothing": None}.get(k, d)
    monkeypatch.setitem(sys.modules, "jc.nova_config", fake)
    assert m._cfg("x", 1) == 5
    assert m._cfg("nothing", 7) == 7
    assert m._cfg("missing") is None

    def boom(k, d=None):
        raise RuntimeError("x")
    fake.get = boom
    assert m._cfg("x", 3) == 3


# ── custom modes ───────────────────────────────────────────────────────────

def test_custom_mode_added(modes, monkeypatch):
    _config(modes, monkeypatch, custom_modes={
        " Cleaning ": {"description": "vacuum running", "proactive": False}})
    all_modes = modes._all_modes()
    assert all_modes["cleaning"] == {"description": "vacuum running", "proactive": False}
    assert modes._resolve("cleaning") == dict(BUILTIN["normal"], proactive=False)


def test_custom_mode_merges_over_builtin(modes, monkeypatch):
    _config(modes, monkeypatch, custom_modes={"PARTY": {"proactive": True}})
    party = modes._all_modes()["party"]
    assert party["proactive"] is True and party["banter"] == 2
    assert party["description"].startswith("Guests over")


def test_custom_normal_changes_every_fallback(modes, monkeypatch):
    _config(modes, monkeypatch, custom_modes={"normal": {"banter": 1, "quiet": True}})
    assert modes._resolve("away")["banter"] == 1
    assert modes._resolve("away")["quiet"] is False     # away states its own quiet
    assert modes._resolve("party")["banter"] == 2
    assert modes._resolve("normal")["quiet"] is True


@pytest.mark.parametrize("custom", [
    "not a dict", ["party"], {"bad": "spec"}, {"": {"proactive": False}},
    {"  ": {"proactive": False}}, None,
])
def test_bad_custom_modes_are_ignored(modes, monkeypatch, custom):
    _config(modes, monkeypatch, custom_modes=custom)
    assert set(modes._all_modes()) == set(BUILTIN)


def test_custom_mode_values_coerced_by_truthiness(modes, monkeypatch):
    _config(modes, monkeypatch, custom_modes={"odd": {
        "proactive": "false", "auto_actions": 0, "quiet": "yes", "banter": "2",
        "announce_scope": "all"}})
    assert modes._resolve("odd") == {"proactive": True, "banter": "2",
                                     "announce_scope": "all", "auto_actions": False,
                                     "quiet": True}


def test_resolve_unknown_name_is_normal(modes):
    assert modes._resolve("nonexistent") == BUILTIN["normal"]


def test_builtins_not_mutated_by_custom(modes, monkeypatch):
    _config(modes, monkeypatch, custom_modes={"party": {"proactive": True}})
    modes._all_modes()
    assert modes._BUILTIN_MODES["party"]["proactive"] is False


# ── set_mode / clear_mode ──────────────────────────────────────────────────

def test_set_mode_success_shape(modes):
    before = time.time()
    res = modes.set_mode("  Movie ", reason="film night")
    assert res == {"ok": True, "mode": "movie", "overrides": BUILTIN["movie"]}
    assert modes.active_mode() == "movie"
    assert modes._state["reason"] == "film night"
    assert modes._state["since"] >= before


def test_set_mode_persists(modes):
    modes.set_mode("lab", "soldering")
    disk = _disk(modes)
    assert disk["mode"] == "lab" and disk["reason"] == "soldering"
    assert isinstance(disk["since"], float)
    assert not os.path.exists(modes.MODE_STATE_PATH + ".tmp")


def test_set_unknown_mode(modes):
    res = modes.set_mode("Disco")
    assert res == {"ok": False, "error": "unknown mode 'Disco'",
                   "available": sorted(BUILTIN)}
    assert modes.active_mode() == "normal"
    assert not os.path.exists(modes.MODE_STATE_PATH)


@pytest.mark.parametrize("name", ["", None])
def test_set_empty_name(modes, name):
    res = modes.set_mode(name)
    assert res["ok"] is False and res["error"] == f"unknown mode '{name}'"


def test_set_mode_reason_defaults_empty(modes):
    modes.set_mode("party")
    assert modes._state["reason"] == ""
    modes.set_mode("party", None)
    assert modes._state["reason"] == ""


def test_set_same_mode_refreshes_since(modes):
    modes.set_mode("guest")
    modes._state["since"] = 1.0
    modes.set_mode("guest")
    assert modes._state["since"] > 1.0


def test_clear_mode(modes):
    modes.set_mode("party")
    res = modes.clear_mode()
    assert res["ok"] is True and res["mode"] == "normal"
    assert modes._state["reason"] == "cleared"
    modes.set_mode("party")
    modes.clear_mode("guests left")
    assert modes._state["reason"] == "guests left"


def test_persist_failure_still_switches(modes, tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(modes, "MODE_STATE_PATH", str(blocker / "mode_state.json"))
    res = modes.set_mode("party")
    assert res["ok"] is True and modes.active_mode() == "party"


# ── loading from disk ──────────────────────────────────────────────────────

def _write(modes, payload):
    os.makedirs(os.path.dirname(modes.MODE_STATE_PATH), exist_ok=True)
    with open(modes.MODE_STATE_PATH, "w") as f:
        f.write(payload if isinstance(payload, str) else json.dumps(payload))


def test_load_restores(modes):
    _write(modes, {"mode": "FOCUS", "since": 123.5, "reason": "deadline"})
    assert modes.active_mode() == "focus"
    assert modes._state == {"mode": "focus", "since": 123.5, "reason": "deadline"}


def test_load_defaults_for_missing_fields(modes):
    before = time.time()
    _write(modes, {"mode": "away"})
    modes._load()
    assert modes._state["reason"] == "restored"
    assert modes._state["since"] >= before


def test_load_ignores_unknown_mode(modes):
    _write(modes, {"mode": "disco", "since": 5, "reason": "x"})
    assert modes.active_mode() == "normal"
    assert modes._state == {"mode": "normal", "since": 0.0, "reason": ""}


def test_load_restores_a_custom_mode(modes, monkeypatch):
    _config(modes, monkeypatch, custom_modes={"cleaning": {"proactive": False}})
    _write(modes, {"mode": "cleaning", "since": 1, "reason": "r"})
    assert modes.active_mode() == "cleaning"


@pytest.mark.parametrize("payload", ["{broken", "[1, 2]", '"text"'])
def test_load_bad_file_keeps_default(modes, payload):
    _write(modes, payload)
    assert modes.active_mode() == "normal"
    assert modes._loaded is True


def test_load_bad_since_after_mode(modes):
    _write(modes, {"mode": "party", "since": "soon", "reason": "x"})
    assert modes.active_mode() == "party"
    assert modes._state["since"] == 0.0 and modes._state["reason"] == ""


def test_load_runs_once(modes):
    assert modes.active_mode() == "normal"
    _write(modes, {"mode": "party", "since": 1, "reason": ""})
    assert modes.active_mode() == "normal"
    modes._loaded = False
    assert modes.active_mode() == "party"


def test_set_mode_survives_a_restart(modes):
    modes.set_mode("lab", "soldering")
    modes._state = {"mode": "normal", "since": 0.0, "reason": ""}
    modes._loaded = False
    assert modes.active_mode() == "lab"
    assert modes.mode_info()["reason"] == "soldering"


# ── consulting the active mode ─────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(BUILTIN))
def test_hooks_follow_the_active_mode(modes, name):
    modes.set_mode(name)
    want = BUILTIN[name]
    assert modes.mode_overrides() == want
    assert modes.mode_allows_proactive() is want["proactive"]
    assert modes.mode_allows_auto_actions() is want["auto_actions"]
    assert modes.mode_banter_level() == want["banter"]


def test_active_mode_whose_definition_vanished(modes, monkeypatch):
    _config(modes, monkeypatch, custom_modes={"cleaning": {"proactive": False,
                                                           "description": "vac"}})
    modes.set_mode("cleaning")
    _config(modes, monkeypatch)
    assert modes.active_mode() == "cleaning"
    info = modes.mode_info()
    assert info["active"] == "cleaning"
    assert info["description"] == modes._BUILTIN_MODES["normal"]["description"]
    assert info["overrides"] == BUILTIN["normal"]


def test_mode_info_shape(modes, monkeypatch):
    _config(modes, monkeypatch, custom_modes={"cleaning": {"proactive": False}})
    modes.set_mode("party", "guests")
    info = modes.mode_info()
    assert set(info) == {"active", "since", "reason", "description", "overrides",
                         "available"}
    assert info["active"] == "party" and info["reason"] == "guests"
    assert info["description"] == modes._BUILTIN_MODES["party"]["description"]
    assert info["overrides"] == BUILTIN["party"]
    names = [a["name"] for a in info["available"]]
    assert names == sorted(names) and "cleaning" in names
    cleaning = next(a for a in info["available"] if a["name"] == "cleaning")
    assert cleaning == {"name": "cleaning", "description": ""}


# ── automatic mode ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (None, True), (True, True), (False, False), (0, False), ("false", True), (1, True),
])
def test_auto_enabled(modes, monkeypatch, value, expected):
    monkeypatch.setattr(modes, "_cfg",
                        lambda k, d=None: value if k == "operational_mode_auto" else d)
    assert modes.auto_enabled() is expected


def test_auto_enabled_by_default(modes):
    assert modes.auto_enabled() is True


def test_auto_goes_away_when_empty(modes):
    res = modes.auto_evaluate(False)
    assert res["ok"] is True and res["mode"] == "away"
    assert modes._state["reason"] == "auto: home empty"
    assert modes.auto_evaluate(False) is None       # already away


def test_auto_returns_to_normal(modes):
    modes.set_mode("away")
    res = modes.auto_evaluate(True)
    assert res["mode"] == "normal" and modes._state["reason"] == "auto: someone home"
    assert modes.auto_evaluate(True) is None


@pytest.mark.parametrize("name", ["party", "movie", "lab", "guest", "focus"])
def test_auto_leaves_discretionary_modes_while_home(modes, name):
    modes.set_mode(name)
    assert modes.auto_evaluate(True) is None
    assert modes.active_mode() == name


def test_auto_supersedes_discretionary_when_empty_and_lands_in_normal(modes):
    modes.set_mode("movie")
    assert modes.auto_evaluate(0)["mode"] == "away"
    assert modes.auto_evaluate(1)["mode"] == "normal"


def test_auto_off_never_switches(modes, monkeypatch):
    _config(modes, monkeypatch, operational_mode_auto=False)
    assert modes.auto_evaluate(False) is None
    modes.set_mode("away")
    assert modes.auto_evaluate(True) is None
    assert modes.active_mode() == "away"


def test_auto_never_raises(modes, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(modes, "set_mode", boom)
    assert modes.auto_evaluate(False) is None


def test_auto_loads_state_first(modes):
    _write(modes, {"mode": "away", "since": 1, "reason": ""})
    assert modes.auto_evaluate(False) is None
    assert modes.active_mode() == "away"


# ── room-scoped modes ──────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (["workshop", "garage"], ["workshop", "garage"]),
    (["workshop", "", None, 7], ["workshop", "7"]),
    ('["office", "shed"]', ["office", "shed"]),
    ("not json", []),
    ('{"a": 1}', []),
    (("workshop",), []),
    ([], []),
    (None, []),
    ("", []),
])
def test_lab_areas(modes, monkeypatch, value, expected):
    _config(modes, monkeypatch, lab_areas=value)
    modes.set_mode("lab")
    assert modes.mode_scoped_to_areas() == expected


@pytest.mark.parametrize("value,expected", [
    ("living_room", ["living_room"]), ("", []), (None, []), (5, ["5"]),
])
def test_movie_area(modes, monkeypatch, value, expected):
    _config(modes, monkeypatch, movie_area=value)
    modes.set_mode("movie")
    assert modes.mode_scoped_to_areas() == expected


@pytest.mark.parametrize("name", ["normal", "party", "guest", "away", "focus"])
def test_other_modes_are_whole_house(modes, monkeypatch, name):
    _config(modes, monkeypatch, lab_areas=["x"], movie_area="y")
    modes.set_mode(name)
    assert modes.mode_scoped_to_areas() == []


def test_scope_errors_are_whole_house(modes, monkeypatch):
    modes.set_mode("lab")

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(modes, "_cfg", boom)
    assert modes.mode_scoped_to_areas() == []
