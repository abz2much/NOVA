"""Fresh behaviour tests for diagnostics/fault_log.py, written from the spec.

The module is loaded on its own (no parent package) exactly as the spec
requires, plus once through the synthetic package to check default-path
resolution."""
import importlib
import importlib.util
import json
import logging
import os
import pathlib
import sys

import pytest

SRC = (pathlib.Path(__file__).resolve().parents[2]
       / "custom_components" / "nova" / "diagnostics" / "fault_log.py")


def _standalone():
    spec = importlib.util.spec_from_file_location("fresh_fault_log", SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fresh_fault_log"] = mod
    spec.loader.exec_module(mod)
    return mod


FL = _standalone()


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "ledger" / "faults.json"


@pytest.fixture
def ledger(ledger_path):
    return FL.FaultLog(path=str(ledger_path))


def _read(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


# ── interface and loading ─────────────────────────────────────────────────────

def test_constants_and_standalone_load():
    assert FL.MAX_ENTRIES == 1000
    assert FL.DEFAULT_FAULT_LOG_PATH is None
    assert callable(FL._default_fault_log_path)


def test_constructor_keeps_path_and_cap_without_touching_disk(tmp_path):
    target = tmp_path / "never" / "made.json"
    log = FL.FaultLog(path=str(target), max_entries=7)
    assert log.path == str(target)
    assert log.max_entries == 7
    assert not target.parent.exists()


def test_default_cap_is_max_entries(ledger):
    assert ledger.max_entries == 1000


def test_override_wins_for_default_path(load, monkeypatch, tmp_path):
    mod = load("diagnostics.fault_log")
    monkeypatch.setattr(mod, "DEFAULT_FAULT_LOG_PATH", str(tmp_path / "o.json"))
    assert mod.FaultLog().path == str(tmp_path / "o.json")
    assert mod._default_fault_log_path() == str(tmp_path / "o.json")


def test_standalone_default_path_needs_the_package(monkeypatch, tmp_path):
    # paths is imported before the override is consulted
    monkeypatch.setattr(FL, "DEFAULT_FAULT_LOG_PATH", str(tmp_path / "o.json"))
    with pytest.raises(ImportError):
        FL.FaultLog()


def test_default_path_resolves_through_paths(load, monkeypatch, tmp_path):
    mod = load("diagnostics.fault_log")
    paths = load("paths")
    monkeypatch.setattr(mod, "DEFAULT_FAULT_LOG_PATH", None)
    monkeypatch.setattr(paths, "_root", str(tmp_path))
    assert mod.FaultLog().path == os.path.join(str(tmp_path), "nova", "fault_history.json")
    assert mod.FaultLog(path="").path.endswith(os.path.join("nova", "fault_history.json"))


def test_package_reexports_the_class(load):
    mod = load("diagnostics.fault_log")
    pkg = importlib.import_module("jc.diagnostics")
    assert pkg.FaultLog is mod.FaultLog
    assert "FaultLog" in pkg.__all__


# ── commit_event ──────────────────────────────────────────────────────────────

def test_commit_returns_entry_with_text_lowercased_tags_and_float_ts(ledger, monkeypatch):
    monkeypatch.setattr(FL.time, "time", lambda: 1234.5)
    entry = ledger.commit_event("Disk Nearly FULL", ["Server Room", "NAS"])
    assert entry == {"text": "Disk Nearly FULL", "tags": ["server room", "nas"], "ts": 1234.5}


def test_commit_stringifies_text_and_tags(ledger):
    entry = ledger.commit_event(404, tags=[7, None, "  Mixed  "])
    assert entry["text"] == "404"
    assert entry["tags"] == ["7", "none", "  mixed  "]   # not stripped
    assert isinstance(entry["ts"], float)


def test_commit_without_tags_stores_empty_list(ledger, ledger_path):
    ledger.commit_event("switch offline")
    ledger.commit_event("switch offline again", tags=None)
    assert [e["tags"] for e in _read(ledger_path)] == [[], []]


def test_commit_creates_parent_dir_and_appends_in_order(ledger, ledger_path):
    for word in ("first", "second", "third"):
        ledger.commit_event(word, ["t"])
    assert [e["text"] for e in _read(ledger_path)] == ["first", "second", "third"]
    assert not pathlib.Path(f"{ledger_path}.tmp").exists()


def test_file_is_written_as_utf8_without_ascii_escapes(ledger, ledger_path):
    ledger.commit_event("Température élevée — 45°C", ["chauffe-eau"])
    raw = ledger_path.read_text(encoding="utf-8")
    assert "Température élevée — 45°C" in raw
    assert "\\u" not in raw


def test_cap_keeps_only_the_newest(tmp_path):
    log = FL.FaultLog(path=str(tmp_path / "c.json"), max_entries=3)
    for i in range(8):
        log.commit_event(f"fault {i}")
    assert [e["text"] for e in _read(tmp_path / "c.json")] == ["fault 5", "fault 6", "fault 7"]


def test_cap_of_zero_never_trims(tmp_path):
    log = FL.FaultLog(path=str(tmp_path / "z.json"), max_entries=0)
    for i in range(4):
        log.commit_event(f"f{i}")
    assert len(_read(tmp_path / "z.json")) == 4


def test_save_failure_logs_error_and_still_returns_entry(tmp_path, caplog):
    blocker = tmp_path / "plainfile"
    blocker.write_text("x")
    log = FL.FaultLog(path=str(blocker / "sub" / "f.json"))
    with caplog.at_level(logging.ERROR):
        entry = log.commit_event("cannot persist", ["io"])
    assert entry["text"] == "cannot persist"
    assert any("Failed to persist fault log" in r.getMessage() and r.levelno == logging.ERROR
               for r in caplog.records)


# ── loading tolerance ─────────────────────────────────────────────────────────

def test_missing_file_reads_empty_and_logs_nothing(ledger, caplog):
    with caplog.at_level(logging.DEBUG):
        assert ledger.recent() == []
        assert ledger.query_related_faults(["anything"]) == []
    assert not caplog.records


def test_invalid_json_warns_and_is_overwritten(ledger_path, caplog):
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text("[{broken", encoding="utf-8")
    log = FL.FaultLog(path=str(ledger_path))
    with caplog.at_level(logging.WARNING):
        assert log.recent() == []
    assert any("fault log unreadable" in r.getMessage() and "starting fresh" in r.getMessage()
               and r.levelno == logging.WARNING for r in caplog.records)
    log.commit_event("fresh start", ["x"])
    assert [e["text"] for e in _read(ledger_path)] == ["fresh start"]


@pytest.mark.parametrize("payload", ['{"text": "x"}', "42", '"text"', "null"])
def test_non_list_json_is_treated_as_empty(ledger_path, payload):
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(payload, encoding="utf-8")
    log = FL.FaultLog(path=str(ledger_path))
    assert log.recent() == []
    log.commit_event("after", [])
    assert len(_read(ledger_path)) == 1


def test_non_utf8_file_raises(ledger_path):
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(UnicodeDecodeError):
        FL.FaultLog(path=str(ledger_path)).recent()


def test_second_instance_sees_first_instances_writes(tmp_path):
    p = str(tmp_path / "shared.json")
    FL.FaultLog(path=p).commit_event("pump stalled", ["sump pump"])
    other = FL.FaultLog(path=p)
    assert [e["text"] for e in other.query_related_faults(["pump"])] == ["pump stalled"]


# ── query_related_faults ──────────────────────────────────────────────────────

def test_query_matches_text_case_insensitively(ledger):
    ledger.commit_event("Router REBOOTED unexpectedly", ["network"])
    ledger.commit_event("Freezer warm", ["kitchen"])
    hits = ledger.query_related_faults(["rebooted"])
    assert [h["text"] for h in hits] == ["Router REBOOTED unexpectedly"]
    assert ledger.query_related_faults(["ROUTER"]) == hits


def test_query_matches_tags_and_substrings(ledger):
    ledger.commit_event("generic warning", ["garage freezer"])
    assert len(ledger.query_related_faults(["freez"])) == 1


def test_query_returns_all_matches_oldest_first(ledger):
    ledger.commit_event("backup failed", ["backup"])
    ledger.commit_event("unrelated", ["other"])
    ledger.commit_event("backup failed again", ["backup"])
    assert [h["text"] for h in ledger.query_related_faults(["backup"])] == [
        "backup failed", "backup failed again"]


def test_any_keyword_is_enough(ledger):
    ledger.commit_event("battery low", ["sensor.hall_motion"])
    ledger.commit_event("ups on battery", ["ups"])
    ledger.commit_event("printer jam", ["office"])
    hits = ledger.query_related_faults(["nothing", "ups", "hall"])
    assert [h["text"] for h in hits] == ["battery low", "ups on battery"]


def test_query_returns_stored_entries(ledger):
    stored = ledger.commit_event("dns slow", ["dns"])
    assert ledger.query_related_faults(["dns"]) == [stored]


@pytest.mark.parametrize("keywords", [[], None, ["", "   "], ["\t"]])
def test_blank_keywords_give_nothing(ledger, keywords):
    ledger.commit_event("anything at all", ["tag"])
    assert ledger.query_related_faults(keywords) == []


def test_keywords_are_not_stripped(ledger):
    ledger.commit_event("disk", ["array"])
    assert ledger.query_related_faults([" disk "]) == []
    ledger.commit_event("the disk is failing", [])
    assert [h["text"] for h in ledger.query_related_faults([" disk "])] == ["the disk is failing"]


def test_keyword_can_span_text_and_tags(ledger):
    ledger.commit_event("Storage at 97 percent", ["root storage"])
    assert len(ledger.query_related_faults(["percent root"])) == 1


def test_keywords_are_stringified(ledger):
    ledger.commit_event("error code 503 from gateway", [])
    assert len(ledger.query_related_faults([503])) == 1


def test_non_dict_rows_break_query_but_not_recent(ledger_path):
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text('["stray", {"text": "ok", "tags": []}]', encoding="utf-8")
    log = FL.FaultLog(path=str(ledger_path))
    assert log.recent() == ["stray", {"text": "ok", "tags": []}]
    with pytest.raises(AttributeError):
        log.query_related_faults(["ok"])


def test_rows_missing_fields_are_tolerated(ledger_path):
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text('[{"tags": ["Attic Fan"]}, {"text": "Attic leak"}]',
                           encoding="utf-8")
    log = FL.FaultLog(path=str(ledger_path))
    assert len(log.query_related_faults(["attic"])) == 2


# ── recent ────────────────────────────────────────────────────────────────────

def test_recent_defaults_to_ten_newest_last(ledger):
    for i in range(12):
        ledger.commit_event(f"r{i}")
    assert [e["text"] for e in ledger.recent()] == [f"r{i}" for i in range(2, 12)]


def test_recent_with_limit(ledger):
    for i in range(5):
        ledger.commit_event(f"r{i}")
    assert [e["text"] for e in ledger.recent(2)] == ["r3", "r4"]
    assert len(ledger.recent(50)) == 5


def test_recent_zero_returns_everything(ledger):
    for i in range(3):
        ledger.commit_event(f"r{i}")
    assert len(ledger.recent(0)) == 3


def test_recent_negative_drops_from_front(ledger):
    for i in range(4):
        ledger.commit_event(f"r{i}")
    assert [e["text"] for e in ledger.recent(-1)] == ["r1", "r2", "r3"]
