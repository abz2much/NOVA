"""
Nova Document RAG agent (v6.55.0).

The blueprint's "Document Analytics Agent": semantic retrieval over the
household's manuals and receipts, so Nova can answer "what's the filter size
for the furnace?" or "when did we buy the dishwasher?" from your own paperwork
instead of guessing.

Design, mirroring memory.py's proven ChromaDB approach exactly:
  - A SEPARATE Chroma collection ('nova_documents') in the same persistent
    client. Documents are a different corpus from conversation turns — a manual
    is not a chat message, and a furnace-spec query should not surface old
    conversations, nor should "what did I say yesterday" surface the manual.
  - Same default embedding function Chroma uses for memory (documents are
    embedded internally on add/query — no extra model dependency), cosine space.
  - FTS5 fallback in the existing nova.db when ChromaDB isn't installed, so
    keyword retrieval still works on a minimal install.

Ingestion source: /config/nova/documents/ — drop PDFs or .txt files there
(via the HA File editor, Samba, etc.) and Nova ingests them. No upload UI
needed; it uses infrastructure the user already has, exactly like config.json.

PDF text extraction degrades honestly: it tries pypdf, then pdfplumber, then
PyPDF2; if none is installed it says so and skips that file rather than
crashing — plain-text files always work regardless. Nothing here ever raises
to the caller.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .persistence import sqlite as _store
from . import paths

_LOGGER = logging.getLogger(__name__)

DOCS_DIR: Optional[str] = None  # override; None resolves via paths.py


def _documents_dir() -> str:
    return DOCS_DIR or paths.nova_path("documents")


MEMORY_DIR: Optional[str] = None  # override; None resolves via paths.py; same Chroma client as memory.py


def _memory_dir() -> str:
    return MEMORY_DIR or paths.memory_dir()


_COLLECTION_NAME = "nova_documents"
_DB_PATH: Optional[str] = None  # override; None resolves via paths.py


def _db_path() -> str:
    return _DB_PATH or paths.nova_db()

_CHUNK_CHARS = 900            # ~1 chunk ≈ a paragraph or two — good recall granularity
_CHUNK_OVERLAP = 150         # carry context across chunk boundaries
_MAX_FILE_MB = 25            # skip absurdly large files
_SUPPORTED = (".pdf", ".txt", ".md")

_chroma_ok = False
_collection = None
_fts_ok = False
_initialized = False


# ── init (mirrors memory.py) ─────────────────────────────────────────────────

def _init_chroma() -> bool:
    global _chroma_ok, _collection
    try:
        import chromadb
        client = chromadb.PersistentClient(path=_memory_dir())
        _collection = client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        _chroma_ok = True
        _LOGGER.info("Nova documents: ChromaDB collection ready at %s", _memory_dir())
        return True
    except ImportError:
        _LOGGER.debug("Nova documents: ChromaDB not available, FTS5 fallback")
        return False
    except Exception as exc:
        _LOGGER.warning("Nova documents: ChromaDB init failed: %s", exc)
        return False


def _init_fts() -> bool:
    global _fts_ok
    try:
        import sqlite3
        conn = sqlite3.connect(_db_path())
        _store.ensure(conn, "document_fts")
        conn.commit()
        conn.close()
        _fts_ok = True
        _LOGGER.info("Nova documents: FTS5 fallback ready")
        return True
    except Exception as exc:
        _LOGGER.warning("Nova documents: FTS5 init failed: %s", exc)
        return False


def _ensure_init() -> None:
    global _initialized
    if _initialized:
        return
    if not _init_chroma():
        _init_fts()
    _initialized = True


# ── PDF text extraction (graceful across libs / none) ────────────────────────

def extract_text(path: str) -> tuple[str, Optional[str]]:
    """
    (text, error). Plain text/markdown read directly; PDFs via whichever of
    pypdf / pdfplumber / PyPDF2 is installed. error is a human string when we
    couldn't read it (missing lib, unreadable) — text is "" in that case.
    """
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext in (".txt", ".md"):
            return p.read_text(encoding="utf-8", errors="ignore"), None
        if ext == ".pdf":
            return _extract_pdf(path)
        return "", f"unsupported type {ext}"
    except Exception as exc:
        return "", f"read error: {exc}"


def _extract_pdf(path: str) -> tuple[str, Optional[str]]:
    # pypdf (current), then pdfplumber (better layout), then PyPDF2 (legacy).
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        return text, None
    except ImportError:
        pass
    except Exception as exc:
        return "", f"pypdf failed: {exc}"
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            text = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
        return text, None
    except ImportError:
        pass
    except Exception as exc:
        return "", f"pdfplumber failed: {exc}"
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(path)
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        return text, None
    except ImportError:
        pass
    except Exception as exc:
        return "", f"PyPDF2 failed: {exc}"
    return "", ("no PDF library installed — add 'pypdf' to read PDFs "
                "(plain .txt/.md files work without it)")


# ── chunking (pure) ──────────────────────────────────────────────────────────

def chunk_text(text: str, size: int = _CHUNK_CHARS,
               overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Split into overlapping chunks on paragraph/sentence-ish boundaries.
    Pure and deterministic — the retrieval-quality core, fully testable."""
    text = re.sub(r"[ \t]+", " ", (text or "")).strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # prefer to break at a paragraph, then sentence, then space
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", " "):
                idx = window.rfind(sep)
                if idx > size * 0.5:          # don't make a tiny chunk
                    end = start + idx + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def _doc_id(source: str, idx: int, chunk: str) -> str:
    h = hashlib.sha1(f"{source}:{idx}:{chunk[:60]}".encode()).hexdigest()[:12]
    return f"{source}__{idx}__{h}"


