"""Behaviour tests for the write-ahead state ledger, written from its spec.

The module is loaded on its own from its file (no package, no Home
Assistant), the same way the existing unit tests load it. Only the
default-path tests use the shared `load` fixture, because that path needs
the package-relative `paths` module.
"""
import ast
import importlib.util
import json
import logging
import os
import pathlib
import sys
import threading
import types

import pytest

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
SRC = COMP / "state_ledger.py"


def _standalone():
    spec = importlib.util.spec_from_file_location("nova_state_ledger_fresh", SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nova_state_ledger_fresh"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _standalone()


def _lines(path):
    return [json.loads(l) for l in pathlib.Path(path).read_text(encoding="utf-8").splitlines()
            if l.strip()]


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "ledger.jsonl")


@pytest.fixture
def led(path):
    return mod.StateLedger(path)


# ── Module surface ──────────────────────────────────────────────────────────

def test_constants():
    assert mod.MAX_RECORDS == 5000
    assert mod.DEFAULT_LEDGER_PATH is None


def test_constructor_keeps_path_and_default_limit(path):
    led = mod.StateLedger(path)
    assert led.path == path
    assert led.max_records == mod.MAX_RECORDS


def test_max_records_is_keyword_only(path):
    with pytest.raises(TypeError):
        mod.StateLedger(path, 10)
    assert mod.StateLedger(path, max_records=10).max_records == 10


def test_constructor_touches_no_files(tmp_path):
    target = tmp_path / "deep" / "dir" / "ledger.jsonl"
    mod.StateLedger(str(target))
    assert not (tmp_path / "deep").exists()


def test_module_has_no_top_level_relative_import():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports must be lazy"


def test_storage_literal_is_present_once():
    calls = [n for n in ast.walk(ast.parse(SRC.read_text(encoding="utf-8")))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "paths"]
    assert len(calls) == 1
    assert calls[0].func.attr == "nova_path"
    assert [a.value for a in calls[0].args] == ["state_ledger.jsonl"]
    assert not calls[0].keywords


def test_standalone_default_path_needs_the_package(monkeypatch):
    monkeypatch.setattr(mod, "DEFAULT_LEDGER_PATH", "/tmp/anything.jsonl")
    with pytest.raises(ImportError):
        mod.StateLedger()


# ── Default path (through the package loader) ───────────────────────────────

def _hass(root):
    return types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *p: str(pathlib.Path(root, *p))))


def test_default_path_follows_configured_config_dir(load, tmp_path, monkeypatch):
    paths = load("paths")
    sl = load("state_ledger")
    monkeypatch.setattr(paths, "_root", paths._root)
    monkeypatch.setattr(sl, "DEFAULT_LEDGER_PATH", None)
    paths.configure(_hass(tmp_path))
    assert sl.StateLedger().path == str(tmp_path / "nova" / "state_ledger.jsonl")
    assert sl.StateLedger("").path == str(tmp_path / "nova" / "state_ledger.jsonl")


def test_default_path_override_wins(load, tmp_path, monkeypatch):
    sl = load("state_ledger")
    override = str(tmp_path / "elsewhere.jsonl")
    monkeypatch.setattr(sl, "DEFAULT_LEDGER_PATH", override)
    assert sl.StateLedger().path == override
    assert sl.StateLedger(str(tmp_path / "explicit.jsonl")).path.endswith("explicit.jsonl")


def test_default_path_is_fixed_at_construction(load, tmp_path, monkeypatch):
    paths = load("paths")
    sl = load("state_ledger")
    monkeypatch.setattr(paths, "_root", paths._root)
    monkeypatch.setattr(sl, "DEFAULT_LEDGER_PATH", None)
    paths.configure(_hass(tmp_path / "one"))
    led = sl.StateLedger()
    paths.configure(_hass(tmp_path / "two"))
    assert led.path == str(tmp_path / "one" / "nova" / "state_ledger.jsonl")


# ── record_intent ───────────────────────────────────────────────────────────

def test_intent_record_shape(led, path):
    txn = led.record_intent("lock.example", "locked", action="lock.lock")
    (rec,) = _lines(path)
    assert set(rec) == {"txn", "op", "entity_id", "desired_state", "meta", "ts"}
    assert rec["txn"] == txn
    assert rec["op"] == "intent"
    assert rec["entity_id"] == "lock.example"
    assert rec["desired_state"] == "locked"
    assert rec["meta"] == {"action": "lock.lock"}
    assert isinstance(rec["ts"], float)


