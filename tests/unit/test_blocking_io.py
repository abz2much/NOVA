"""Files read from async hot paths are read once, then served from memory.

reasoning_cache.load() and ha_secrets._read_secrets() used to go back to
disk on every call, which Home Assistant's blocking I/O detector flags in
the event loop. Both now keep what they read. These tests count the actual
disk reads rather than trusting the module flags.
"""
from __future__ import annotations

import pytest


# ── reasoning_cache ────────────────────────────────────────────────────────

@pytest.fixture
def rc(load, monkeypatch, tmp_path):
    """reasoning_cache with its file in tmp_path, its in-memory state
    restored after the test, and every disk read counted in ``rc_reads``."""
    mod = load("reasoning_cache")
    monkeypatch.setattr(mod, "CACHE_PATH", tmp_path / "reasoning_cache.json")
    monkeypatch.setattr(mod, "_cache", {})
    monkeypatch.setattr(mod, "_loaded", False)
    reads = []
    real_read = mod.read_json

    def counting_read(path):
        reads.append(path)
        return real_read(path)

    monkeypatch.setattr(mod, "read_json", counting_read)
    mod.rc_reads = reads
    yield mod
    del mod.rc_reads


def test_load_reads_the_file_on_first_use_only(rc):
    rc.CACHE_PATH.write_text('{"sig1": {"speak": true}}', encoding="utf-8")
    assert rc.load() == 1
    rc.CACHE_PATH.write_text('{"a": {}, "b": {}, "c": {}}', encoding="utf-8")
    assert rc.load() == 1                  # the new file is not read again
    assert rc._cache == {"sig1": {"speak": True}}
    assert len(rc.rc_reads) == 1


def test_a_loaded_cache_is_never_cleared_by_another_load(rc, monkeypatch):
    # Without the guard, load() would read the absent file and wipe this.
    monkeypatch.setattr(rc, "_cache", {"sig1": {"speak": True}})
    monkeypatch.setattr(rc, "_loaded", True)
    assert rc.load() == 1
    assert rc._cache == {"sig1": {"speak": True}}
    assert rc.rc_reads == []


def test_stats_leaves_a_loaded_cache_alone(rc, monkeypatch):
    monkeypatch.setattr(rc, "_cache", {"a": {"speak": False}, "b": {"speak": True}})
    monkeypatch.setattr(rc, "_loaded", True)
    assert rc.stats()["learned_patterns"] == 2
    assert set(rc._cache) == {"a", "b"}
    assert rc.rc_reads == []


def test_repeated_stats_calls_read_the_disk_once(rc):
    for _ in range(5):
        rc.stats()
    assert len(rc.rc_reads) == 1


# ── ha_secrets ─────────────────────────────────────────────────────────────

@pytest.fixture
def hs(load, monkeypatch, tmp_path):
    """ha_secrets pointed at tmp_path/secrets.yaml with a fresh cache, every
    parse of a file counted in ``hs_parses``."""
    mod = load("ha_secrets")
    monkeypatch.setattr(mod, "SECRETS_PATH", tmp_path / "secrets.yaml")
    mod._reset_secrets_cache()
    parses = []
    real_parse = mod._parse_file

    def counting_parse(path):
        parses.append(path)
        return real_parse(path)

    monkeypatch.setattr(mod, "_parse_file", counting_parse)
    mod.hs_parses = parses
    yield mod
    del mod.hs_parses
    mod._reset_secrets_cache()


def test_the_default_file_is_cached_until_forced(hs):
    hs.SECRETS_PATH.write_text("nova_api_key: test-key-1\n", encoding="utf-8")
    assert hs._read_secrets() == {"nova_api_key": "test-key-1"}

    hs.SECRETS_PATH.write_text("nova_api_key: test-key-2\n", encoding="utf-8")
    assert hs._read_secrets() == {"nova_api_key": "test-key-1"}   # from memory
    assert len(hs.hs_parses) == 1

    assert hs._read_secrets(force=True) == {"nova_api_key": "test-key-2"}
    assert len(hs.hs_parses) == 2


def test_many_lookups_parse_the_default_file_once(hs):
    hs.SECRETS_PATH.write_text("a: 1\nb: 2\n", encoding="utf-8")
    for key in ("a", "b", "a", "missing"):
        hs.get_secret_sync(key)
    assert len(hs.hs_parses) == 1


def test_an_explicit_path_is_read_every_time(hs, tmp_path):
    other = tmp_path / "other.yaml"
    other.write_text("k: v1\n", encoding="utf-8")
    first = hs._read_secrets(other)
    other.write_text("k: v2\n", encoding="utf-8")
    second = hs._read_secrets(other)
    assert (first, second) == ({"k": "v1"}, {"k": "v2"})
    assert hs._SECRETS_CACHE is None        # and it never fills the cache


def test_a_missing_default_file_is_cached_as_empty(hs):
    assert hs._read_secrets() == {}
    assert hs._read_secrets() == {}
    assert len(hs.hs_parses) == 1


def test_pointing_at_another_file_is_not_served_from_the_old_cache(hs, tmp_path, monkeypatch):
    hs.SECRETS_PATH.write_text("k: old\n", encoding="utf-8")
    assert hs._read_secrets() == {"k": "old"}
    moved = tmp_path / "moved.yaml"
    moved.write_text("k: new\n", encoding="utf-8")
    monkeypatch.setattr(hs, "SECRETS_PATH", moved)
    assert hs._read_secrets() == {"k": "new"}


def test_novas_own_write_clears_the_cache(hs):
    hs.SECRETS_PATH.write_text("k: before\n", encoding="utf-8")
    assert hs.get_secret_sync("k") == "before"
    assert hs.set_secret_sync("k", "after") is True
    assert hs.get_secret_sync("k") == "after"
