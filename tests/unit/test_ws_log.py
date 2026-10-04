"""Characterisation tests for the panel's debug log (ws_log.py).

The log is two in memory ring buffers (everything, and conversation and reply
routing only) plus a persistent file written by a daemon thread. These tests
pin its behaviour as it was when it lived in websocket.py, so that moving it
to ws_log.py could not change anything: buffer limits, which categories reach
the conversation buffer, the persisted line format and the parser that reads
it back at import, the queue full drop, and the writer's rotation.

Each test gets a fresh copy of the module (so its buffers, queue and writer
flag start empty) with Nova's config directory pointed at a temporary folder.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
import threading

import pytest
from ws_sources import COMP

_MODULE_NAME = "jc._ws_log_under_test"


@pytest.fixture
def make_log(tmp_path, monkeypatch, load):
    """Return make(persisted=None): a fresh ws_log module. `persisted` is the
    text of nova.log that exists before the module is imported, because the
    module reads it once, at import."""
    paths = load("paths")
    monkeypatch.setattr(paths, "_root", str(tmp_path))
    (tmp_path / "nova").mkdir()
    log_path = tmp_path / "nova" / "nova.log"

    def make(persisted: str | None = None):
        if persisted is not None:
            log_path.write_text(persisted)
        sys.modules.pop(_MODULE_NAME, None)
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, COMP / "ws_log.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)
        return module

    make.log_path = log_path
    yield make
    sys.modules.pop(_MODULE_NAME, None)


@pytest.fixture
def quiet(make_log, monkeypatch):
    """A fresh module whose entries are not queued for the writer thread."""
    mod = make_log()
    monkeypatch.setattr(mod, "_persist_log_entry", lambda entry: None)
    return mod


def _entry(i, cat="X"):
    return {"date": "2026-01-02", "ts": "03:04:05", "cat": cat, "msg": f"m{i}"}


# ── ring buffers ─────────────────────────────────────────────────────────────

def test_buffer_limits(quiet):
    assert quiet._DEBUG_LOG.maxlen == 500
    assert quiet._CONV_LOG.maxlen == 80


def test_debug_buffer_keeps_the_newest_500(quiet):
    for i in range(600):
        quiet.nova_log("SENTINEL", f"m{i}")
    assert len(quiet._DEBUG_LOG) == 500
    assert quiet._DEBUG_LOG[0]["msg"] == "m100"
    assert quiet._DEBUG_LOG[-1]["msg"] == "m599"


def test_conversation_buffer_keeps_the_newest_80(quiet):
    for i in range(100):
        quiet.nova_log("CONV", f"m{i}")
    assert len(quiet._CONV_LOG) == 80
    assert quiet._CONV_LOG[0]["msg"] == "m20"
    assert quiet._CONV_LOG[-1]["msg"] == "m99"


def test_conversation_categories_are_exact(quiet):
    assert quiet._CONV_CATEGORIES == frozenset(
        {"CONV", "LOCAL", "AGENT", "REPLY", "ROUTE", "OFFLINE", "ERROR"})


def test_only_conversation_categories_reach_the_conversation_buffer(quiet):
    for cat in sorted(quiet._CONV_CATEGORIES):
        quiet.nova_log(cat, "in")
    for cat in ("SENTINEL", "OBSERVER", "conv", "Error", ""):
        quiet.nova_log(cat, "out")
    assert {e["msg"] for e in quiet._CONV_LOG} == {"in"}
    assert len(quiet._CONV_LOG) == len(quiet._CONV_CATEGORIES)
    assert len(quiet._DEBUG_LOG) == len(quiet._CONV_CATEGORIES) + 5


def test_noise_cannot_evict_conversation_entries(quiet):
    quiet.nova_log("REPLY", "kept")
    for i in range(1000):
        quiet.nova_log("SENTINEL", f"noise{i}")
    assert [e["msg"] for e in quiet._CONV_LOG] == ["kept"]
    assert all(e["msg"] != "kept" for e in quiet._DEBUG_LOG)


def test_entry_shape_and_message_cap(quiet):
    quiet.nova_log("CONV", "x" * 800)
    entry = quiet._DEBUG_LOG[-1]
    assert set(entry) == {"date", "ts", "cat", "msg"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["date"])
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", entry["ts"])
    assert entry["cat"] == "CONV"
    assert entry["msg"] == "x" * 500
    assert quiet._CONV_LOG[-1] is entry          # one shared entry, not a copy


def test_nova_log_queues_the_entry_for_the_writer(make_log, monkeypatch):
    mod = make_log()
    queued = []
    monkeypatch.setattr(mod, "_persist_log_entry", queued.append)
    mod.nova_log("CONV", "hello")
    assert queued == [mod._DEBUG_LOG[-1]]


def test_recent_debug_log_slicing(quiet):
    for i in range(200):
        quiet.nova_log("SENTINEL", f"m{i}")
    assert len(quiet.recent_debug_log()) == 150           # default n
    assert quiet.recent_debug_log()[-1]["msg"] == "m199"
    assert [e["msg"] for e in quiet.recent_debug_log(2)] == ["m198", "m199"]
    assert len(quiet.recent_debug_log(1000)) == 200       # n beyond the buffer
    assert len(quiet.recent_debug_log(0)) == 200          # 0 means everything
    assert len(quiet.recent_debug_log(-5)) == 200         # so does a negative n
    assert isinstance(quiet.recent_debug_log(), list)


def test_recent_conversation_log_slicing(quiet):
    for i in range(100):
        quiet.nova_log("ROUTE", f"m{i}")
    assert len(quiet.recent_conversation_log()) == 80     # default n
    assert [e["msg"] for e in quiet.recent_conversation_log(3)] == ["m97", "m98", "m99"]
    assert len(quiet.recent_conversation_log(0)) == 80
    assert len(quiet.recent_conversation_log(-1)) == 80


def test_recent_logs_return_copies_of_the_buffer(quiet):
    quiet.nova_log("CONV", "a")
    quiet.recent_debug_log().clear()
    quiet.recent_conversation_log().clear()
    assert len(quiet._DEBUG_LOG) == 1 and len(quiet._CONV_LOG) == 1


# ── persisted log: file location ─────────────────────────────────────────────

def test_log_file_defaults_to_nova_log_under_the_config_dir(make_log):
    mod = make_log()
    assert mod._LOG_FILE is None
    assert mod._log_file() == make_log.log_path


def test_log_file_override_wins(make_log, tmp_path):
    mod = make_log()
    mod._LOG_FILE = tmp_path / "elsewhere.log"
    assert mod._log_file() == tmp_path / "elsewhere.log"


# ── persisted log: reading it back at import ─────────────────────────────────

def test_import_loads_the_persisted_log_once(make_log):
    mod = make_log(
        "2026-01-01 10:00:00 [CONV] first\n"
        "2026-01-01 10:00:01 [SENTINEL] second\n"
        "2026-01-01 10:00:02 [ERROR] third\n")
    assert [e["msg"] for e in mod._DEBUG_LOG] == ["first", "second", "third"]
    assert [e["msg"] for e in mod._CONV_LOG] == ["first", "third"]
    assert mod._DEBUG_LOG[0] == {
        "date": "2026-01-01", "ts": "10:00:00", "cat": "CONV", "msg": "first"}


def test_persisted_line_parser_accepts_and_rejects(make_log):
    mod = make_log(
        "2026-01-01 10:00:00 [CONV] ok with [brackets] and spaces\n"
        "not a log line\n"
        "2026-01-01 10:00:01 [TWO WORDS] bad category\n"
        "2026-01-01 10:00:02 [CONV]\n"                       # no message
        "2026-01-01 10:00:03 CONV no brackets\n"
        "2026-1-1 10:00:04 [CONV] short date\n"
        "\n"
        "2026-01-01 10:00:05 [AGENT] last good\n")
    assert [e["msg"] for e in mod._DEBUG_LOG] == [
        "ok with [brackets] and spaces", "last good"]


def test_only_the_last_200_persisted_lines_are_loaded(make_log):
    mod = make_log("".join(
        f"2026-01-01 10:00:00 [SENTINEL] line{i}\n" for i in range(250)))
    assert len(mod._DEBUG_LOG) == 200
    assert mod._DEBUG_LOG[0]["msg"] == "line50"
    assert mod._DEBUG_LOG[-1]["msg"] == "line249"


def test_loaded_conversation_entries_respect_the_buffer_limit(make_log):
    mod = make_log("".join(
        f"2026-01-01 10:00:00 [CONV] line{i}\n" for i in range(150)))
    assert len(mod._DEBUG_LOG) == 150
    assert len(mod._CONV_LOG) == 80
    assert mod._CONV_LOG[0]["msg"] == "line70"


def test_a_missing_persisted_log_is_fine(make_log):
    mod = make_log()
    assert not make_log.log_path.exists()
    assert list(mod._DEBUG_LOG) == [] and list(mod._CONV_LOG) == []


def test_an_unreadable_persisted_log_is_swallowed(make_log):
    # exists() is true but read_text() raises: import must not fail.
    make_log.log_path.mkdir()
    mod = make_log()
    assert list(mod._DEBUG_LOG) == []


# ── queue full drop ──────────────────────────────────────────────────────────

def test_a_full_queue_drops_the_persisted_copy_without_blocking(make_log, monkeypatch):
    mod = make_log()
    monkeypatch.setattr(mod, "_ensure_writer", lambda: None)
    assert mod._LOG_QUEUE.maxsize == 2000
    for i in range(2000):
        mod._persist_log_entry(_entry(i))
    assert mod._LOG_QUEUE.qsize() == 2000
    mod._persist_log_entry(_entry("dropped"))            # returns, does not raise
    assert mod._LOG_QUEUE.qsize() == 2000
    assert mod._LOG_QUEUE.get_nowait()["msg"] == "m0"    # the queue is unchanged


def test_nova_log_still_fills_the_buffers_when_the_queue_is_full(make_log, monkeypatch):
    mod = make_log()
    monkeypatch.setattr(mod, "_ensure_writer", lambda: None)
    for i in range(2000):
        mod._persist_log_entry(_entry(i))
    mod.nova_log("CONV", "still visible")
    assert mod._DEBUG_LOG[-1]["msg"] == "still visible"
    assert mod._CONV_LOG[-1]["msg"] == "still visible"


# ── writer thread ────────────────────────────────────────────────────────────

def _writers():
    return [t for t in threading.enumerate() if t.name == "nova-log-writer"]


def test_ensure_writer_starts_one_daemon_thread_once(make_log):
    mod = make_log()
    before = len(_writers())
    mod._ensure_writer()
    mod._ensure_writer()
    assert len(_writers()) == before + 1
    assert mod._WRITER_STARTED is True
    assert _writers()[-1].daemon is True


def test_writer_appends_entries_in_the_persisted_format(make_log):
    mod = make_log()
    mod.nova_log("CONV", "one")
    mod.nova_log("SENTINEL", "two")
    mod._LOG_QUEUE.join()
    lines = make_log.log_path.read_text().splitlines()
    assert len(lines) == 2
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[CONV\] one", lines[0])
    assert lines[1].endswith("[SENTINEL] two")


def test_what_the_writer_writes_the_importer_reads_back(make_log):
    mod = make_log()
    mod.nova_log("REPLY", "round trip")
    mod._LOG_QUEUE.join()
    mod2 = make_log()                                    # re-import reads the file
    assert [e["msg"] for e in mod2._DEBUG_LOG] == ["round trip"]
    assert mod2._DEBUG_LOG[0] == {
        k: mod._DEBUG_LOG[0][k] for k in ("date", "ts", "cat", "msg")}
    assert [e["cat"] for e in mod2._CONV_LOG] == ["REPLY"]


def test_writer_uses_the_override_path_and_creates_its_folder(make_log, tmp_path):
    mod = make_log()
    mod._LOG_FILE = tmp_path / "deep" / "er" / "x.log"
    mod.nova_log("CONV", "there")
    mod._LOG_QUEUE.join()
    assert mod._LOG_FILE.read_text().endswith("[CONV] there\n")


def test_writer_rotates_a_log_over_2mb_to_its_last_2000_lines(make_log):
    mod = make_log()
    line = "2026-01-01 10:00:00 [SENTINEL] " + "x" * 960 + "\n"     # 1,000 bytes
    make_log.log_path.write_text("".join(
        line.replace("[SENTINEL] x", f"[SENTINEL] {i:04d}x", 1) for i in range(2100)))
    assert make_log.log_path.stat().st_size > 2_000_000
    mod.nova_log("CONV", "newest")
    mod._LOG_QUEUE.join()
    lines = make_log.log_path.read_text().splitlines()
    assert len(lines) == 2000
    assert lines[-1].endswith("[CONV] newest")
    assert "[SENTINEL] 0101x" in lines[0]                # 2101 lines, first 101 gone
    assert make_log.log_path.read_text().endswith("\n")


def test_writer_does_not_rotate_a_small_log(make_log):
    mod = make_log()
    make_log.log_path.write_text("".join(
        f"2026-01-01 10:00:00 [SENTINEL] l{i}\n" for i in range(50)))
    mod.nova_log("CONV", "x")
    mod._LOG_QUEUE.join()
    assert len(make_log.log_path.read_text().splitlines()) == 51


def test_a_bad_entry_does_not_stop_the_writer(make_log):
    mod = make_log()
    mod._persist_log_entry({})                           # KeyError inside the loop
    mod._persist_log_entry(None)                         # ignored
    mod.nova_log("CONV", "after")
    mod._LOG_QUEUE.join()                                # every task_done ran
    assert make_log.log_path.read_text().endswith("[CONV] after\n")


# ── how the module is wired (static: websocket.py cannot be imported here) ───

_MOVED = {"_DEBUG_LOG", "_CONV_CATEGORIES", "_CONV_LOG", "_LOG_FILE", "_log_file",
          "_LOG_QUEUE", "_WRITER_STARTED", "_WRITER_LOCK", "_log_writer_loop",
          "_ensure_writer", "_persist_log_entry", "_load_persisted_log", "nova_log",
          "recent_conversation_log", "recent_debug_log"}


def _defined(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_ws_log_defines_the_log_and_imports_nothing_from_home_assistant():
    tree = ast.parse((COMP / "ws_log.py").read_text(encoding="utf-8"))
    assert _MOVED <= _defined(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] == "homeassistant" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "homeassistant"
            if node.level:
                # the only Nova module it may use is paths (`from . import paths`)
                assert node.module is None and [a.name for a in node.names] == ["paths"]


def test_the_persisted_log_is_loaded_once_at_import_and_nowhere_else():
    tree = ast.parse((COMP / "ws_log.py").read_text(encoding="utf-8"))
    top_calls = [ast.unparse(n.value) for n in tree.body
                 if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    assert top_calls == ["_load_persisted_log()"]


def test_websocket_reexports_the_log_names_other_code_reaches_through_it():
    tree = ast.parse((COMP / "websocket.py").read_text(encoding="utf-8"))
    imported = {a.name: a.asname for n in tree.body
                if isinstance(n, ast.ImportFrom) and n.level == 1 and n.module == "ws_log"
                for a in n.names}
    assert {"nova_log", "recent_debug_log", "recent_conversation_log",
            "_DEBUG_LOG", "_LOG_QUEUE", "_ensure_writer"} <= set(imported)
    assert not any(imported.values())                  # re-exported under their own names
    # _LOG_FILE is reassigned in tests; a copy of it in websocket.py would be a trap.
    assert "_LOG_FILE" not in imported
    assert not (_MOVED & _defined(tree))               # defined in ws_log.py only