def test_meta_defaults_to_empty_and_keeps_value_types(led, path):
    led.record_intent("cover.one", "closed")
    led.record_intent("cover.two", "closed", attempt=2, flags=["a"], note=None)
    a, b = _lines(path)
    assert a["meta"] == {}
    assert b["meta"] == {"attempt": 2, "flags": ["a"], "note": None}


def test_desired_state_is_stringified(led, path):
    led.record_intent("number.level", 5)
    led.record_intent("switch.thing", True)
    assert [r["desired_state"] for r in _lines(path)] == ["5", "True"]


def test_txn_is_unique_string(led):
    txns = [led.record_intent("lock.example", "locked") for _ in range(50)]
    assert all(isinstance(t, str) for t in txns)
    assert len(set(txns)) == 50


def test_each_call_appends_one_line(led, path):
    led.record_intent("lock.a", "locked")
    led.record_intent("lock.b", "unlocked")
    text = pathlib.Path(path).read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert len(text.splitlines()) == 2


def test_creates_missing_parent_directories(tmp_path):
    target = tmp_path / "x" / "y" / "ledger.jsonl"
    led = mod.StateLedger(str(target))
    led.record_intent("lock.a", "locked")
    assert target.is_file()


def test_non_ascii_is_written_raw(led, path):
    led.record_intent("lock.a", "locked", label="Tür")
    raw = pathlib.Path(path).read_text(encoding="utf-8")
    assert "Tür" in raw and "\\u" not in raw


def test_intent_is_fsynced(led, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(mod.os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    led.record_intent("lock.a", "locked")
    assert synced


def test_unserialisable_meta_raises_and_writes_nothing(led, path):
    with pytest.raises(TypeError):
        led.record_intent("lock.a", "locked", obj=object())
    assert led.pending_intents() == []


def test_append_failure_is_logged_and_swallowed(tmp_path, caplog):
    led = mod.StateLedger(str(tmp_path))  # a directory cannot be appended to
    with caplog.at_level(logging.ERROR):
        txn = led.record_intent("lock.a", "locked")
    assert isinstance(txn, str) and txn
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ── mark_complete / pending_intents ─────────────────────────────────────────

def test_complete_record_shape(led, path):
    txn = led.record_intent("lock.a", "locked")
    led.mark_complete(txn)
    rec = _lines(path)[-1]
    assert set(rec) == {"txn", "op", "ts"}
    assert rec["txn"] == txn and rec["op"] == "complete"
    assert isinstance(rec["ts"], float)


def test_pending_lists_only_uncompleted_in_order(led):
    t1 = led.record_intent("lock.a", "locked")
    led.record_intent("cover.b", "closed")
    led.record_intent("lock.c", "locked")
    led.mark_complete(t1)
    pending = led.pending_intents()
    assert [p["entity_id"] for p in pending] == ["cover.b", "lock.c"]
    assert all(p["op"] == "intent" for p in pending)


def test_mark_complete_unknown_txn_is_harmless(led, path):
    led.record_intent("lock.a", "locked")
    led.mark_complete("no-such-txn")
    assert len(led.pending_intents()) == 1
    assert _lines(path)[-1]["op"] == "complete"


def test_completion_counts_wherever_it_appears(path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"txn": "t1", "op": "complete", "ts": 1.0}) + "\n")
        fh.write(json.dumps({"txn": "t1", "op": "intent", "entity_id": "lock.a",
                             "desired_state": "locked", "meta": {}, "ts": 2.0}) + "\n")
    assert mod.StateLedger(path).pending_intents() == []


def test_pending_ignores_other_ops_and_returns_duplicates(path):
    intent = {"txn": "t1", "op": "intent", "entity_id": "lock.a",
              "desired_state": "locked", "meta": {}, "ts": 1.0}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(intent) + "\n")
        fh.write(json.dumps({"txn": "t2", "op": "something_else"}) + "\n")
        fh.write(json.dumps(intent) + "\n")
    assert mod.StateLedger(path).pending_intents() == [intent, intent]


def test_pending_returns_records_as_stored(led):
    txn = led.record_intent("lock.a", "locked", action="lock.lock")
    (p,) = led.pending_intents()
    assert p["txn"] == txn and p["meta"] == {"action": "lock.lock"}


