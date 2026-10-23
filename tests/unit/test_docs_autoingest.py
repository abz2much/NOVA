"""Picking up new documents on a timer (v6.79.0).

The timer itself is wired up in __init__ and cannot run here, so these cases
cover the incremental scan underneath it: auto_ingest_new indexes a file it
has not seen, leaves an unchanged one alone (a timer must not re-embed the
whole library each run), indexes a changed one again, and never raises.
The indexing step is replaced so only the scan's own choices are tested.

Modification times are set with os.utime to fixed numbers, so nothing here
depends on the clock or on sleeping.
"""
import ast
import os
import pathlib
import sqlite3
import types

import pytest

from ws_sources import ws_function

_SOURCE = (pathlib.Path(__file__).resolve().parents[2]
           / "custom_components" / "nova" / "documents.py")
BASE_MTIME = 1_700_000_000.0


@pytest.fixture
def lib(load, tmp_path, monkeypatch):
    """The documents module pointed at an empty library in tmp_path, with
    indexing replaced by a recorder. Paths listed in .refuse fail to index."""
    docs = load("documents")
    folder = tmp_path / "docs"
    folder.mkdir()
    monkeypatch.setattr(docs, "DOCS_DIR", str(folder))
    monkeypatch.setattr(docs, "_DB_PATH", str(tmp_path / "nova.db"))
    state = types.SimpleNamespace(mod=docs, folder=folder, indexed=[], refuse=set())

    async def record(hass, path):
        state.indexed.append(path)
        name = pathlib.Path(path).name
        if name in state.refuse:
            return {"ok": False, "filename": name, "error": "unreadable"}
        return {"ok": True, "filename": name, "chunks": 3}

    monkeypatch.setattr(docs, "save_and_ingest_upload_from_path", record)
    return state


def _place(folder, name, text="hello", mtime=BASE_MTIME):
    path = folder / name
    path.write_text(text)
    os.utime(path, (mtime, mtime))
    return path


class _ExecutorLog:
    """A hass whose executor runs each job at once and notes its name, so a
    test can see that every blocking step went through the executor."""

    def __init__(self):
        self.jobs = []

    async def async_add_executor_job(self, func, *args):
        self.jobs.append(getattr(func, "__name__", repr(func)))
        return func(*args)


# ── what one scan picks up ──────────────────────────────────────────────────

async def test_first_scan_indexes_a_new_file(lib, fake_hass):
    path = _place(lib.folder, "manual.txt")
    report = await lib.mod.auto_ingest_new(fake_hass)
    assert report["ok"] is True
    assert report["new_files"] == 1
    assert lib.indexed == [str(path)]
    assert report["files"] == [{"source": "manual.txt", "ok": True,
                                "filename": "manual.txt", "chunks": 3}]


async def test_second_scan_skips_the_unchanged_file(lib, fake_hass):
    _place(lib.folder, "manual.txt")
    await lib.mod.auto_ingest_new(fake_hass)
    lib.indexed.clear()
    report = await lib.mod.auto_ingest_new(fake_hass)
    assert report["new_files"] == 0
    assert lib.indexed == []


@pytest.mark.parametrize("bump, indexed_again", [
    (0.5, False),     # within a second: treated as the same file
    (5.0, True),      # clearly newer: indexed again
])
async def test_change_threshold(lib, fake_hass, bump, indexed_again):
    path = _place(lib.folder, "manual.txt")
    await lib.mod.auto_ingest_new(fake_hass)
    lib.indexed.clear()
    _place(lib.folder, "manual.txt", "new content", BASE_MTIME + bump)
    report = await lib.mod.auto_ingest_new(fake_hass)
    assert report["new_files"] == (1 if indexed_again else 0)
    assert lib.indexed == ([str(path)] if indexed_again else [])


async def test_only_supported_types_are_indexed(lib, fake_hass):
    for name in ("photo.jpg", "notes.md", "archive.zip", "SHOUTY.TXT"):
        _place(lib.folder, name)
    report = await lib.mod.auto_ingest_new(fake_hass)
    names = sorted(pathlib.Path(p).name for p in lib.indexed)
    assert names == ["SHOUTY.TXT", "notes.md"]
    assert report["new_files"] == 2


async def test_sub_folders_are_not_indexed(lib, fake_hass):
    (lib.folder / "nested.txt").mkdir()
    report = await lib.mod.auto_ingest_new(fake_hass)
    assert report["new_files"] == 0 and lib.indexed == []


async def test_no_library_folder_is_fine(lib, fake_hass, monkeypatch):
    monkeypatch.setattr(lib.mod, "DOCS_DIR", str(lib.folder / "not-there"))
    report = await lib.mod.auto_ingest_new(fake_hass)
    assert report["ok"] is True
    assert report["new_files"] == 0
    assert lib.indexed == []