# ── ingest ───────────────────────────────────────────────────────────────────

def _forget_source(source: str) -> None:
    """Remove all chunks for a source before re-ingesting (idempotent re-scan)."""
    if _chroma_ok and _collection is not None:
        try:
            _collection.delete(where={"source": source})
        except Exception as exc:
            _LOGGER.debug("doc forget (chroma) failed: %s", exc)
    if _fts_ok:
        try:
            import sqlite3
            conn = sqlite3.connect(_db_path())
            conn.execute("DELETE FROM document_fts WHERE source = ?", (source,))
            conn.commit()
            conn.close()
        except Exception as exc:
            _LOGGER.debug("doc forget (fts) failed: %s", exc)


def delete_source(filename: str) -> dict:
    """Remove a document entirely: delete its file from DOCS_DIR and purge all
    its chunks from FTS/chroma and its vectors from the embedding store. Returns
    {"ok", "filename", "error"?}. Never raises."""
    safe = _safe_filename(filename)
    if not safe:
        return {"ok": False, "error": "invalid filename"}
    # purge chunks (FTS + chroma)
    _forget_source(safe)
    # purge vectors
    try:
        from . import embeddings
        embeddings.forget_source(safe)
    except Exception:
        pass
    # remove the file
    try:
        p = Path(_documents_dir()) / safe
        if p.exists():
            p.unlink()
    except Exception as exc:
        # The detail (an OS error with the full path) goes to the Home
        # Assistant log only; the panel gets the error type (8.7.23).
        from .safe_errors import safe_error_message
        return {"ok": False, "filename": safe,
                "error": "file remove failed: "
                         + safe_error_message(exc, where="document delete", log=True)}
    return {"ok": True, "filename": safe}


def ingest_file(path: str) -> dict:
    """Ingest one document file into keyword (FTS) search. Returns
    {"source", "chunks", "ok", "chunk_texts"?, "error"?}. Never raises.
    chunk_texts is included on success so an async caller can also embed them
    for semantic search (v6.57.0)."""
    _ensure_init()
    source = os.path.basename(path)
    text, err = extract_text(path)
    if err:
        return {"source": source, "chunks": 0, "ok": False, "error": err}
    chunks = chunk_text(text)
    if not chunks:
        return {"source": source, "chunks": 0, "ok": False,
                "error": "no extractable text (scanned image PDF?)"}

    _forget_source(source)
    ingested = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    if _chroma_ok and _collection is not None:
        try:
            _collection.add(
                documents=chunks,
                metadatas=[{"source": source, "chunk": i, "ingested": ingested}
                           for i in range(len(chunks))],
                ids=[_doc_id(source, i, c) for i, c in enumerate(chunks)],
            )
            return {"source": source, "chunks": len(chunks), "ok": True,
                    "chunk_texts": chunks, "ingested": ingested}
        except Exception as exc:
            _LOGGER.debug("doc chroma add failed: %s", exc)

    if _fts_ok:
        try:
            import sqlite3
            conn = sqlite3.connect(_db_path())
            conn.executemany(
                "INSERT INTO document_fts (content, source, chunk_id, ingested) "
                "VALUES (?, ?, ?, ?)",
                [(c, source, str(i), ingested) for i, c in enumerate(chunks)],
            )
            conn.commit()
            conn.close()
            return {"source": source, "chunks": len(chunks), "ok": True,
                    "chunk_texts": chunks, "ingested": ingested}
        except Exception as exc:
            return {"source": source, "chunks": 0, "ok": False,
                    "error": f"fts store failed: {exc}"}

    return {"source": source, "chunks": 0, "ok": False,
            "error": "no vector or FTS store available"}