def test_missing_file_reads_empty_without_creating_it(path):
    led = mod.StateLedger(path)
    assert led.pending_intents() == []
    assert not os.path.exists(path)


def test_unreadable_path_warns_and_reads_empty(tmp_path, caplog):
    led = mod.StateLedger(str(tmp_path))
    with caplog.at_level(logging.WARNING):
        assert led.pending_intents() == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_blank_lines_are_skipped(led, path):
    led.record_intent("lock.a", "locked")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n   \n\t\n")
    led.record_intent("lock.b", "locked")
    assert len(led.pending_intents()) == 2


def test_corrupt_lines_anywhere_are_skipped(led, path):
    led.record_intent("lock.a", "locked")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    led.record_intent("lock.b", "locked")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"txn": "torn", "op": "int')  # torn final line, no newline
    assert [p["entity_id"] for p in led.pending_intents()] == ["lock.a", "lock.b"]


def test_state_survives_new_instances(path):
    first = mod.StateLedger(path)
    txn = first.record_intent("lock.a", "locked")
    first.record_intent("cover.b", "closed")
    second = mod.StateLedger(path)
    second.mark_complete(txn)
    third = mod.StateLedger(path)
    assert [p["entity_id"] for p in third.pending_intents()] == ["cover.b"]


# ── reconcile ───────────────────────────────────────────────────────────────

def test_reconcile_reports_only_mismatches(led):
    led.record_intent("lock.a", "locked")
    led.record_intent("cover.b", "closed")
    states = {"lock.a": "locked", "cover.b": "open"}
    (d,) = led.reconcile(states.get)
    assert d["entity_id"] == "cover.b"
    assert d["desired_state"] == "closed"
    assert d["actual"] == "open"
    assert d["op"] == "intent" and "txn" in d and "meta" in d


def test_reconcile_skips_completed(led):
    txn = led.record_intent("lock.a", "locked")
    led.mark_complete(txn)
    assert led.reconcile(lambda eid: "unlocked") == []


def test_reconcile_passes_entity_id_and_keeps_order(led):
    for eid in ("lock.a", "lock.b", "lock.c"):
        led.record_intent(eid, "locked")
    seen = []

    def verify(eid):
        seen.append(eid)
        return "unknown"

    result = led.reconcile(verify)
    assert seen == ["lock.a", "lock.b", "lock.c"]
    assert [d["entity_id"] for d in result] == seen


def test_reconcile_missing_device_is_a_mismatch(led):
    led.record_intent("lock.a", "locked")
    (d,) = led.reconcile(lambda eid: None)
    assert d["actual"] is None


def test_reconcile_verify_error_becomes_none(led):
    led.record_intent("lock.a", "locked")
    led.record_intent("lock.b", "locked")

    def verify(eid):
        if eid == "lock.a":
            raise ValueError("cannot read")
        return "locked"

    (d,) = led.reconcile(verify)
    assert d["entity_id"] == "lock.a" and d["actual"] is None


def test_reconcile_compares_as_strings(led):
    led.record_intent("number.level", 3)
    led.record_intent("sensor.flag", None)
    assert led.reconcile(lambda eid: 3 if eid == "number.level" else None) == []


def test_reconcile_keeps_raw_actual(led):
    led.record_intent("number.level", "3")
    (d,) = led.reconcile(lambda eid: 4)
    assert d["actual"] == 4


def test_reconcile_is_read_only_and_repeatable(led, path):
    led.record_intent("lock.a", "locked")
    before = pathlib.Path(path).read_bytes()
    first = led.reconcile(lambda eid: "unlocked")
    second = led.reconcile(lambda eid: "unlocked")
    assert first == second and len(first) == 1
    assert pathlib.Path(path).read_bytes() == before
    assert "actual" not in led.pending_intents()[0]


def test_reconcile_on_missing_file(path):
    calls = []
    assert mod.StateLedger(path).reconcile(lambda eid: calls.append(eid)) == []
    assert calls == []


# ── compact ─────────────────────────────────────────────────────────────────

def test_compact_keeps_pending_records_unchanged(led, path):
    done = led.record_intent("lock.a", "locked")
    led.record_intent("cover.b", "closed", action="cover.close_cover")
    led.record_intent("lock.c", "locked")
    led.mark_complete(done)
    led.mark_complete("orphan")
    before = led.pending_intents()
    led.compact()
    assert _lines(path) == before
    assert [r["entity_id"] for r in before] == ["cover.b", "lock.c"]