async def test_files_over_the_size_cap_are_left(lib, fake_hass, monkeypatch):
    monkeypatch.setattr(lib.mod, "_MAX_FILE_MB", 0.0001)    # about 100 bytes
    _place(lib.folder, "big.txt", "x" * 5000)
    report = await lib.mod.auto_ingest_new(fake_hass)
    assert report["new_files"] == 0
    assert lib.indexed == []


async def test_a_file_that_fails_is_retried_next_scan(lib, fake_hass):
    _place(lib.folder, "broken.txt")
    lib.refuse.add("broken.txt")
    first = await lib.mod.auto_ingest_new(fake_hass)
    assert first["new_files"] == 0
    assert first["files"][0]["ok"] is False
    lib.refuse.clear()
    second = await lib.mod.auto_ingest_new(fake_hass)
    assert second["new_files"] == 1                      # not remembered as done


# ── blocking work stays off the event loop (v8.7.4) ─────────────────────────

async def test_library_scan_uses_the_executor_for_each_step(lib):
    _place(lib.folder, "manual.txt")
    hass = _ExecutorLog()
    report = await lib.mod.auto_ingest_new(hass)
    assert report["new_files"] == 1
    assert hass.jobs == ["is_dir", "_read_seen", "_new_docs_in_library", "_mark_seen"]


async def test_watch_folder_scan_uses_the_executor_for_each_step(lib, tmp_path, monkeypatch):
    watched = tmp_path / "watch"
    watched.mkdir()
    _place(watched, "manual.txt")
    monkeypatch.setattr(
        lib.mod, "_cfg",
        lambda key, default: str(watched) if key == "document_watch_folders" else default)
    hass = _ExecutorLog()
    report = await lib.mod.scan_watch_folders(hass)
    assert report["new_files"] == 1
    assert report["watched"] == 1
    assert hass.jobs == ["_read_seen", "_new_docs_in_watch_folder",
                         "_copy_into_docs", "_mark_seen"]
    assert (lib.folder / "manual.txt").read_text() == "hello"   # now in the library
    hass.jobs.clear()
    again = await lib.mod.scan_watch_folders(hass)               # already seen
    assert again["new_files"] == 0
    assert hass.jobs == ["_read_seen", "_new_docs_in_watch_folder"]


async def test_folder_ingest_uses_the_executor_for_each_step(lib, load, monkeypatch):
    emb = load("embeddings")
    monkeypatch.setattr(lib.mod, "ingest_directory", lambda d=None: {
        "ok": True, "files": [{"ok": True, "source": "a.txt", "chunk_texts": ["x"]}]})
    for name, value in (("is_enabled", lambda: True), ("init_store", lambda: True),
                        ("forget_source", lambda s: None),
                        ("store_vectors", lambda *a: 1)):
        monkeypatch.setattr(emb, name, value)

    async def embed(hass, chunks):
        return [[0.1]]
    monkeypatch.setattr(emb, "embed_texts", embed)
    hass = _ExecutorLog()
    report = await lib.mod.ingest_directory_async(hass)
    assert report["semantic"] is True
    assert report["embedded_files"] == 1 and report["embedded_chunks"] == 1
    # ingest_directory and init_store first, then forget_source and store_vectors
    assert hass.jobs[:2] == ["<lambda>", "<lambda>"]
    assert len(hass.jobs) == 4


def test_seen_table_closes_every_connection(lib, monkeypatch):
    connections = []
    genuine_connect = sqlite3.connect

    def tracking(*args, **kwargs):
        conn = genuine_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking)
    assert lib.mod._read_seen() == {}                 # makes the table
    lib.mod._mark_seen("/x/a.txt", 1.0)
    assert lib.mod._read_seen() == {"/x/a.txt": 1.0}
    assert len(connections) == 3
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError):     # already closed
            conn.execute("SELECT 1")


# ── source checks: the async paths never block ──────────────────────────────

_ASYNC_PATHS = ("auto_ingest_new", "scan_watch_folders",
                "save_and_ingest_upload_from_path", "save_and_ingest_upload",
                "ingest_directory_async")
_BLOCKING_SNIPPETS = ("sqlite3", ".iterdir(", ".is_dir()", "embeddings.init_store()",
                      "embeddings.forget_source(", "= ingest_directory(",
                      "= ingest_file(")


def _async_bodies():
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    return {node.name: ast.unparse(node) for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name in _ASYNC_PATHS}


def test_every_async_path_is_present():
    assert sorted(_async_bodies()) == sorted(_ASYNC_PATHS)


@pytest.mark.parametrize("snippet", _BLOCKING_SNIPPETS)
def test_async_paths_do_no_blocking_call_directly(snippet):
    offenders = [name for name, body in _async_bodies().items() if snippet in body]
    assert offenders == []


def test_semantic_search_switch_saves_through_the_executor():
    body = ast.unparse(ws_function("ws_semantic_search"))
    assert "nova_config.set(" not in body
    assert "embeddings.init_store()" not in body
    assert body.count("async_add_executor_job(nova_config.set") == 2
    assert "async_add_executor_job(embeddings.init_store)" in body
