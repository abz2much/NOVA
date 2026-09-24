"""Regression tests for the save-failure visibility fix (security review,
Finding 6): save()/set()/set_many() used to swallow a disk-write failure
entirely -- the in-memory cache had the new value, but nothing on disk did,
and nothing told the caller. A panel setting could appear to "stick" and
then silently revert on the next restart. save() now returns whether the
write actually succeeded, and set()/set_many() propagate that."""
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
    def _boom(self, target):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "replace", _boom)
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

    def _boom(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", _boom)
    assert jcfg.set_many_atomic({"model": "after"}) is False
    assert jcfg.get("model") == "before"


def test_set_many_atomic_refuses_credentials(jcfg):
    assert jcfg.set_many_atomic({"groq_api_key": "secret", "model": "after"}) is False
    assert jcfg.get("model") is None