def test_compact_with_nothing_pending_empties_file(led, path):
    t = led.record_intent("lock.a", "locked")
    led.mark_complete(t)
    led.compact()
    assert pathlib.Path(path).read_text(encoding="utf-8") == ""


def test_compact_on_missing_file_creates_empty_one(tmp_path):
    target = tmp_path / "new" / "ledger.jsonl"
    mod.StateLedger(str(target)).compact()
    assert target.is_file() and target.read_text(encoding="utf-8") == ""


def test_compact_drops_corrupt_lines_and_leaves_no_tmp(led, path):
    led.record_intent("lock.a", "locked")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("garbage\n")
    led.compact()
    assert len(_lines(path)) == 1
    assert not os.path.exists(path + ".tmp")


def test_compact_replaces_atomically_via_tmp(led, path, monkeypatch):
    led.record_intent("lock.a", "locked")
    seen = []
    real = os.replace
    monkeypatch.setattr(mod.os, "replace", lambda a, b: (seen.append((a, b)), real(a, b)))
    led.compact()
    assert seen == [(path + ".tmp", path)]


def test_compact_failure_is_swallowed_and_keeps_file(led, path, monkeypatch, caplog):
    led.record_intent("lock.a", "locked")
    before = pathlib.Path(path).read_bytes()

    def boom(a, b):
        raise OSError("disk full")

    monkeypatch.setattr(mod.os, "replace", boom)
    with caplog.at_level(logging.ERROR):
        led.compact()
    assert pathlib.Path(path).read_bytes() == before
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_completion_after_compaction_still_clears(led):
    txn = led.record_intent("lock.a", "locked")
    led.compact()
    led.mark_complete(txn)
    assert led.pending_intents() == []


# ── Automatic compaction on growth ──────────────────────────────────────────

def test_growth_beyond_limit_compacts(path):
    led = mod.StateLedger(path, max_records=4)
    t1 = led.record_intent("lock.a", "locked")
    led.mark_complete(t1)
    t2 = led.record_intent("lock.b", "locked")
    led.mark_complete(t2)
    assert len(_lines(path)) == 4  # at the limit: not compacted
    led.record_intent("lock.c", "locked")  # 5 > 4 → compact
    recs = _lines(path)
    assert [r["entity_id"] for r in recs] == ["lock.c"]


def test_growth_compaction_never_loses_pending(path):
    led = mod.StateLedger(path, max_records=3)
    txns = [led.record_intent(f"lock.n{i}", "locked") for i in range(6)]
    assert [p["txn"] for p in led.pending_intents()] == txns


def test_mark_complete_does_not_compact(path):
    led = mod.StateLedger(path, max_records=1)
    t = led.record_intent("lock.a", "locked")
    led.mark_complete(t)
    led.mark_complete("orphan")
    assert len(_lines(path)) == 3


# ── Concurrency ─────────────────────────────────────────────────────────────

def test_concurrent_intents_all_land(path):
    led = mod.StateLedger(path)
    out = []
    lock = threading.Lock()

    def worker(n):
        for i in range(20):
            t = led.record_intent(f"lock.w{n}", "locked", i=i)
            with lock:
                out.append(t)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    recs = _lines(path)
    assert len(recs) == 160
    assert len(set(out)) == 160
    assert {r["txn"] for r in recs} == set(out)


def test_concurrent_records_and_compactions_keep_pending(path):
    led = mod.StateLedger(path, max_records=10)
    out = []
    lock = threading.Lock()

    def writer(n):
        for i in range(15):
            t = led.record_intent(f"cover.w{n}", "closed")
            with lock:
                out.append(t)

    def compactor():
        for _ in range(15):
            led.compact()

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    threads.append(threading.Thread(target=compactor))
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert {p["txn"] for p in led.pending_intents()} == set(out)


# ── Boot-style use: pending + compact as plain callables ────────────────────

def test_boot_flow_with_bound_methods(path):
    led = mod.StateLedger(path)
    ok = led.record_intent("lock.a", "locked")
    led.mark_complete(ok)
    led.record_intent("cover.b", "closed")
    fetch, tidy = led.pending_intents, led.compact  # what an executor job receives
    pending = fetch()
    assert all({"entity_id", "desired_state"} <= set(p) for p in pending)
    tidy()
    assert [r["entity_id"] for r in _lines(path)] == ["cover.b"]
