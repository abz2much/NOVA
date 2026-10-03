"""paths.py owns Home Assistant's config directory for every Nova store.

Pins that it follows hass.config.path(), and that no other module hard codes
a /config path again (the bug this module exists to remove)."""
from __future__ import annotations

import ast
import pathlib
import types

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


def _hass(root):
    return types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *p: str(pathlib.Path(root, *p))))


def test_paths_follow_hass_config_dir(load, monkeypatch, tmp_path):
    paths = load("paths")
    monkeypatch.setattr(paths, "_root", paths._root)
    paths.configure(_hass(tmp_path))
    assert paths.config_dir() == str(tmp_path)
    assert paths.nova_path("x.db") == str(tmp_path / "nova" / "x.db")
    assert paths.config_path("secrets.yaml") == str(tmp_path / "secrets.yaml")
    assert paths.patterns_db() == str(tmp_path / "nova" / "patterns.db")
    assert paths.conversations_db() == str(tmp_path / "nova" / "conversations.db")
    assert paths.nova_db() == str(tmp_path / "nova.db")
    assert paths.memory_dir() == str(tmp_path / "nova_memory")
    assert paths.learned_file() == str(tmp_path / ".nova_learned.json")


def test_stores_resolve_at_call_time(load, monkeypatch, tmp_path):
    """A module resolves its path when used, so configure() after import
    still moves it; an explicit override still wins."""
    paths = load("paths")
    goals = load("goals")
    monkeypatch.setattr(paths, "_root", paths._root)
    paths.configure(_hass(tmp_path))
    assert goals._db_path() == str(tmp_path / "nova" / "patterns.db")
    monkeypatch.setattr(goals, "DB_PATH", "/elsewhere/p.db")
    assert goals._db_path() == "/elsewhere/p.db"


def test_no_module_hard_codes_config_paths():
    offenders = []
    for p in sorted(COMP.rglob("*.py")):
        if p.name == "paths.py":
            continue
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                v = n.value
                if v == "/config" or v.startswith("/config/"):
                    offenders.append(f"{p.relative_to(COMP)}:{n.lineno} {v!r}")
    assert offenders == [], "use paths.py instead: " + ", ".join(offenders)