async def ingest_directory_async(hass, directory: Optional[str] = None) -> dict:
    """Ingest the documents folder, adding Ollama-embedded vectors when semantic
    search is enabled (v6.57.0). Always does keyword (FTS) ingest; layers vector
    embeddings on top when available. Falls back silently to keyword-only if
    Ollama is unreachable."""
    from . import embeddings
    base = await hass.async_add_executor_job(ingest_directory, directory)  # FTS ingest (sync)
    if not base.get("ok") or not embeddings.is_enabled():
        base["semantic"] = False
        return base

    await hass.async_add_executor_job(embeddings.init_store)
    embedded_files = 0
    embedded_chunks = 0
    semantic_error = None
    for res in base.get("files", []):
        if not res.get("ok") or not res.get("chunk_texts"):
            continue
        source = res["source"]
        chunks = res["chunk_texts"]
        vecs = await embeddings.embed_texts(hass, chunks)
        if vecs is None:
            semantic_error = ("Ollama embeddings unavailable — pull the embed "
                              "model and check the host; keyword search is active")
            break
        await hass.async_add_executor_job(embeddings.forget_source, source)
        stored = await hass.async_add_executor_job(
            embeddings.store_vectors, source, chunks, vecs,
            res.get("ingested", ""))
        if stored:
            embedded_files += 1
            embedded_chunks += stored

    base["semantic"] = embedded_files > 0
    base["embedded_files"] = embedded_files
    base["embedded_chunks"] = embedded_chunks
    if semantic_error:
        base["semantic_error"] = semantic_error
    return base


    base["embedded_chunks"] = embedded_chunks
    if semantic_error:
        base["semantic_error"] = semantic_error
    return base


def _safe_filename(name: str) -> Optional[str]:
    """Sanitize a user-supplied filename to a bare, safe basename. Returns None
    if it can't be made safe or isn't a supported type. Prevents path traversal
    (../, absolute paths, separators) since this name is written to disk."""
    import os as _os
    import re as _re
    if not name:
        return None
    # strip any directory components — keep only the final name
    base = _os.path.basename(str(name).replace("\\", "/").strip())
    # drop anything that isn't a sane filename char
    base = _re.sub(r"[^A-Za-z0-9._ \-()]", "_", base).strip(". ")
    if not base or base in (".", ".."):
        return None
    if _os.path.splitext(base)[1].lower() not in _SUPPORTED:
        return None
    return base[:180]          # bound length


def save_uploaded_file(filename: str, b64_content: str) -> dict:
    """Write an uploaded document (base64) into DOCS_DIR, safely. Returns
    {"ok", "filename"?, "path"?, "error"?}. Does NOT ingest — caller ingests
    after. Never raises."""
    import base64 as _b64
    safe = _safe_filename(filename)
    if not safe:
        return {"ok": False, "error": "unsupported or unsafe filename "
                                      "(allowed: .pdf, .txt, .md)"}
    try:
        raw = _b64.b64decode(b64_content, validate=False)
    except Exception:
        return {"ok": False, "error": "could not decode file content"}
    if len(raw) > _MAX_FILE_MB * 1_000_000:
        return {"ok": False, "error": f"file exceeds {_MAX_FILE_MB}MB"}
    try:
        d = Path(_documents_dir())
        d.mkdir(parents=True, exist_ok=True)
        dest = d / safe
        # resolve and re-check the destination stays inside DOCS_DIR
        if not str(dest.resolve()).startswith(str(d.resolve())):
            return {"ok": False, "error": "path escapes documents directory"}
        dest.write_bytes(raw)
        return {"ok": True, "filename": safe, "path": str(dest),
                "bytes": len(raw)}
    except Exception as exc:
        return {"ok": False, "error": f"write failed: {exc}"}


