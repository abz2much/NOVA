"""Fresh behaviour tests for reasoning_cache.py: the learned announce /
stay-quiet memory keyed by a coarse event signature.

Every test points the cache at its own temporary file and resets the
module-level state, restored afterwards by monkeypatch."""
from __future__ import annotations

import json
import time

import pytest


@pytest.fixture
def rc(load, tmp_path, monkeypatch):
    m = load("reasoning_cache")
    monkeypatch.setattr(m, "CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(m, "_cache", {})
    monkeypatch.setattr(m, "_loaded", False)
    monkeypatch.setattr(m, "_CLOUD_CALLS", 0)
    monkeypatch.setattr(m, "_CACHE_HITS", 0)
    return m


def _file(rc):
    return json.loads(rc.CACHE_PATH.read_text())


# ── constants & path ───────────────────────────────────────────────────────

def test_constants(rc):
    assert rc.REFRESH_AGE == 14 * 86400
    assert rc.MAX_ENTRIES == 2000


def test_path_override_and_default(rc, load, monkeypatch):
    assert rc._cache_path() == rc.CACHE_PATH
    monkeypatch.setattr(rc, "CACHE_PATH", None)
    paths = load("paths")
    assert str(rc._cache_path()) == paths.nova_path("reasoning_cache.json")


# ── state normalisation & signature ────────────────────────────────────────

@pytest.mark.parametrize("raw,norm", [
    (None, "?"), ("", "?"), ("  ", "?"), ("unknown", "?"), ("Unavailable", "?"),
    ("None", "?"), ("42", "#"), ("-3.5", "#"), (" 7 ", "#"), (12, "#"),
    ("1e3", "1e3"), ("+5", "+5"), (".5", ".5"), ("5.", "5."),
    (" Open ", "open"), ("not_home", "not_home"),
])
def test_norm_state(rc, raw, norm):
    assert rc._norm_state(raw) == norm


def test_signature_full_shape(rc):
    sig = rc.signature("binary_sensor", "motion", "presence", "off", "on", True, "low")
    assert sig == "binary_sensor|motion|presence|off->on|home=1|u=low"


def test_signature_blanks_and_numbers(rc):
    sig = rc.signature("", None, "", "21.5", "unavailable", 0)
    assert sig == "-|-|-|#->?|home=0|u=-"


def test_signature_does_not_normalise_domain_or_class(rc):
    assert rc.signature("Sensor", "Humidity", "Climate", "1", "2", "yes", "Medium") == \
        "Sensor|Humidity|Climate|#->#|home=1|u=Medium"


def test_signature_generalises_across_entities(rc):
    a = rc.signature("sensor", "humidity", "climate", "50", "71", False, "low")
    b = rc.signature("sensor", "humidity", "climate", "48.2", "49", False, "low")
    assert a == b


# ── load ───────────────────────────────────────────────────────────────────

def test_load_missing_file_is_empty(rc):
    assert rc.load() == 0
    assert rc._cache == {} and rc._loaded is True


def test_load_reads_a_dict(rc):
    rc.CACHE_PATH.write_text(json.dumps({"s1": {"speak": True}, "s2": {"speak": False}}))
    assert rc.load() == 2
    assert rc._cache["s1"] == {"speak": True}


def test_load_corrupt_or_non_dict_is_empty(rc, monkeypatch):
    rc.CACHE_PATH.write_text("{nope")
    assert rc.load() == 0
    monkeypatch.setattr(rc, "_loaded", False)
    rc.CACHE_PATH.write_text("[1, 2]")
    assert rc.load() == 0 and rc._cache == {}


def test_load_is_idempotent_once_loaded(rc):
    rc._cache = {"kept": {"speak": True}}
    rc._loaded = True
    rc.CACHE_PATH.write_text(json.dumps({"other": {}}))
    assert rc.load() == 1
    assert rc._cache == {"kept": {"speak": True}}


# ── save ───────────────────────────────────────────────────────────────────

def test_save_writes_the_whole_cache(rc):
    rc._cache = {"a": {"speak": True, "refreshed": 5}}
    rc.save()
    assert _file(rc) == {"a": {"speak": True, "refreshed": 5}}


def test_save_evicts_oldest_refreshed_above_the_cap(rc, monkeypatch):
    monkeypatch.setattr(rc, "MAX_ENTRIES", 3)
    rc._cache = {
        "old": {"refreshed": 1}, "none": {}, "mid": {"refreshed": 5},
        "new": {"refreshed": 9}, "newer": {"refreshed": 10},
    }
    rc.save()
    assert set(rc._cache) == {"mid", "new", "newer"}
    assert set(_file(rc)) == {"mid", "new", "newer"}


def test_save_at_the_cap_keeps_everything(rc, monkeypatch):
    monkeypatch.setattr(rc, "MAX_ENTRIES", 2)
    rc._cache = {"a": {"refreshed": 1}, "b": {"refreshed": 2}}
    rc.save()
    assert set(rc._cache) == {"a", "b"}


def test_save_never_raises(rc, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(rc, "CACHE_PATH", blocker / "sub" / "rc.json")
    rc._cache = {"a": {}}
    rc.save()          # parent is a file: write fails, swallowed


def test_save_does_not_load_first(rc):
    rc.CACHE_PATH.write_text(json.dumps({"on_disk": {"speak": True}}))
    rc._cache = {"in_memory": {"speak": False}}
    rc.save()
    assert _file(rc) == {"in_memory": {"speak": False}}


# ── get ────────────────────────────────────────────────────────────────────

def test_get_loads_on_first_use(rc):
    now = time.time()
    rc.CACHE_PATH.write_text(json.dumps({"s": {"speak": True, "refreshed": now}}))
    got = rc.get("s")
    assert got["speak"] is True and rc._loaded is True


def test_get_absent_or_empty_entry(rc):
    rc._loaded = True
    rc._cache = {"empty": {}}
    assert rc.get("missing") is None
    assert rc.get("empty") is None


def test_get_returns_a_copy(rc):
    rc._loaded = True
    rc._cache = {"s": {"speak": True, "refreshed": time.time()}}
    got = rc.get("s")
    got["speak"] = False
    assert rc._cache["s"]["speak"] is True


def test_get_stale_entries(rc):
    rc._loaded = True
    now = time.time()
    rc._cache = {
        "fresh": {"speak": True, "refreshed": now - rc.REFRESH_AGE + 60},
        "stale": {"speak": True, "refreshed": now - rc.REFRESH_AGE - 60},
        "never": {"speak": False},
    }
    assert rc.get("fresh") is not None
    assert rc.get("stale") is None
    assert rc.get("never") is None
    assert rc.get("stale", ignore_age=True)["speak"] is True
    assert rc.get("never", ignore_age=True) == {"speak": False}


# ── similar (case memory) ──────────────────────────────────────────────────

def test_similar_tallies_matching_domain_class_category_and_home(rc):
    rc._loaded = True
    rc._cache = {
        "binary_sensor|window|doors_windows|off->on|home=0|u=medium": {"speak": True},
        "binary_sensor|window|doors_windows|on->off|home=0|u=low": {"speak": False},
        "binary_sensor|window|doors_windows|off->?|home=0|u=-": {"speak": 1},
        "binary_sensor|window|doors_windows|off->on|home=1|u=medium": {"speak": True},
        "binary_sensor|door|doors_windows|off->on|home=0|u=medium": {"speak": True},
        "binary_sensor|window|security|off->on|home=0|u=medium": {"speak": False},
    }
    assert rc.similar("binary_sensor", "window", "doors_windows", False) == (2, 1)
    assert rc.similar("binary_sensor", "window", "doors_windows", True) == (1, 0)
    assert rc.similar("binary_sensor", "window", "other", False) == (0, 0)


def test_similar_blank_parts_and_ignores_age(rc):
    rc._loaded = True
    rc._cache = {"-|-|-|a->b|home=1|u=-": {"speak": False, "refreshed": 0}}
    assert rc.similar("", None, "", True) == (0, 1)


def test_similar_returns_a_tuple(rc):
    rc._loaded = True
    assert rc.similar("x", "y", "z", False) == (0, 0)
    assert isinstance(rc.similar("x", "y", "z", False), tuple)


# ── remember ───────────────────────────────────────────────────────────────

def test_remember_new_entry(rc):
    before = time.time()
    rc.remember("s", 1, "medium")
    e = rc._cache["s"]
    assert e["speak"] is True and e["urgency"] == "medium" and e["hits"] == 0
    assert e["created"] == e["refreshed"] and e["created"] >= before
    assert set(e) == {"speak", "urgency", "hits", "created", "refreshed"}
    assert _file(rc)["s"]["speak"] is True


def test_remember_refreshes_existing_entry(rc):
    rc._loaded = True
    rc._cache = {"s": {"speak": True, "urgency": "high", "hits": 4, "created": 10.0,
                       "refreshed": 10.0}}
    rc.remember("s", 0, "low")
    e = rc._cache["s"]
    assert e["speak"] is False and e["urgency"] == "low"
    assert e["hits"] == 4 and e["created"] == 10.0 and e["refreshed"] > 10.0


def test_remember_loads_before_writing(rc):
    rc.CACHE_PATH.write_text(json.dumps({"kept": {"speak": True, "refreshed": 1}}))
    rc.remember("new", False, "low")
    assert set(_file(rc)) == {"kept", "new"}


def test_remember_then_get(rc):
    rc.remember("s", True, "high")
    assert rc.get("s")["urgency"] == "high"


# ── counters & stats ───────────────────────────────────────────────────────

def test_note_hit_counts_and_bumps_entry(rc):
    rc._loaded = True
    rc._cache = {"s": {"speak": True}}
    rc.note_hit("s")
    rc.note_hit("s")
    rc.note_hit("absent")
    assert rc._cache["s"]["hits"] == 2
    assert "absent" not in rc._cache
    assert rc._CACHE_HITS == 3


def test_note_hit_does_not_save(rc):
    rc._loaded = True
    rc._cache = {"s": {"speak": True}}
    rc.note_hit("s")
    assert not rc.CACHE_PATH.exists()


def test_note_cloud_call(rc):
    rc.note_cloud_call()
    rc.note_cloud_call()
    assert rc._CLOUD_CALLS == 2


def test_stats_with_no_traffic(rc):
    assert rc.stats() == {"learned_patterns": 0, "cloud_calls": 0,
                          "local_decisions": 0, "local_rate": 0}


def test_stats_rates(rc):
    rc._loaded = True
    rc._cache = {"a": {}, "b": {}}
    for _ in range(3):
        rc.note_cloud_call()
    rc.note_hit("a")
    assert rc.stats() == {"learned_patterns": 2, "cloud_calls": 3,
                          "local_decisions": 1, "local_rate": 25}


def test_stats_rounds_half_to_even(rc):
    rc._loaded = True
    rc._CACHE_HITS = 1
    rc._CLOUD_CALLS = 7          # 12.5 % → 12
    assert rc.stats()["local_rate"] == 12


def test_stats_loads_without_clobbering(rc):
    rc._cache = {"a": {}}
    rc._loaded = True
    rc.stats()
    assert rc._cache == {"a": {}}


# ── reset ──────────────────────────────────────────────────────────────────

def test_reset_clears_memory_counters_and_file(rc):
    rc.remember("s", True, "low")
    rc.note_cloud_call()
    rc.note_hit("s")
    rc.reset()
    assert rc._cache == {} and rc._CLOUD_CALLS == 0 and rc._CACHE_HITS == 0
    assert _file(rc) == {}
    assert rc._loaded is True


# ── module attributes are read at call time ────────────────────────────────

def test_replaced_cache_object_is_used(rc):
    rc._loaded = True
    rc._cache = {"x|y|z|a->b|home=0|u=-": {"speak": True, "refreshed": time.time()}}
    assert rc.get("x|y|z|a->b|home=0|u=-")["speak"] is True
    assert rc.similar("x", "y", "z", False) == (1, 0)


def test_remember_calls_the_module_save(rc, monkeypatch):
    saved = []
    monkeypatch.setattr(rc, "save", lambda: saved.append(True))
    rc.remember("s", True, "low")
    assert saved == [True]
    assert not rc.CACHE_PATH.exists()
