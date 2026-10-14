"""Fresh behaviour tests for documents.py, written from its spec.

The document library: manuals and receipts kept in the Nova documents
folder, split into overlapping chunks, indexed for keyword search in the
document_fts table of nova.db, and embedded for semantic search when that
is switched on. Uploads and watch folders feed the same library. Nothing
here raises to a caller.
"""
from __future__ import annotations

import base64
import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def docs(load, tmp_path, monkeypatch):
    mod = load("documents")
    monkeypatch.setattr(mod, "DOCS_DIR", str(tmp_path / "library"))
    monkeypatch.setattr(mod, "_DB_PATH", str(tmp_path / "nova.db"))
    monkeypatch.setattr(mod, "_fts_ok", False)
    monkeypatch.setattr(mod, "_initialized", False)
    return mod


@pytest.fixture
def emb(load, monkeypatch):
    """embeddings.py with semantic search off unless a test turns it on."""
    mod = load("embeddings")
    state = {"enabled": False, "vectors": [[0.5]], "stored": [], "forgot": [],
             "inits": 0, "hits": []}
    monkeypatch.setattr(mod, "is_enabled", lambda: state["enabled"])

    def _init():
        state["inits"] += 1
        return True

    async def _embed(hass, chunks):
        return state["vectors"]

    async def _one(hass, text):
        return state["vectors"][0] if state["vectors"] else None

    def _store(source, chunks, vectors, ingested):
        state["stored"].append((source, list(chunks), ingested))
        return len(chunks)
    monkeypatch.setattr(mod, "init_store", _init)
    monkeypatch.setattr(mod, "embed_texts", _embed)
    monkeypatch.setattr(mod, "embed_one", _one)
    monkeypatch.setattr(mod, "forget_source", lambda s: state["forgot"].append(s))
    monkeypatch.setattr(mod, "store_vectors", _store)
    monkeypatch.setattr(mod, "search_vectors", lambda vec, k: [dict(h) for h in state["hits"]])
    return state


class _Hass:
    """Runs executor jobs inline and records their names."""

    def __init__(self):
        self.jobs = []

    async def async_add_executor_job(self, func, *args):
        self.jobs.append(getattr(func, "__name__", "?"))
        return func(*args)


def _lib(docs):
    path = Path(docs._documents_dir())
    path.mkdir(parents=True, exist_ok=True)
    return path


def _put(folder, name, text="the boiler filter is size 16x25"):
    path = Path(folder) / name
    path.write_text(text)
    return path


def _rows(tmp_path, sql):
    conn = sqlite3.connect(str(tmp_path / "nova.db"))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ── settings ────────────────────────────────────────────────────────────────

def test_defaults(load, monkeypatch):
    mod = load("documents")
    paths = load("paths")
    monkeypatch.setattr(mod, "DOCS_DIR", None)
    monkeypatch.setattr(mod, "_DB_PATH", None)
    monkeypatch.setattr(paths, "nova_path", lambda *p: "/n/" + "/".join(p))
    monkeypatch.setattr(paths, "nova_db", lambda: "/n/nova.db")
    assert mod._documents_dir() == "/n/documents" and mod._db_path() == "/n/nova.db"
    assert (mod._CHUNK_CHARS, mod._CHUNK_OVERLAP, mod._MAX_FILE_MB) == (900, 150, 25)
    assert mod._SUPPORTED == (".pdf", ".txt", ".md")


def test_config_values_fall_back(docs, load, monkeypatch):
    cfg = load("nova_config")
    monkeypatch.setattr(cfg, "get", lambda k, d=None: {"a": None, "b": "x"}.get(k, d))
    assert docs._cfg("a", 5) == 5 and docs._cfg("b", 5) == "x"

    def boom(k, d=None):
        raise RuntimeError
    monkeypatch.setattr(cfg, "get", boom)
    assert docs._cfg("b", 7) == 7