async def save_and_ingest_upload(hass, filename: str, b64_content: str) -> dict:
    """Save an uploaded file then ingest it (semantic-aware). Returns the save
    result merged with per-file ingest outcome. Never raises."""
    saved = await hass.async_add_executor_job(
        save_uploaded_file, filename, b64_content)
    if not saved.get("ok"):
        return saved
    # ingest just this file (FTS), then embed if semantic is on
    res = await hass.async_add_executor_job(ingest_file, saved["path"])
    out = {"ok": bool(res.get("ok")), "filename": saved["filename"],
           "chunks": res.get("chunks", 0)}
    if not res.get("ok"):
        out["error"] = res.get("error")
        return out
    try:
        from . import embeddings
        if embeddings.is_enabled() and res.get("chunk_texts"):
            await hass.async_add_executor_job(embeddings.init_store)
            vecs = await embeddings.embed_texts(hass, res["chunk_texts"])
            if vecs is not None:
                await hass.async_add_executor_job(
                    embeddings.forget_source, saved["filename"])
                n = await hass.async_add_executor_job(
                    embeddings.store_vectors, saved["filename"],
                    res["chunk_texts"], vecs, res.get("ingested", ""))
                out["embedded"] = n
            else:
                out["semantic_note"] = "saved & indexed; Ollama embedding unavailable"
    except Exception:
        pass
    return out


def _watch_folders() -> list[str]:
    """Configured extra folders to scan for documents (besides DOCS_DIR)."""
    raw = _cfg("document_watch_folders", "")
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        items = [p.strip() for p in str(raw).replace(",", "\n").splitlines()]
    return [p for p in items if p]


def _cfg(key: str, default):
    try:
        from . import nova_config
        val = nova_config.get(key, default)
        return val if val is not None else default
    except Exception:
        return default


# Blocking helpers for the two scans below; run them in the executor.

def _read_seen() -> dict:
    """Path -> mtime of every file already ingested ({} on any failure)."""
    import sqlite3
    try:
        with closing(sqlite3.connect(_db_path())) as conn:
            _store.ensure(conn, "document_watch_seen")
            conn.commit()
            return {r[0]: r[1] for r in
                    conn.execute("SELECT path, mtime FROM document_watch_seen")}
    except Exception:
        return {}


def _mark_seen(key: str, mtime: float) -> None:
    """Record that `key` was ingested at this mtime. Never raises."""
    import sqlite3
    try:
        with closing(sqlite3.connect(_db_path())) as conn:
            conn.execute("INSERT OR REPLACE INTO document_watch_seen "
                         "(path, mtime, ingested) VALUES (?, ?, ?)",
                         (key, mtime, datetime.now(timezone.utc).replace(tzinfo=None).isoformat()))
            conn.commit()
    except Exception:
        pass


def _is_unchanged(seen: dict, key: str, mtime: float) -> bool:
    return key in seen and abs(seen[key] - mtime) < 1.0


def _new_docs_in_library(docs: Path, seen: dict) -> list[tuple[str, float, str]]:
    """(path, mtime, name) of each new or changed supported file in DOCS_DIR."""
    out = []
    for f in sorted(docs.iterdir()):
        try:
            if not f.is_file() or f.suffix.lower() not in _SUPPORTED:
                continue
            st = f.stat()
            if st.st_size > _MAX_FILE_MB * 1_000_000:
                continue
            key = str(f)
            if _is_unchanged(seen, key, st.st_mtime):
                continue                       # already ingested this version
            out.append((key, st.st_mtime, f.name))
        except Exception as exc:
            _LOGGER.debug("auto-ingest of %s failed: %s", f, exc)
    return out


def _new_docs_in_watch_folder(d: Path, seen: dict) -> list[tuple[str, float, str]]:
    """(path, mtime, name) of each new or changed supported file in one watch
    folder; [] when it is not a directory."""
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.suffix.lower() not in _SUPPORTED:
            continue
        try:
            mtime = f.stat().st_mtime
            if f.stat().st_size > _MAX_FILE_MB * 1_000_000:
                continue
        except Exception:
            continue
        key = str(f)
        if _is_unchanged(seen, key, mtime):
            continue          # already ingested this version
        out.append((key, mtime, f.name))
    return out


