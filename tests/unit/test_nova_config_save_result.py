"""Regression tests for the save-failure visibility fix (security review,
Finding 6): save()/set()/set_many() used to swallow a disk-write failure
entirely -- the in-memory cache had the new value, but nothing on disk did,
and nothing told the caller. A panel setting could appear to "stick" and
then silently revert on the next restart. save() now returns whether the
write actually succeeded, and set()/set_many() propagate that."""
import os
import stat
from pathlib import Path

import pytest


@pytest.fixture
def jcfg(load, tmp_path, monkeypatch):
    j = load("nova_config")
    monkeypatch.setattr(j, "CONFIG_PATH", Path(tmp_path / "config.json"))
    monkeypatch.setattr(j, "_cache", {})
    monkeypatch.setattr(j, "_loaded", True)  # skip load() — cache is already "loaded"
    return j


def test_save_returns_true_on_success(jcfg):
    jcfg._cache["honorific"] = "sir"
    assert jcfg.save() is True
    assert jcfg.CONFIG_PATH.exists()


def test_save_returns_false_on_write_failure(jcfg, monkeypatch):
    """Inject a failure in the atomic-write step (e.g. disk full, permission
    error) and confirm it's now reported rather than only logged."""
    def _boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", _boom)
    jcfg._cache["honorific"] = "sir"
    assert jcfg.save() is False
    # The in-memory value is still there -- this session keeps working.
    assert jcfg._cache["honorific"] == "sir"


def test_set_propagates_save_result(jcfg, monkeypatch):
    monkeypatch.setattr(jcfg, "save", lambda: False)
    assert jcfg.set("honorific", "sir") is False
    assert jcfg._cache_dict()["honorific"] == "sir"  # applied in memory regardless


def test_set_returns_true_on_real_success(jcfg):
    assert jcfg.set("honorific", "ma'am") is True


def test_set_many_propagates_save_result(jcfg, monkeypatch):
    monkeypatch.setattr(jcfg, "save", lambda: False)
    assert jcfg.set_many({"a": 1, "b": 2}) is False
    assert jcfg._cache_dict()["a"] == 1


def test_set_many_atomic_updates_disk_and_cache_together(jcfg):
    jcfg._cache.update({"model": "before", "untouched": True})

    assert jcfg.set_many_atomic({"model": "after", "ollama_num_ctx": 16384}) is True

    assert jcfg.get("model") == "after"
    assert jcfg.get("untouched") is True
    saved = __import__("json").loads(jcfg.CONFIG_PATH.read_text())
    assert saved["model"] == "after"
    assert saved["ollama_num_ctx"] == 16384


def test_set_many_atomic_rolls_back_memory_when_replace_fails(jcfg, monkeypatch):
    jcfg._cache.update({"model": "before"})

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)
    assert jcfg.set_many_atomic({"model": "after"}) is False
    assert jcfg.get("model") == "before"


def test_set_many_atomic_refuses_credentials(jcfg):
    assert jcfg.set_many_atomic({"groq_api_key": "secret", "model": "after"}) is False
    assert jcfg.get("model") is None


# ── File permissions (v8.7.4) ────────────────────────────────────────────────
# config.json can hold credentials and personal details, so it is written
# owner-only, and a file older versions left readable is tightened on load.

def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_save_writes_owner_only(jcfg):
    jcfg._cache["honorific"] = "sir"
    assert jcfg.save() is True
    assert _mode(jcfg.CONFIG_PATH) == 0o600
    assert [x.name for x in jcfg.CONFIG_PATH.parent.iterdir()] == ["config.json"]


def test_save_tightens_a_readable_file(jcfg):
    jcfg.CONFIG_PATH.write_text("{}")
    os.chmod(jcfg.CONFIG_PATH, 0o644)
    assert jcfg.save() is True
    assert _mode(jcfg.CONFIG_PATH) == 0o600


def test_set_many_atomic_writes_owner_only(jcfg):
    assert jcfg.set_many_atomic({"model": "m"}) is True
    assert _mode(jcfg.CONFIG_PATH) == 0o600


def test_load_tightens_a_readable_file_and_keeps_its_content(jcfg):
    jcfg.CONFIG_PATH.write_text('{"honorific": "sir"}')
    os.chmod(jcfg.CONFIG_PATH, 0o644)
    assert jcfg.load() == {"honorific": "sir"}
    assert _mode(jcfg.CONFIG_PATH) == 0o600


def test_save_still_serialises_unknown_types_as_text(jcfg):
    jcfg._cache["when"] = Path("/x")
    assert jcfg.save() is True
    assert '"when": "/x"' in jcfg.CONFIG_PATH.read_text()