@pytest.mark.parametrize("raw, folders", [
    ("", []), (None, []), ("/a, /b\n\n /c ", ["/a", "/b", "/c"]),
    (["/x", "", "/y"], ["/x", "/y"]),
])
def test_watch_folders(docs, monkeypatch, raw, folders):
    monkeypatch.setattr(docs, "_cfg", lambda k, d: raw if raw is not None else d)
    assert docs._watch_folders() == folders


# ── reading files ───────────────────────────────────────────────────────────

def test_text_and_markdown_are_read_directly(docs, tmp_path):
    assert docs.extract_text(str(_put(tmp_path, "a.txt", "hello"))) == ("hello", None)
    assert docs.extract_text(str(_put(tmp_path, "b.MD", "# hi"))) == ("# hi", None)


def test_other_types_and_missing_files(docs, tmp_path):
    assert docs.extract_text(str(_put(tmp_path, "c.doc"))) == ("", "unsupported type .doc")
    text, err = docs.extract_text(str(tmp_path / "gone.txt"))
    assert text == "" and err.startswith("read error: ")


def test_pdf_libraries_are_tried_in_order(docs, tmp_path, monkeypatch):
    import builtins
    import sys
    import types
    real_import = builtins.__import__
    tried = []

    def fake_import(name, *a, **k):
        if name in ("pypdf", "pdfplumber", "PyPDF2"):
            tried.append(name)
            raise ImportError(name)
        return real_import(name, *a, **k)
    for name in ("pypdf", "pdfplumber", "PyPDF2"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    text, err = docs._extract_pdf(str(tmp_path / "x.pdf"))
    assert tried == ["pypdf", "pdfplumber", "PyPDF2"]
    assert text == "" and "no PDF library installed" in err

    page = types.SimpleNamespace(extract_text=lambda: "page")
    blank = types.SimpleNamespace(extract_text=lambda: None)
    fake = types.ModuleType("PyPDF2")
    fake.PdfReader = lambda path: types.SimpleNamespace(pages=[page, blank, page])
    monkeypatch.setitem(sys.modules, "PyPDF2", fake)
    monkeypatch.setattr(builtins, "__import__",
                        lambda name, *a, **k: fake if name == "PyPDF2"
                        else fake_import(name, *a, **k))
    assert docs._extract_pdf("x.pdf") == ("page\n\npage", None)


def test_a_failing_pdf_library_is_reported(docs, monkeypatch):
    import sys
    import types
    fake = types.ModuleType("pypdf")

    def broken(path):
        raise ValueError("bad xref")
    fake.PdfReader = broken
    monkeypatch.setitem(sys.modules, "pypdf", fake)
    assert docs._extract_pdf("x.pdf") == ("", "pypdf failed: bad xref")


# ── chunking ────────────────────────────────────────────────────────────────

def test_chunking_basics(docs):
    assert docs.chunk_text(None) == [] and docs.chunk_text(" \t ") == []
    assert docs.chunk_text("a  \t b\n c") == ["a b\n c"]


def test_chunks_break_at_the_best_boundary_past_half(docs):
    text = "x" * 60 + ". " + "y" * 30 + "\n\n" + "z" * 50
    chunks = docs.chunk_text(text, size=100, overlap=10)
    assert chunks[0] == "x" * 60 + ". " + "y" * 30
    assert all(len(c) <= 100 for c in chunks)
    assert chunks[-1].endswith("z" * 50)


def test_chunks_overlap_and_cover_everything(docs):
    words = " ".join(f"w{i}" for i in range(300))
    chunks = docs.chunk_text(words, size=120, overlap=30)
    assert len(chunks) > 5
    for left, right in zip(chunks, chunks[1:]):
        assert left.split()[-1] in right       # the boundary word is carried over
    assert {f"w{i}" for i in range(300)} <= set(" ".join(chunks).split())


def test_text_without_boundaries_is_cut_hard(docs):
    chunks = docs.chunk_text("q" * 250, size=100, overlap=20)
    assert [len(c) for c in chunks] == [100, 100, 90]


# ── filenames and uploads ───────────────────────────────────────────────────

@pytest.mark.parametrize("name, safe", [
    ("manual.pdf", "manual.pdf"), ("C:\\Users\\me\\Bill.TXT", "Bill.TXT"),
    ("../../x/notes.md", "notes.md"), ("a;b|c.txt", "a_b_c.txt"),
    (" (copy) list.txt ", "(copy) list.txt"), ("..", None), ("", None),
    (None, None), ("run.exe", None), ("noext", None),
])
def test_safe_filenames(docs, name, safe):
    assert docs._safe_filename(name) == safe


def test_safe_filenames_are_capped(docs):
    assert len(docs._safe_filename("a" * 300 + ".txt")) == 180


def _b64(data):
    return base64.b64encode(data).decode()


def test_an_upload_is_written_to_the_library(docs):
    res = docs.save_uploaded_file("Receipt.txt", _b64(b"bought 2024"))
    lib = Path(docs._documents_dir())
    assert res == {"ok": True, "filename": "Receipt.txt",
                   "path": str(lib / "Receipt.txt"), "bytes": 11}
    assert (lib / "Receipt.txt").read_bytes() == b"bought 2024"


def test_bad_uploads_are_refused(docs, monkeypatch):
    assert docs.save_uploaded_file("x.exe", _b64(b"x")) == {
        "ok": False, "error": "unsupported or unsafe filename (allowed: .pdf, .txt, .md)"}
    assert docs.save_uploaded_file("x.txt", 123) == {
        "ok": False, "error": "could not decode file content"}
    monkeypatch.setattr(docs, "_MAX_FILE_MB", 0.00001)
    assert docs.save_uploaded_file("x.txt", _b64(b"x" * 50)) == {
        "ok": False, "error": "file exceeds 1e-05MB"}


def test_an_unwritable_library_is_reported(docs, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setattr(docs, "DOCS_DIR", str(blocker / "lib"))
    res = docs.save_uploaded_file("x.txt", _b64(b"x"))
    assert res["ok"] is False and res["error"].startswith("write failed: ")


# ── the keyword index ───────────────────────────────────────────────────────

def test_ingesting_a_file_indexes_its_chunks(docs, tmp_path):
    path = _put(tmp_path, "boiler.txt", "Boiler pressure 1.5 bar. " * 60)
    res = docs.ingest_file(str(path))
    assert res["ok"] is True and res["source"] == "boiler.txt"
    assert res["chunks"] == len(res["chunk_texts"]) > 1 and res["ingested"]
    rows = _rows(tmp_path, "SELECT source, chunk_id, ingested FROM document_fts")
    assert [r[1] for r in rows] == [str(i) for i in range(res["chunks"])]
    assert {r[0] for r in rows} == {"boiler.txt"} and {r[2] for r in rows} == {res["ingested"]}
    assert docs._fts_ok is True and docs._initialized is True


def test_reingesting_replaces_the_old_chunks(docs, tmp_path):
    path = _put(tmp_path, "boiler.txt", "first version")
    docs.ingest_file(str(path))
    path.write_text("second version")
    docs.ingest_file(str(path))
    assert _rows(tmp_path, "SELECT content FROM document_fts") == [("second version",)]


def test_ingest_failures(docs, tmp_path, monkeypatch):
    assert docs.ingest_file(str(_put(tmp_path, "x.doc"))) == {
        "source": "x.doc", "chunks": 0, "ok": False, "error": "unsupported type .doc"}
    assert docs.ingest_file(str(_put(tmp_path, "e.txt", "  "))) == {
        "source": "e.txt", "chunks": 0, "ok": False,
        "error": "no extractable text (scanned image PDF?)"}
    monkeypatch.setattr(docs, "_initialized", True)
    monkeypatch.setattr(docs, "_fts_ok", False)
    assert docs.ingest_file(str(_put(tmp_path, "y.txt")))["error"] == (
        "no vector or FTS store available")


def test_a_broken_database_disables_the_index(docs, tmp_path, monkeypatch):
    monkeypatch.setattr(docs, "_DB_PATH", str(tmp_path / "missing" / "nova.db"))
    assert docs._init_fts() is False and docs._fts_ok is False
    assert docs.search_documents("boiler") == []
    assert docs.library_status() == {"fts": False, "directory": docs._documents_dir(),
                                     "chunk_count": 0, "sources": []}


def test_keyword_search(docs, tmp_path):
    docs.ingest_file(str(_put(tmp_path, "a.txt", "The boiler filter is 16x25.")))
    docs.ingest_file(str(_put(tmp_path, "b.txt", "Dishwasher bought in March.")))
    hits = docs.search_documents("which FILTER fits the boiler?", k=5)
    assert hits == [{"text": "The boiler filter is 16x25.", "source": "a.txt",
                     "chunk": "0", "score": None}]
    assert docs.search_documents("") == [] and docs.search_documents("  ") == []
    assert docs.search_documents("zebra") == []
    assert docs.search_documents("!!!") == []       # no words: an FTS error, not a raise


def test_library_status_counts_by_source(docs, tmp_path):
    docs.ingest_file(str(_put(tmp_path, "a.txt", "alpha")))
    docs.ingest_file(str(_put(tmp_path, "b.txt", "bravo " * 400)))
    status = docs.library_status()
    assert status["fts"] is True and status["directory"] == docs._documents_dir()
    by = {s["source"]: s["chunks"] for s in status["sources"]}
    assert by["a.txt"] == 1 and by["b.txt"] > 1
    assert status["chunk_count"] == sum(by.values())


def test_ingesting_a_folder(docs, tmp_path, monkeypatch):
    folder = tmp_path / "in"
    folder.mkdir()
    _put(folder, "b.txt")
    _put(folder, "a.md")
    _put(folder, "c.jpg")
    _put(folder, "d.txt", " ")
    (folder / "sub.txt").mkdir()
    res = docs.ingest_directory(str(folder))
    assert [r["source"] for r in res["files"]] == ["a.md", "b.txt", "d.txt"]
    assert res["ok"] is True and res["files_ingested"] == 2 and res["files_seen"] == 3
    assert res["total_chunks"] == 2 and res["directory"] == str(folder)
    monkeypatch.setattr(docs, "_MAX_FILE_MB", 0.00001)
    res = docs.ingest_directory(str(folder))
    assert res["files"][0] == {"source": "a.md", "chunks": 0, "ok": False,
                               "error": "larger than 1e-05MB — skipped"}


def test_ingesting_the_default_folder_creates_it(docs):
    res = docs.ingest_directory()
    assert res["directory"] == docs._documents_dir() and os.path.isdir(res["directory"])
    assert res["files"] == [] and res["total_chunks"] == 0


def test_an_unusable_folder_is_reported(docs, tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("")
    res = docs.ingest_directory(str(blocker / "x"))
    assert res["ok"] is False and res["files"] == [] and res["total_chunks"] == 0
    assert res["error"].startswith(f"cannot access {blocker / 'x'}: ")


def test_deleting_a_document(docs, tmp_path, emb):
    lib = _lib(docs)
    docs.ingest_file(str(_put(lib, "old.txt")))
    res = docs.delete_source("../old.txt")
    assert res == {"ok": True, "filename": "old.txt"}
    assert not (lib / "old.txt").exists()
    assert _rows(tmp_path, "SELECT * FROM document_fts") == []
    assert emb["forgot"] == ["old.txt"]
    assert docs.delete_source("bad.exe") == {"ok": False, "error": "invalid filename"}
    assert docs.delete_source("never.txt") == {"ok": True, "filename": "never.txt"}


def test_a_document_that_cannot_be_removed(docs, monkeypatch, emb):
    lib = _lib(docs)
    (lib / "locked.txt").mkdir()          # a folder: unlink fails
    res = docs.delete_source("locked.txt")
    assert res["ok"] is False and res["filename"] == "locked.txt"
    assert res["error"].startswith("file remove failed: ")


# ── semantic layers ─────────────────────────────────────────────────────────

async def test_a_folder_ingest_without_semantic_search(docs, emb):
    _put(_lib(docs), "a.txt")
    res = await docs.ingest_directory_async(_Hass())
    assert res["semantic"] is False and res["files_ingested"] == 1
    assert emb["inits"] == 0


async def test_a_folder_ingest_with_semantic_search(docs, emb):
    lib = _lib(docs)
    _put(lib, "a.txt")
    _put(lib, "b.txt")
    _put(lib, "c.txt", " ")
    emb["enabled"] = True
    hass = _Hass()
    res = await docs.ingest_directory_async(hass)
    assert res["semantic"] is True and res["embedded_files"] == 2
    assert res["embedded_chunks"] == 2 and "semantic_error" not in res
    assert [s[0] for s in emb["stored"]] == ["a.txt", "b.txt"]
    assert hass.jobs == ["ingest_directory", "_init", "<lambda>", "_store",
                         "<lambda>", "_store"]


async def test_unreachable_embeddings_leave_keyword_search(docs, emb):
    _put(_lib(docs), "a.txt")
    emb["enabled"], emb["vectors"] = True, None
    res = await docs.ingest_directory_async(_Hass())
    assert res["semantic"] is False and res["embedded_files"] == 0
    assert res["semantic_error"].startswith("Ollama embeddings unavailable")
    assert res["files_ingested"] == 1


async def test_an_upload_is_saved_indexed_and_embedded(docs, emb):
    emb["enabled"] = True
    res = await docs.save_and_ingest_upload(_Hass(), "Bill.txt", _b64(b"gas bill 2024"))
    assert res == {"ok": True, "filename": "Bill.txt", "chunks": 1, "embedded": 1}
    assert emb["forgot"] == ["Bill.txt"]


async def test_upload_outcomes(docs, emb):
    hass = _Hass()
    assert (await docs.save_and_ingest_upload(hass, "x.exe", _b64(b"x")))["ok"] is False
    res = await docs.save_and_ingest_upload(hass, "e.txt", _b64(b"  "))
    assert res == {"ok": False, "filename": "e.txt", "chunks": 0,
                   "error": "no extractable text (scanned image PDF?)"}
    emb["enabled"], emb["vectors"] = True, None
    res = await docs.save_and_ingest_upload(hass, "n.txt", _b64(b"note"))
    assert res["semantic_note"] == "saved & indexed; Ollama embedding unavailable"
    assert res["ok"] is True and "embedded" not in res


async def test_ingesting_a_placed_file(docs, emb):
    path = _put(_lib(docs), "p.txt")
    emb["enabled"] = True
    res = await docs.save_and_ingest_upload_from_path(_Hass(), str(path))
    assert res == {"ok": True, "filename": "p.txt", "chunks": 1}
    assert [s[0] for s in emb["stored"]] == ["p.txt"]
    bad = await docs.save_and_ingest_upload_from_path(_Hass(), str(_put(_lib(docs), "q.txt", "")))
    assert bad["ok"] is False and bad["error"]


async def test_search_prefers_semantic_hits(docs, emb, tmp_path):
    docs.ingest_file(str(_put(tmp_path, "a.txt", "boiler filter")))
    emb["enabled"], emb["hits"] = True, [{"text": "s", "source": "v.txt"}]
    assert await docs.search_documents_async(_Hass(), "boiler") == [
        {"text": "s", "source": "v.txt", "engine": "semantic"}]
    emb["hits"] = []
    hits = await docs.search_documents_async(_Hass(), "boiler")
    assert [h["engine"] for h in hits] == ["keyword"] and hits[0]["source"] == "a.txt"
    emb["enabled"] = False
    hass = _Hass()
    assert (await docs.search_documents_async(hass, "boiler"))[0]["engine"] == "keyword"
    assert hass.jobs == ["search_documents"]                # the query runs off the loop


# ── automatic pick-up ───────────────────────────────────────────────────────

def test_the_seen_table(docs):
    assert docs._read_seen() == {}
    docs._mark_seen("/a.txt", 5.0)
    docs._mark_seen("/a.txt", 6.0)
    assert docs._read_seen() == {"/a.txt": 6.0}
    assert docs._is_unchanged({"/a.txt": 6.0}, "/a.txt", 6.9)
    assert not docs._is_unchanged({"/a.txt": 6.0}, "/a.txt", 7.0)
    assert not docs._is_unchanged({}, "/a.txt", 6.0)


async def test_new_and_changed_library_files_are_picked_up(docs, emb):
    lib = _lib(docs)
    first = _put(lib, "a.txt")
    _put(lib, "skip.png")
    hass = _Hass()
    res = await docs.auto_ingest_new(hass)
    assert res["ok"] is True and res["new_files"] == 1
    assert res["files"] == [{"source": "a.txt", "ok": True, "filename": "a.txt", "chunks": 1}]
    assert hass.jobs[:3] == ["is_dir", "_read_seen", "_new_docs_in_library"]
    assert (await docs.auto_ingest_new(_Hass()))["new_files"] == 0
    os.utime(first, (first.stat().st_atime, first.stat().st_mtime + 30))
    assert (await docs.auto_ingest_new(_Hass()))["new_files"] == 1


async def test_a_failed_pick_up_is_tried_again(docs, emb):
    _put(_lib(docs), "e.txt", " ")
    res = await docs.auto_ingest_new(_Hass())
    assert res["new_files"] == 0 and res["files"][0]["ok"] is False
    assert docs._read_seen() == {}


async def test_no_library_folder(docs):
    assert await docs.auto_ingest_new(_Hass()) == {
        "ok": True, "new_files": 0, "note": "docs dir absent"}


def test_listing_new_files_skips_big_and_seen_ones(docs, tmp_path, monkeypatch):
    folder = tmp_path / "w"
    folder.mkdir()
    a = _put(folder, "a.txt")
    _put(folder, "b.txt", "x" * 500)
    _put(folder, "c.gif")
    monkeypatch.setattr(docs, "_MAX_FILE_MB", 0.0001)
    seen = {}
    assert [n for _k, _m, n in docs._new_docs_in_library(folder, seen)] == ["a.txt"]
    assert [n for _k, _m, n in docs._new_docs_in_watch_folder(folder, seen)] == ["a.txt"]
    seen = {str(a): a.stat().st_mtime}
    assert docs._new_docs_in_library(folder, seen) == []
    assert docs._new_docs_in_watch_folder(folder, seen) == []
    assert docs._new_docs_in_watch_folder(tmp_path / "nope", {}) == []


async def test_watch_folders_copy_new_files_into_the_library(docs, tmp_path, monkeypatch, emb):
    watch = tmp_path / "downloads"
    watch.mkdir()
    _put(watch, "Manual (2).txt")
    _put(watch, "blank.txt", "")
    monkeypatch.setattr(docs, "_cfg", lambda k, d: f"{watch}, {tmp_path / 'missing'}")
    res = await docs.scan_watch_folders(_Hass())
    assert res["ok"] is True and res["watched"] == 2 and res["new_files"] == 1
    assert (Path(docs._documents_dir()) / "Manual (2).txt").exists()
    # a file that failed to ingest is not remembered, so the next scan retries it
    assert set(docs._read_seen()) == {str(watch / "Manual (2).txt")}
    again = await docs.scan_watch_folders(_Hass())
    assert again["new_files"] == 0
    assert [f["source"] for f in again["files"]] == ["blank.txt"]


async def test_watch_scan_without_folders(docs, monkeypatch):
    monkeypatch.setattr(docs, "_cfg", lambda k, d: d)
    assert await docs.scan_watch_folders(_Hass()) == {
        "ok": True, "watched": 0, "new_files": 0, "note": "no watch folders configured"}


def test_copying_into_the_library(docs, tmp_path):
    src = _put(tmp_path, "In Box.md")
    res = docs._copy_into_docs(str(src))
    assert res == {"ok": True, "path": str(Path(docs._documents_dir()) / "In Box.md"),
                   "filename": "In Box.md"}
    assert docs._copy_into_docs(str(tmp_path / "x.exe")) == {"ok": False}
    assert docs._copy_into_docs(str(tmp_path / "gone.txt")) == {"ok": False}