async def auto_ingest_new(hass) -> dict:
    """Ingest only NEW or CHANGED files in DOCS_DIR (v6.79.0).

    ingest_directory_async re-embeds the whole folder every call, which is fine
    for a manual rescan but would hammer Ollama if run on a timer. This variant
    tracks each file's mtime in document_watch_seen (the same table the watch
    folders use) and ingests a file only when it's new or its mtime changed —
    so dropping a manual into /config/nova/documents gets picked up
    automatically on the next scheduled scan, without re-embedding everything.
    Never raises."""
    docs = Path(_documents_dir())
    if not await hass.async_add_executor_job(docs.is_dir):
        return {"ok": True, "new_files": 0, "note": "docs dir absent"}

    seen = await hass.async_add_executor_job(_read_seen)
    pending = await hass.async_add_executor_job(_new_docs_in_library, docs, seen)

    results = []
    for key, mtime, name in pending:
        try:
            res = await save_and_ingest_upload_from_path(hass, key)
            results.append({"source": name, **res})
            if res.get("ok"):
                await hass.async_add_executor_job(_mark_seen, key, mtime)
        except Exception as exc:
            _LOGGER.debug("auto-ingest of %s failed: %s", key, exc)

    ingested = sum(1 for r in results if r.get("ok"))
    return {"ok": True, "new_files": ingested, "files": results}


async def scan_watch_folders(hass) -> dict:
    """Ingest any NEW supported files found in configured watch folders (e.g. a
    Downloads or Drive-sync folder). Tracks what's been seen so re-scans only
    pick up new files. Never raises."""
    folders = _watch_folders()
    if not folders:
        return {"ok": True, "watched": 0, "new_files": 0,
                "note": "no watch folders configured"}

    # remember ingested watch-file paths+mtimes in a tiny table
    seen = await hass.async_add_executor_job(_read_seen)

    new_results = []
    for folder in folders:
        try:
            pending = await hass.async_add_executor_job(
                _new_docs_in_watch_folder, Path(folder), seen)
            for key, mtime, name in pending:
                # ingest by copying into DOCS_DIR (keeps the library in one place)
                copied = await hass.async_add_executor_job(
                    _copy_into_docs, key)
                if not copied.get("ok"):
                    continue
                res = await save_and_ingest_upload_from_path(hass, copied["path"])
                new_results.append({"source": name, **res})
                await hass.async_add_executor_job(_mark_seen, key, mtime)
        except Exception as exc:
            _LOGGER.debug("watch scan of %s failed: %s", folder, exc)

    ingested = sum(1 for r in new_results if r.get("ok"))
    return {"ok": True, "watched": len(folders), "new_files": ingested,
            "files": new_results}


def _copy_into_docs(src_path: str) -> dict:
    """Copy a watched file into DOCS_DIR (sanitized name). Never raises."""
    import shutil
    safe = _safe_filename(os.path.basename(src_path))
    if not safe:
        return {"ok": False}
    try:
        d = Path(_documents_dir())
        d.mkdir(parents=True, exist_ok=True)
        dest = d / safe
        shutil.copy2(src_path, dest)
        return {"ok": True, "path": str(dest), "filename": safe}
    except Exception:
        return {"ok": False}


async def save_and_ingest_upload_from_path(hass, path: str) -> dict:
    """Ingest an already-placed file (used by watch-folder copy). Semantic-aware."""
    res = await hass.async_add_executor_job(ingest_file, path)
    out = {"ok": bool(res.get("ok")), "filename": os.path.basename(path),
           "chunks": res.get("chunks", 0)}
    if not res.get("ok"):
        out["error"] = res.get("error")
        return out
    try:
        from . import embeddings
        if embeddings.is_enabled() and res.get("chunk_texts"):
            await hass.async_add_executor_job(embeddings.init_store)
            vecs = await embeddings.embed_texts(hass, res["chunk_texts"])
            if vecs is not None:
                await hass.async_add_executor_job(
                    embeddings.forget_source, out["filename"])
                await hass.async_add_executor_job(
                    embeddings.store_vectors, out["filename"],
                    res["chunk_texts"], vecs, res.get("ingested", ""))
    except Exception:
        pass
    return out


async def search_documents_async(hass, query: str, k: int = 4) -> list[dict]:
    """Retrieve document chunks, preferring Ollama semantic search when enabled,
    falling back to keyword (FTS) otherwise (v6.57.0)."""
    from . import embeddings
    if embeddings.is_enabled():
        qvec = await embeddings.embed_one(hass, query)
        if qvec:
            hits = await hass.async_add_executor_job(
                embeddings.search_vectors, qvec, k)
            if hits:
                for h in hits:
                    h["engine"] = "semantic"
                return hits
    # fall back to keyword
    hits = search_documents(query, k)
    for h in hits:
        h["engine"] = "keyword"
    return hits


