"""The household document library: manuals, receipts and notes Nova can
answer from ("what size is the boiler filter?").

Documents live as files in the Nova documents folder. Each one is split into
overlapping chunks and indexed in its own document_fts table in nova.db,
apart from conversation memory, so a manual never shows up as something
said and a chat never shows up as a manual. Keyword search on that index
always works; when semantic search is switched on the chunks are also
embedded through embeddings.py and searched by meaning first.

Files arrive three ways: dropped into the folder (picked up by
auto_ingest_new), uploaded from the panel, or found in a configured watch
folder and copied in. Plain text and Markdown always work; PDFs need one of
pypdf, pdfplumber or PyPDF2, and say so plainly when none is installed.

Every entry point reports problems in its result and never raises.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import shutil
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import paths
from .persistence import sqlite as _store

_LOGGER = logging.getLogger(__name__)

# Test overrides. None means <nova folder>/documents and nova.db.
DOCS_DIR: Optional[str] = None
_DB_PATH: Optional[str] = None

_CHUNK_CHARS = 900            # about a paragraph or two per chunk
_CHUNK_OVERLAP = 150          # text carried into the next chunk
_MAX_FILE_MB = 25
_SUPPORTED = (".pdf", ".txt", ".md")

# Set once the keyword index has been prepared (or failed to be).
_fts_ok = False
_initialized = False

_WATCH_KEY = "document_watch_folders"
_BREAKS = ("\n\n", "\n", ". ", " ")       # preferred chunk ends, best first


def _documents_dir() -> str:
    return DOCS_DIR or paths.nova_path("documents")


def _db_path() -> str:
    return _DB_PATH or paths.nova_db()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _too_big(size: float) -> bool:
    return size > _MAX_FILE_MB * 1_000_000


def _supported(path: Path) -> bool:
    return path.suffix.lower() in _SUPPORTED


def _open_db(rows: bool = False):
    """A connection to nova.db; ``rows`` gives name-addressable rows."""
    if rows:
        return _store.connect(_db_path(), mkdir=False, busy_timeout_ms=None)
    return _store.connect(_db_path(), row_factory=False, mkdir=False,
                          busy_timeout_ms=None)


# ── the keyword index ───────────────────────────────────────────────────────

def _init_fts() -> bool:
    """Create the document_fts table if needed; remember whether it works."""
    global _fts_ok
    try:
        with closing(_open_db()) as conn:
            _store.ensure(conn, "document_fts")
            conn.commit()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Nova documents: FTS5 init failed: %s", err)
        return False
    _fts_ok = True
    _LOGGER.info("Nova documents: FTS5 ready")
    return True


def _ensure_init() -> None:
    """Prepare the index once per run, whether or not that succeeds."""
    global _initialized
    if not _initialized:
        _init_fts()
        _initialized = True


def _forget_source(source: str) -> None:
    """Drop every chunk of one document from the keyword index."""
    if not _fts_ok:
        return
    try:
        with closing(_open_db()) as conn:
            conn.execute("DELETE FROM document_fts WHERE source = ?", (source,))
            conn.commit()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Nova documents: removing %s from the index failed: %s",
                      source, err)


# ── reading files ───────────────────────────────────────────────────────────

def extract_text(path: str) -> tuple[str, Optional[str]]:
    """(text, error). The error is a readable reason and the text is empty
    when the file could not be read."""
    suffix = Path(path).suffix.lower()
    try:
        if suffix in (".txt", ".md"):
            return Path(path).read_text(encoding="utf-8", errors="ignore"), None
        if suffix == ".pdf":
            return _extract_pdf(path)
    except Exception as err:  # noqa: BLE001
        return "", f"read error: {err}"
    return "", f"unsupported type {suffix}"


def _pages_text(pages) -> str:
    return "\n".join(page.extract_text() or "" for page in pages)


def _read_with_pypdf(path: str) -> str:
    import pypdf
    return _pages_text(pypdf.PdfReader(path).pages)


def _read_with_pdfplumber(path: str) -> str:
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return _pages_text(pdf.pages)


def _read_with_pypdf2(path: str) -> str:
    import PyPDF2
    return _pages_text(PyPDF2.PdfReader(path).pages)


_PDF_READERS = (("pypdf", _read_with_pypdf),
                ("pdfplumber", _read_with_pdfplumber),
                ("PyPDF2", _read_with_pypdf2))


def _extract_pdf(path: str) -> tuple[str, Optional[str]]:
    """The PDF's text through the first installed library."""
    for name, reader in _PDF_READERS:
        try:
            return reader(path), None
        except ImportError:
            continue
        except Exception as err:  # noqa: BLE001
            return "", f"{name} failed: {err}"
    return "", ("no PDF library installed — add 'pypdf' to read PDFs "
                "(plain .txt/.md files work without it)")


