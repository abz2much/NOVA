"""A config.json Nova cannot use must never stop setup or be overwritten.

A hand edited file that fails to parse, or parses to something other than
an object, once broke setup and with it the panel. Such a file is renamed
to config.json.corrupt-<time>, defaults are used, and every accessor keeps
working. Since 8.49.39 a file that is not UTF-8 text is handled the same
way: before, it was read as empty but left in place, so the next panel save
replaced every setting in it with that one key.
"""
import json
from pathlib import Path

import pytest


@pytest.fixture
def cfg(load, tmp_path, monkeypatch):
    mod = load("nova_config")
    monkeypatch.setattr(mod, "CONFIG_PATH", Path(tmp_path) / "config.json")
    monkeypatch.setattr(mod, "_cache", {})
    monkeypatch.setattr(mod, "_loaded", False)
    monkeypatch.setattr(mod, "last_load_error", None)
    return mod


def _set_aside(mod):
    return sorted(mod.CONFIG_PATH.parent.glob("config.json.corrupt-*"))


def test_a_good_file_loads_with_no_error(cfg):
    cfg.CONFIG_PATH.write_text(json.dumps({"honorific": "madam", "n": {"x": 1}}))
    assert cfg.load() == {"honorific": "madam", "n": {"x": 1}}
    assert cfg.last_load_error is None
    assert _set_aside(cfg) == []


def test_no_file_means_defaults_and_no_error(cfg):
    assert cfg.load() == {}
    assert cfg.last_load_error is None
    assert cfg.get("missing", 7) == 7


@pytest.mark.parametrize("text, named", [
    ("[1, 2]", "list"),
    ('"plain words"', "str"),
    ("42", "int"),
])
def test_json_that_is_not_an_object_is_set_aside(cfg, text, named):
    cfg.CONFIG_PATH.write_text(text)
    assert cfg.load() == {}
    assert named in cfg.last_load_error
    assert not cfg.CONFIG_PATH.exists()
    assert len(_set_aside(cfg)) == 1
    assert cfg.get_all() == {}


def test_broken_json_is_set_aside_with_its_text_kept(cfg):
    original = '{"honorific": "madam", }'
    cfg.CONFIG_PATH.write_text(original)
    assert cfg.load() == {}
    assert cfg.last_load_error.startswith("invalid JSON")
    (kept,) = _set_aside(cfg)
    assert kept.read_text() == original
    assert kept.name in cfg.last_load_error


def test_a_file_that_is_not_utf8_is_set_aside_not_overwritten(cfg):
    # "señora" saved by an editor as Latin-1: the ñ is a lone 0xF1 byte.
    original = '{"honorific": "se\xf1ora", "panel_theme": "dark"}'.encode("latin-1")
    cfg.CONFIG_PATH.write_bytes(original)
    assert cfg.load() == {}
    assert cfg.last_load_error.startswith("invalid JSON")
    (kept,) = _set_aside(cfg)
    assert kept.read_bytes() == original
    # The next save writes a fresh file; the user's own file is untouched.
    assert cfg.set("announce_notify_only", True) is True
    assert json.loads(cfg.CONFIG_PATH.read_text()) == {"announce_notify_only": True}
    assert kept.read_bytes() == original


def test_utf8_text_beyond_ascii_reads_back(cfg):
    cfg.CONFIG_PATH.write_bytes(json.dumps({"honorific": "señora"},
                                           ensure_ascii=False).encode("utf-8"))
    assert cfg.load() == {"honorific": "señora"}
    assert cfg.last_load_error is None


def test_saving_after_a_bad_file_starts_a_clean_one(cfg):
    cfg.CONFIG_PATH.write_text("{nope")
    cfg.load()
    assert cfg.set("honorific", "madam") is True
    assert cfg.get("honorific") == "madam"
    assert json.loads(cfg.CONFIG_PATH.read_text()) == {"honorific": "madam"}


def test_a_replaced_cache_heals_itself(cfg, monkeypatch):
    cfg.CONFIG_PATH.write_text('{"a": 1}')
    cfg.load()
    monkeypatch.setattr(cfg, "_cache", ("not", "a", "dict"))
    assert cfg.get("a", "fallback") == "fallback"
    cfg.set("b", 2)
    assert cfg.get_all() == {"b": 2}
