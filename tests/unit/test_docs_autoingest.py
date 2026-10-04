"""Tests for automatic document ingestion (v6.79.0).

The scheduler lives in __init__ (can't run headless), so these cover the
incremental logic that carries the weight: auto_ingest_new must ingest a new
file, skip an unchanged one (so a timer doesn't re-embed the library every
run), re-ingest a changed one, and never raise. Ingestion itself is stubbed."""
import pathlib
import sqlite3
import time

import pytest


@pytest.fixture
def docs(load, tmp_path, monkeypatch):
    m = load("documents")
    # isolate the docs dir + the seen-table DB
    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(m, "DOCS_DIR", str(d))
    monkeypatch.setattr(m, "_DB_PATH", str(tmp_path / "nova.db"))
    # stub the actual ingest so we test the scan/skip logic, not embedding
    calls = []
    async def _fake_ingest(hass, path):
        calls.append(path)
        return {"ok": True, "source": pathlib.Path(path).name, "chunks": 3}
    monkeypatch.setattr(m, "save_and_ingest_upload_from_path", _fake_ingest)
    m._ingest_calls = calls
    m._docs_dir = d
    return m


def _write(d, name, text="hello"):
    p = d / name
    p.write_text(text)
    return p


async def test_ingests_a_new_file(docs, fake_hass):
    _write(docs._docs_dir, "manual.txt")
    res = await docs.auto_ingest_new(fake_hass)
    assert res["new_files"] == 1
    assert docs._ingest_calls              # ingest was actually called


async def test_skips_unchanged_file_on_rescan(docs, fake_hass):
    _write(docs._docs_dir, "manual.txt")
    await docs.auto_ingest_new(fake_hass)             # first scan ingests
    docs._ingest_calls.clear()
    res = await docs.auto_ingest_new(fake_hass)       # second scan: nothing new
    assert res["new_files"] == 0
    assert docs._ingest_calls == []              # NOT re-ingested


async def test_reingests_changed_file(docs, fake_hass):
    p = _write(docs._docs_dir, "manual.txt")
    await docs.auto_ingest_new(fake_hass)
    docs._ingest_calls.clear()
    # change the file + bump its mtime past the 1s threshold
    time.sleep(0.01)
    p.write_text("new content")
    import os
    future = time.time() + 5
    os.utime(p, (future, future))
    res = await docs.auto_ingest_new(fake_hass)
    assert res["new_files"] == 1                 # change detected → re-ingested


async def test_ignores_unsupported_extensions(docs, fake_hass):
    _write(docs._docs_dir, "photo.jpg")
    _write(docs._docs_dir, "notes.md")
    res = await docs.auto_ingest_new(fake_hass)
    assert res["new_files"] == 1                 # only the .md
    assert any("notes.md" in c for c in docs._ingest_calls)
    assert not any("photo.jpg" in c for c in docs._ingest_calls)


async def test_missing_docs_dir_is_safe(docs, fake_hass, monkeypatch):
    monkeypatch.setattr(docs, "DOCS_DIR", "/nonexistent/path/xyz")
    res = await docs.auto_ingest_new(fake_hass)
    assert res["ok"] is True
    assert res["new_files"] == 0


async def test_skips_oversized_files(docs, fake_hass, monkeypatch):
    monkeypatch.setattr(docs, "_MAX_FILE_MB", 0.0001)   # ~100 bytes
    _write(docs._docs_dir, "big.txt", "x" * 5000)
    res = await docs.auto_ingest_new(fake_hass)
    assert res["new_files"] == 0


# ── Off the event loop (v8.7.4) ──────────────────────────────────────────────
# The folder listing, the stat calls and every sqlite access run in the
# executor. A recording fake hass proves each blocking step went through it.

class _RecordingHass:
    def __init__(self):
        self.jobs = []

    async def async_add_executor_job(self, func, *args):
        self.jobs.append(getattr(func, "__name__", repr(func)))
        return func(*args)


async def test_scan_runs_blocking_steps_in_the_executor(docs):
    _write(docs._docs_dir, "manual.txt")
    hass = _RecordingHass()
    res = await docs.auto_ingest_new(hass)
    assert res["new_files"] == 1
    assert hass.jobs == ["is_dir", "_read_seen", "_new_docs_in_library", "_mark_seen"]


async def test_watch_scan_runs_blocking_steps_in_the_executor(docs, tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    _write(watch, "manual.txt")
    monkeypatch.setattr(docs, "_cfg", lambda k, d: str(watch) if k == "document_watch_folders" else d)
    hass = _RecordingHass()
    res = await docs.scan_watch_folders(hass)
    assert res["new_files"] == 1
    assert hass.jobs == ["_read_seen", "_new_docs_in_watch_folder",
                         "_copy_into_docs", "_mark_seen"]
    hass.jobs.clear()
    res = await docs.scan_watch_folders(hass)           # second scan: seen
    assert res["new_files"] == 0
    assert hass.jobs == ["_read_seen", "_new_docs_in_watch_folder"]


async def test_seen_table_connections_are_closed(docs, monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    def _connect(*a, **k):
        conn = real_connect(*a, **k)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", _connect)
    assert docs._read_seen() == {}                 # creates the table
    docs._mark_seen("/x/a.txt", 1.0)
    assert docs._read_seen() == {"/x/a.txt": 1.0}
    assert len(opened) == 3
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):     # closed
            conn.execute("SELECT 1")


def test_scan_helpers_are_sync_and_scans_never_touch_sqlite_directly():
    import ast
    src = (pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
           / "documents.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name in (
                "auto_ingest_new", "scan_watch_folders",
                "save_and_ingest_upload_from_path", "save_and_ingest_upload",
                "ingest_directory_async"):
            body = ast.unparse(fn)
            assert "sqlite3" not in body
            assert ".iterdir(" not in body and ".is_dir()" not in body
            assert "embeddings.init_store()" not in body
            assert "embeddings.forget_source(" not in body
            assert "= ingest_directory(" not in body
            assert "= ingest_file(" not in body


def test_semantic_search_toggle_writes_off_the_loop():
    import ast
    src = (pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
           / "websocket.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "ws_semantic_search")
    body = ast.unparse(fn)
    assert "nova_config.set(" not in body
    assert "embeddings.init_store()" not in body
    assert body.count("async_add_executor_job(nova_config.set") == 2
    assert "async_add_executor_job(embeddings.init_store)" in body


async def test_directory_ingest_runs_blocking_steps_in_the_executor(docs, load, monkeypatch):
    emb = load("embeddings")
    monkeypatch.setattr(docs, "ingest_directory", lambda d=None: {
        "ok": True, "files": [{"ok": True, "source": "a.txt", "chunk_texts": ["x"]}]})
    monkeypatch.setattr(emb, "is_enabled", lambda: True)
    monkeypatch.setattr(emb, "init_store", lambda: True)
    monkeypatch.setattr(emb, "forget_source", lambda s: None)
    monkeypatch.setattr(emb, "store_vectors", lambda *a: 1)

    async def _embed(hass, chunks):
        return [[0.1]]

    monkeypatch.setattr(emb, "embed_texts", _embed)
    hass = _RecordingHass()
    res = await docs.ingest_directory_async(hass)
    assert res["semantic"] is True
    assert hass.jobs[:2] == ["<lambda>", "<lambda>"]       # ingest_directory, init_store
    assert len(hass.jobs) == 4                              # + forget_source, store_vectors
