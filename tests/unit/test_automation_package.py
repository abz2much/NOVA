"""The automation capability package: boundaries and root compatibility.

Phase 5 moved Nova's automation code into custom_components/nova/automation/.
These tests pin the package's structure (who may import whom, what it may
touch) and prove the five root modules stay complete, identity-preserving,
write-through views of the package.
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest

import contract_extract as ce

PKG = ce.COMP / "automation"
NEW_MODULES = ("models", "inventory", "attribution", "matching", "suggestions",
               "patterns", "installation", "trials", "api", "_compat")

# Package-internal import layering: a module may import only the modules
# listed for it (function-level imports included).
ALLOWED_INTERNAL = {
    "_compat": set(),
    "models": set(),
    "matching": {"models"},
    "trials": set(),
    "inventory": {"models"},
    "attribution": {"models", "inventory"},
    "suggestions": {"models", "trials", "matching"},
    "patterns": {"models", "suggestions", "inventory", "matching"},
    "installation": {"models", "inventory", "matching", "suggestions",
                     "patterns", "trials"},
    "api": {"models", "suggestions"},
}
# Nova modules outside the package that the package may reach, always
# lazily inside a function (never at import time).
ALLOWED_OUTSIDE = {"action_log", "decision_record", "nova_config", "identity",
                   "knowledge", "person_patterns", "runtime", "websocket",
                   "cognitive"}   # Phase 8: pure routine scoring
FORBIDDEN_TIMERS = {"async_track_time_interval", "async_call_later",
                    "async_track_point_in_time", "async_create_background_task",
                    "async_track_time_change"}


def _tree(name: str) -> ast.Module:
    return ast.parse((PKG / f"{name}.py").read_text(encoding="utf-8"))


def _relative_imports(tree: ast.AST):
    """(level, module, names, top_level) for each relative import."""
    top = {id(n) for n in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            yield node.level, node.module or "", [a.name for a in node.names], id(node) in top


@pytest.mark.parametrize("name", NEW_MODULES)
def test_package_module_exists(name):
    assert (PKG / f"{name}.py").is_file()


def test_package_init_is_unchanged_and_home_assistant_free():
    tree = ast.parse((PKG / "__init__.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported == {"__future__", "mutex", "predictor"}, imported


@pytest.mark.parametrize("name", NEW_MODULES)
def test_import_layering(name):
    tree = _tree(name)
    for level, module, names, top_level in _relative_imports(tree):
        if level == 1:
            targets = {module} if module else set(names)
            assert targets <= ALLOWED_INTERNAL[name], (name, targets)
        else:
            assert level == 2, (name, level, module)
            target = module.split(".")[0] if module else None
            targets = {target} if target else set(names)
            assert targets <= ALLOWED_OUTSIDE, (name, targets)
            assert not top_level, f"{name} imports {targets} at import time"


@pytest.mark.parametrize("name", NEW_MODULES)
def test_no_module_level_home_assistant_import(name):
    tree = _tree(name)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""])
            assert not any(m.startswith("homeassistant") for m in mods), name


@pytest.mark.parametrize("name", NEW_MODULES)
def test_no_llm_timers_or_nova_hass_data(name):
    src = (PKG / f"{name}.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    called = {getattr(n.func, "attr", getattr(n.func, "id", ""))
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not called & FORBIDDEN_TIMERS, (name, called & FORBIDDEN_TIMERS)
    for word in ("llm_provider", "llm_client", ".client.", "async_chat",
                 "reasoning_loop", "hass.data[", "hass.data.setdefault"):
        assert word not in src, (name, word)
    if name == "inventory":
        assert src.count("hass.data.get(DATA_COMPONENT)") == 1
    else:
        assert "hass.data" not in src, name


def test_inventory_listens_only_for_automation_reloads():
    src = (PKG / "inventory.py").read_text(encoding="utf-8")
    assert src.count("async_listen(") == 1
    assert "EVENT_AUTOMATION_RELOADED, self._on_reloaded" in src
    assert "state_changed" not in src


def test_write_lock_is_the_only_module_level_lock():
    src = (PKG / "installation.py").read_text(encoding="utf-8")
    assert src.count("asyncio.Lock()") == 1
    assert "_WRITE_LOCK = asyncio.Lock()" in src


# ── Root compatibility modules ──────────────────────────────────────────────

FACADES = {
    "automation_creator": "automation.installation",
    "automation_inventory": None,
    "automation_matcher": "automation.matching",
    "automation_trials": "automation.trials",
    "pattern_analyzer": None,
}


@pytest.mark.parametrize("root", sorted(FACADES))
def test_root_names_are_the_package_objects(load, root):
    mod = load(root)
    assert mod.__all__, root
    for name in mod.__all__:
        owner = mod._compat_owners[name]
        assert getattr(mod, name) is getattr(owner, name), f"{root}.{name}"


def test_facade_writes_reach_the_package(load, monkeypatch):
    creator = load("automation_creator")
    installation = load("automation.installation")

    async def _fake(*a, **k):
        return {"success": False, "error": "fake"}

    monkeypatch.setattr(creator, "create_automation", _fake)
    assert installation.create_automation is _fake
    monkeypatch.undo()
    assert installation.create_automation is creator.create_automation
    assert installation.create_automation is not _fake


def test_live_thresholds_read_and_write_through(load):
    pa = load("pattern_analyzer")
    patterns = load("automation.patterns")
    before = patterns.CONFIDENCE_THRESHOLD
    try:
        patterns.set_thresholds(None, 0.61)
        assert pa.CONFIDENCE_THRESHOLD == 0.61
        pa.CONFIDENCE_THRESHOLD = 0.7
        assert patterns.CONFIDENCE_THRESHOLD == 0.7
        assert "CONFIDENCE_THRESHOLD" not in vars(pa)
    finally:
        patterns.CONFIDENCE_THRESHOLD = before


def test_trials_default_db_reads_through(load, monkeypatch):
    at = load("automation_trials")
    trials = load("automation.trials")
    monkeypatch.setattr(at, "_DEFAULT_DB", "/tmp/x.db")
    assert trials._DEFAULT_DB == "/tmp/x.db"


def test_analyzer_singleton_is_shared(load):
    assert load("pattern_analyzer").get_analyzer() is load(
        "automation.patterns").get_analyzer()
    assert load("pattern_analyzer")._ANALYZER is load("automation.patterns")._ANALYZER


def test_runtime_field_types_come_from_the_package(load):
    inv = load("automation_inventory")
    assert inv.AutomationInventory is load("automation.inventory").AutomationInventory
    assert inv.AutomationContextTracker is load(
        "automation.attribution").AutomationContextTracker


# ── Storage identity: owners moved, paths and tables did not ────────────────

def test_storage_paths_and_tables_are_unchanged():
    current = ce.storage_contract()
    paths = sorted({p for v in current.values() for p in v["paths"]})
    tables = sorted({t for v in current.values() for t in v["tables"]})
    pinned = json.loads((ce.FIXTURES / "storage.json").read_text(encoding="utf-8"))
    assert paths == sorted({p for v in pinned.values() for p in v["paths"]})
    assert tables == sorted({t for v in pinned.values() for t in v["tables"]})
    automation = {k: v for k, v in current.items()
                  if k.startswith("automation") or k == "pattern_analyzer"}
    assert automation == {
        "automation.installation": {"paths": ["automations.yaml"], "tables": []},
        "automation.predictor": {"paths": ["nova/habit_matrix.json"], "tables": []},
        "automation.suggestions": {"paths": ["nova/patterns.db"], "tables": []},
        "automation.trials": {"paths": ["nova/patterns.db"],
                              "tables": ["automation_trials"]},
    }
    assert "suggestions" in current["cognitive_core"]["tables"]


def test_no_root_module_was_deleted():
    for root in FACADES:
        assert (ce.COMP / f"{root}.py").is_file(), root
    assert pathlib.Path(ce.COMP / "person_patterns.py").is_file()


# ── Panel translation ───────────────────────────────────────────────────────

def test_panel_suggestion_items_shape(load):
    api = load("automation.api")
    row = {"id": 4, "created": "2026-09-01T10:00:00", "description": "d",
           "automation_yaml": "{}", "confidence": 0.8765, "pattern_count": 9,
           "pattern_type": "time_routine", "entity_ids": '["light.porch"]',
           "details": json.dumps({"hour": 18, "state": "on",
                                  "automation_match": {"status": "new"}})}
    (item,) = api.panel_suggestion_items([row])
    assert item == {
        "id": 4, "created": "2026-09-01T10:00:00", "description": "d",
        "yaml": "{}", "confidence": 0.88, "count": 9,
        "pattern_type": "time_routine", "entities": ["light.porch"],
        "why_headline": "A daily routine around 18:00",
        "evidence": ["Observed turning on near 18:00",
                     "Happened 9 times in the last 30 days"],
        "automation_match": {"status": "new"},
    }
    (bad,) = api.panel_suggestion_items([{"details": "not json",
                                          "entity_ids": "nope"}])
    assert bad["entities"] == [] and bad["automation_match"] == {}