def ingest_directory(directory: Optional[str] = None) -> dict:
    """Ingest every supported file in the documents directory. Returns a
    summary with per-file results. Creates the directory if missing."""
    directory = directory or _documents_dir()
    _ensure_init()
    d = Path(directory)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return {"ok": False, "error": f"cannot access {directory}: {exc}",
                "files": [], "total_chunks": 0}

    results = []
    total_chunks = 0
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.suffix.lower() not in _SUPPORTED:
            continue
        try:
            if f.stat().st_size > _MAX_FILE_MB * 1_000_000:
                results.append({"source": f.name, "chunks": 0, "ok": False,
                                "error": f"larger than {_MAX_FILE_MB}MB — skipped"})
                continue
        except Exception:
            pass
        res = ingest_file(str(f))
        results.append(res)
        total_chunks += res.get("chunks", 0)

    ok_files = sum(1 for r in results if r.get("ok"))
    return {"ok": True, "files": results, "files_ingested": ok_files,
            "files_seen": len(results), "total_chunks": total_chunks,
            "directory": directory}


# ── retrieval ────────────────────────────────────────────────────────────────

def search_documents(query: str, k: int = 4) -> list[dict]:
    """Top-k document chunks for a query.
    Returns [{"text", "source", "chunk", "score"}]. Never raises."""
    if not query or not query.strip():
        return []
    _ensure_init()

    if _chroma_ok and _collection is not None:
        try:
            res = _collection.query(query_texts=[query], n_results=min(k, 20))
            out = []
            if res and res.get("documents"):
                docs = res["documents"][0]
                metas = res["metadatas"][0] if res.get("metadatas") else [{}] * len(docs)
                dists = res["distances"][0] if res.get("distances") else [0] * len(docs)
                for doc, meta, dist in zip(docs, metas, dists):
                    out.append({
                        "text": doc,
                        "source": meta.get("source", ""),
                        "chunk": meta.get("chunk", 0),
                        "score": round(1.0 - dist, 3),
                    })
            return out
        except Exception as exc:
            _LOGGER.debug("doc chroma search failed: %s", exc)

    if _fts_ok:
        try:
            import sqlite3
            conn = sqlite3.connect(_db_path())
            conn.row_factory = sqlite3.Row
            terms = " OR ".join(re.findall(r"\w+", query)[:8]) or query
            rows = conn.execute(
                "SELECT content, source, chunk_id, rank FROM document_fts "
                "WHERE document_fts MATCH ? ORDER BY rank LIMIT ?",
                (terms, k),
            ).fetchall()
            conn.close()
            return [{"text": r["content"], "source": r["source"],
                     "chunk": r["chunk_id"], "score": None} for r in rows]
        except Exception as exc:
            _LOGGER.debug("doc fts search failed: %s", exc)

    return []


def library_status() -> dict:
    """What's in the library — backends and counts, for status/UI. Never raises."""
    _ensure_init()
    info = {"chroma": _chroma_ok, "fts": _fts_ok, "directory": _documents_dir(),
            "chunk_count": 0, "sources": []}
    if _chroma_ok and _collection is not None:
        try:
            info["chunk_count"] = _collection.count()
            got = _collection.get(include=["metadatas"])
            srcs = {}
            for m in (got.get("metadatas") or []):
                s = m.get("source", "?")
                srcs[s] = srcs.get(s, 0) + 1
            info["sources"] = [{"source": s, "chunks": n}
                               for s, n in sorted(srcs.items())]
        except Exception as exc:
            _LOGGER.debug("doc status (chroma) failed: %s", exc)
    elif _fts_ok:
        try:
            import sqlite3
            conn = sqlite3.connect(_db_path())
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT source, COUNT(*) n FROM document_fts GROUP BY source"
            ).fetchall()
            conn.close()
            info["chunk_count"] = sum(r["n"] for r in rows)
            info["sources"] = [{"source": r["source"], "chunks": r["n"]} for r in rows]
        except Exception as exc:
            _LOGGER.debug("doc status (fts) failed: %s", exc)
    return info