# ── chunking ────────────────────────────────────────────────────────────────

def _chunk_end(text: str, start: int, size: int) -> int:
    """Where the chunk starting at ``start`` should stop: at the best break
    in its second half, else at ``size`` characters."""
    end = min(start + size, len(text))
    if end == len(text):
        return end
    piece = text[start:end]
    for mark in _BREAKS:
        at = piece.rfind(mark)
        if at > size * 0.5:
            return start + at + len(mark)
    return end


def chunk_text(text: str, size: int = _CHUNK_CHARS,
               overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Split text into chunks of at most ``size`` characters, each starting
    ``overlap`` characters before the previous one ended. Runs of spaces
    and tabs become one space. Pure and deterministic."""
    clean = re.sub(r"[ \t]+", " ", text or "").strip()
    if len(clean) <= size:
        return [clean] if clean else []
    chunks: list[str] = []
    start = 0
    while True:
        end = _chunk_end(clean, start, size)
        piece = clean[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(clean):
            return chunks
        start = max(end - overlap, start + 1)


# ── adding and removing documents ───────────────────────────────────────────

def _failed(source: str, error: str) -> dict:
    return {"source": source, "chunks": 0, "ok": False, "error": error}


def ingest_file(path: str) -> dict:
    """Index one file for keyword search, replacing any earlier version.
    On success the chunks and the ingest time are returned too, so a caller
    can embed them for semantic search."""
    _ensure_init()
    source = os.path.basename(path)
    text, error = extract_text(path)
    if error:
        return _failed(source, error)
    chunks = chunk_text(text)
    if not chunks:
        return _failed(source, "no extractable text (scanned image PDF?)")
    _forget_source(source)
    if not _fts_ok:
        return _failed(source, "no vector or FTS store available")
    stamp = _now_iso()
    try:
        with closing(_open_db()) as conn:
            conn.executemany(
                "INSERT INTO document_fts (content, source, chunk_id, ingested) "
                "VALUES (?, ?, ?, ?)",
                [(chunk, source, str(n), stamp) for n, chunk in enumerate(chunks)])
            conn.commit()
    except Exception as err:  # noqa: BLE001
        return _failed(source, f"fts store failed: {err}")
    return {"source": source, "chunks": len(chunks), "ok": True,
            "chunk_texts": chunks, "ingested": stamp}


def ingest_directory(directory: Optional[str] = None) -> dict:
    """Index every supported file in a folder (the library by default),
    creating the folder when it is missing."""
    folder = directory or _documents_dir()
    _ensure_init()
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": f"cannot access {folder}: {err}",
                "files": [], "total_chunks": 0}
    results = []
    for path in sorted(Path(folder).iterdir()):
        if not (path.is_file() and _supported(path)):
            continue
        try:
            oversized = _too_big(path.stat().st_size)
        except OSError:
            oversized = False
        if oversized:
            results.append(_failed(path.name, f"larger than {_MAX_FILE_MB}MB — skipped"))
            continue
        results.append(ingest_file(str(path)))
    return {"ok": True, "files": results,
            "files_ingested": sum(1 for r in results if r.get("ok")),
            "files_seen": len(results),
            "total_chunks": sum(r.get("chunks", 0) for r in results),
            "directory": folder}


async def _embed_chunks(hass, source: str, chunks: list, ingested: str) -> Optional[int]:
    """Replace a document's vectors. None when embedding was unavailable,
    else how many vectors were stored."""
    from . import embeddings
    vectors = await embeddings.embed_texts(hass, chunks)
    if vectors is None:
        return None
    await hass.async_add_executor_job(embeddings.forget_source, source)
    return await hass.async_add_executor_job(
        embeddings.store_vectors, source, chunks, vectors, ingested)


async def ingest_directory_async(hass, directory: Optional[str] = None) -> dict:
    """Index the library, then embed it when semantic search is on. If the
    embedding host cannot be reached the keyword index still stands."""
    from . import embeddings
    report = await hass.async_add_executor_job(ingest_directory, directory)
    if not report.get("ok") or not embeddings.is_enabled():
        report["semantic"] = False
        return report
    await hass.async_add_executor_job(embeddings.init_store)
    files = chunks = 0
    for result in report.get("files", []):
        if not (result.get("ok") and result.get("chunk_texts")):
            continue
        stored = await _embed_chunks(hass, result["source"], result["chunk_texts"],
                                     result.get("ingested", ""))
        if stored is None:
            report["semantic_error"] = ("Ollama embeddings unavailable — pull the "
                                        "embed model and check the host; keyword "
                                        "search is active")
            break
        if stored:
            files += 1
            chunks += stored
    report["semantic"] = files > 0
    report["embedded_files"] = files
    report["embedded_chunks"] = chunks
    return report


async def _index_placed_file(hass, path: str, filename: str) -> tuple[dict, dict]:
    """Index a file already in the library and embed it when semantic search
    is on. Returns (outcome for the caller, the raw ingest result)."""
    result = await hass.async_add_executor_job(ingest_file, path)
    outcome: dict[str, Any] = {"ok": bool(result.get("ok")), "filename": filename,
                               "chunks": result.get("chunks", 0)}
    if not result.get("ok"):
        outcome["error"] = result.get("error")
    return outcome, result


async def save_and_ingest_upload_from_path(hass, path: str) -> dict:
    """Index a file already placed in the library (watch folder copies and
    files dropped into the folder), embedding it when semantic search is on."""
    outcome, result = await _index_placed_file(hass, path, os.path.basename(path))
    if not outcome["ok"]:
        return outcome
    try:
        from . import embeddings
        if embeddings.is_enabled() and result.get("chunk_texts"):
            await hass.async_add_executor_job(embeddings.init_store)
            await _embed_chunks(hass, outcome["filename"], result["chunk_texts"],
                                result.get("ingested", ""))
    except Exception:  # noqa: BLE001 - keyword search already works
        pass
    return outcome


def _safe_filename(name: str) -> Optional[str]:
    """A user-given file name reduced to a safe bare name of a supported
    type, or None. Folders, separators and odd characters never survive."""
    if not name:
        return None
    bare = os.path.basename(str(name).replace("\\", "/").strip())
    bare = re.sub(r"[^A-Za-z0-9._ \-()]", "_", bare).strip(". ")
    if bare in ("", ".", ".."):
        return None
    if os.path.splitext(bare)[1].lower() not in _SUPPORTED:
        return None
    return bare[:180]


def save_uploaded_file(filename: str, b64_content: str) -> dict:
    """Write a base64 upload into the library under a safe name. It is not
    indexed here."""
    safe = _safe_filename(filename)
    if not safe:
        return {"ok": False,
                "error": "unsupported or unsafe filename (allowed: .pdf, .txt, .md)"}
    try:
        data = base64.b64decode(b64_content, validate=False)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "could not decode file content"}
    if _too_big(len(data)):
        return {"ok": False, "error": f"file exceeds {_MAX_FILE_MB}MB"}
    try:
        library = Path(_documents_dir())
        library.mkdir(parents=True, exist_ok=True)
        target = library / safe
        if not str(target.resolve()).startswith(str(library.resolve())):
            return {"ok": False, "error": "path escapes documents directory"}
        target.write_bytes(data)
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": f"write failed: {err}"}
    return {"ok": True, "filename": safe, "path": str(target), "bytes": len(data)}


async def save_and_ingest_upload(hass, filename: str, b64_content: str) -> dict:
    """Save an upload, index it, and embed it when semantic search is on."""
    saved = await hass.async_add_executor_job(save_uploaded_file, filename, b64_content)
    if not saved.get("ok"):
        return saved
    outcome, result = await _index_placed_file(hass, saved["path"], saved["filename"])
    if not outcome["ok"]:
        return outcome
    try:
        from . import embeddings
        if embeddings.is_enabled() and result.get("chunk_texts"):
            await hass.async_add_executor_job(embeddings.init_store)
            stored = await _embed_chunks(hass, saved["filename"], result["chunk_texts"],
                                         result.get("ingested", ""))
            if stored is None:
                outcome["semantic_note"] = "saved & indexed; Ollama embedding unavailable"
            else:
                outcome["embedded"] = stored
    except Exception:  # noqa: BLE001 - keyword search already works
        pass
    return outcome


def _forget_vectors(source: str) -> None:
    try:
        from . import embeddings
    except Exception:  # noqa: BLE001 - vectors are optional
        return
    try:
        embeddings.forget_source(source)
    except Exception:  # noqa: BLE001
        pass


def delete_source(filename: str) -> dict:
    """Remove a document: its keyword chunks, its vectors and its file."""
    safe = _safe_filename(filename)
    if not safe:
        return {"ok": False, "error": "invalid filename"}
    _forget_source(safe)
    _forget_vectors(safe)
    target = Path(_documents_dir()) / safe
    try:
        target.unlink(missing_ok=True)
    except Exception as err:  # noqa: BLE001
        # The full OS error (with its path) goes only to the Home Assistant log.
        from .safe_errors import safe_error_message
        reason = safe_error_message(err, where="document delete", log=True)
        return {"ok": False, "filename": safe, "error": "file remove failed: " + reason}
    return {"ok": True, "filename": safe}


# ── searching ───────────────────────────────────────────────────────────────

def search_documents(query: str, k: int = 4) -> list[dict]:
    """The ``k`` best keyword matches as {"text", "source", "chunk", "score"}.
    The query's first eight words are matched with OR."""
    if not query or not query.strip():
        return []
    _ensure_init()
    if not _fts_ok:
        return []
    words = re.findall(r"\w+", query)[:8]
    match = " OR ".join(words) or query
    try:
        with closing(_open_db(rows=True)) as conn:
            found = conn.execute(
                "SELECT content, source, chunk_id, rank FROM document_fts "
                "WHERE document_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, k)).fetchall()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Nova documents: keyword search failed: %s", err)
        return []
    return [{"text": row["content"], "source": row["source"],
             "chunk": row["chunk_id"], "score": None} for row in found]


async def search_documents_async(hass, query: str, k: int = 4) -> list[dict]:
    """Semantic hits when semantic search is on and finds something, else
    keyword hits. Each hit names its engine."""
    from . import embeddings
    if embeddings.is_enabled():
        vector = await embeddings.embed_one(hass, query)
        if vector:
            hits = await hass.async_add_executor_job(embeddings.search_vectors, vector, k)
            if hits:
                for hit in hits:
                    hit["engine"] = "semantic"
                return hits
    hits = search_documents(query, k)
    for hit in hits:
        hit["engine"] = "keyword"
    return hits


def library_status() -> dict:
    """The index state, the library folder and chunk counts per document."""
    _ensure_init()
    status: dict[str, Any] = {"fts": _fts_ok, "directory": _documents_dir(),
                              "chunk_count": 0, "sources": []}
    if not _fts_ok:
        return status
    try:
        with closing(_open_db(rows=True)) as conn:
            counted = conn.execute(
                "SELECT source, COUNT(*) n FROM document_fts GROUP BY source").fetchall()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Nova documents: status read failed: %s", err)
        return status
    status["sources"] = [{"source": row["source"], "chunks": row["n"]} for row in counted]
    status["chunk_count"] = sum(entry["chunks"] for entry in status["sources"])
    return status


# ── picking up new files ────────────────────────────────────────────────────
# What has been picked up is remembered (path and modification time) in the
# document_watch_seen table, so a timed scan only touches new or changed
# files. The blocking helpers here run in the executor.

def _cfg(key: str, default):
    """A saved Nova setting, or ``default`` when unset or unreadable."""
    try:
        from . import nova_config
        value = nova_config.get(key, default)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else value


def _watch_folders() -> list[str]:
    """The extra folders to watch, from a list or a comma or line separated
    string."""
    raw = _cfg(_WATCH_KEY, "")
    if not raw:
        return []
    if isinstance(raw, list):
        return [folder for folder in raw if folder]
    parts = str(raw).replace(",", "\n").splitlines()
    return [part.strip() for part in parts if part.strip()]


def _read_seen() -> dict:
    """{path: modification time} of every file picked up so far."""
    try:
        with closing(_open_db()) as conn:
            _store.ensure(conn, "document_watch_seen")
            conn.commit()
            return dict(conn.execute("SELECT path, mtime FROM document_watch_seen"))
    except Exception:  # noqa: BLE001
        return {}


def _mark_seen(key: str, mtime: float) -> None:
    try:
        with closing(_open_db()) as conn:
            conn.execute("INSERT OR REPLACE INTO document_watch_seen "
                         "(path, mtime, ingested) VALUES (?, ?, ?)",
                         (key, mtime, _now_iso()))
            conn.commit()
    except Exception:  # noqa: BLE001
        pass


def _is_unchanged(seen: dict, key: str, mtime: float) -> bool:
    """Picked up before at (within a second of) this modification time."""
    return key in seen and abs(seen[key] - mtime) < 1.0


def _candidate(path: Path, seen: dict) -> Optional[tuple[str, float, str]]:
    """(path, mtime, name) when the file is supported, small enough and new
    or changed; else None. Raises OSError when it cannot be examined."""
    if not (path.is_file() and _supported(path)):
        return None
    info = path.stat()
    if _too_big(info.st_size) or _is_unchanged(seen, str(path), info.st_mtime):
        return None
    return str(path), info.st_mtime, path.name


def _new_docs_in_library(docs: Path, seen: dict) -> list[tuple[str, float, str]]:
    """New or changed files in the library folder."""
    found = []
    for path in sorted(docs.iterdir()):
        try:
            hit = _candidate(path, seen)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("auto-ingest of %s failed: %s", path, err)
            continue
        if hit:
            found.append(hit)
    return found


def _new_docs_in_watch_folder(d: Path, seen: dict) -> list[tuple[str, float, str]]:
    """New or changed files in one watch folder ([] when it is not a folder)."""
    if not d.is_dir():
        return []
    found = []
    for path in sorted(d.iterdir()):
        try:
            hit = _candidate(path, seen)
        except Exception:  # noqa: BLE001
            continue
        if hit:
            found.append(hit)
    return found


def _copy_into_docs(src_path: str) -> dict:
    """Copy a watched file into the library under a safe name."""
    safe = _safe_filename(os.path.basename(src_path))
    if not safe:
        return {"ok": False}
    try:
        library = Path(_documents_dir())
        library.mkdir(parents=True, exist_ok=True)
        target = library / safe
        shutil.copy2(src_path, target)
    except Exception:  # noqa: BLE001
        return {"ok": False}
    return {"ok": True, "path": str(target), "filename": safe}


async def auto_ingest_new(hass) -> dict:
    """Index only the library files that are new or changed since they were
    last picked up, so a timed scan never re-embeds the whole library. A
    file that fails is tried again next time."""
    library = Path(_documents_dir())
    if not await hass.async_add_executor_job(library.is_dir):
        return {"ok": True, "new_files": 0, "note": "docs dir absent"}
    seen = await hass.async_add_executor_job(_read_seen)
    waiting = await hass.async_add_executor_job(_new_docs_in_library, library, seen)
    results = []
    for key, mtime, name in waiting:
        try:
            outcome = await save_and_ingest_upload_from_path(hass, key)
            results.append({"source": name, **outcome})
            if outcome.get("ok"):
                await hass.async_add_executor_job(_mark_seen, key, mtime)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("auto-ingest of %s failed: %s", key, err)
    return {"ok": True, "new_files": sum(1 for r in results if r.get("ok")),
            "files": results}


async def scan_watch_folders(hass) -> dict:
    """Copy new files from the watch folders into the library and index
    them. A copied file is remembered whether or not it indexed."""
    folders = _watch_folders()
    if not folders:
        return {"ok": True, "watched": 0, "new_files": 0,
                "note": "no watch folders configured"}
    seen = await hass.async_add_executor_job(_read_seen)
    results = []
    for folder in folders:
        try:
            waiting = await hass.async_add_executor_job(
                _new_docs_in_watch_folder, Path(folder), seen)
            for key, mtime, name in waiting:
                copied = await hass.async_add_executor_job(_copy_into_docs, key)
                if not copied.get("ok"):
                    continue
                outcome = await save_and_ingest_upload_from_path(hass, copied["path"])
                results.append({"source": name, **outcome})
                await hass.async_add_executor_job(_mark_seen, key, mtime)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("watch scan of %s failed: %s", folder, err)
    return {"ok": True, "watched": len(folders),
            "new_files": sum(1 for r in results if r.get("ok")), "files": results}
