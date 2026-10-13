"""Local semantic search over the household's documents.

Embeddings come from the Ollama server Nova already uses (/api/embed, with the
legacy /api/embeddings as a fallback). Vectors are stored as float32 blobs in
the doc_vectors table of nova.db and compared with plain cosine similarity in
pure Python. Opt-in from Settings. When Ollama or the model is unreachable the
functions return None or empty so callers fall back to keyword search; nothing
here raises to the caller.
"""
from __future__ import annotations

import contextlib
import logging
import math
import struct
from typing import Any, Optional

from . import paths
from .persistence import sqlite as _store

_LOGGER = logging.getLogger(__name__)

_DB_PATH: Optional[str] = None        # test override
_DEFAULT_MODEL = "nomic-embed-text"
_TIMEOUT = 60
_EMBED_BATCH = 32

# Last embed failure reason, shown on the Settings status line. Mutated in
# place; never rebound.
_EMBED_LAST_ERROR: list[str] = [""]


def _db_path() -> str:
    return _DB_PATH or paths.nova_db()


def _cfg(key: str, default: Any) -> Any:
    try:
        from . import nova_config
        value = nova_config.get(key, default)
        return default if value is None else value
    except Exception:
        return default


def is_enabled() -> bool:
    """Semantic search is switched on and an Ollama host is known."""
    return bool(_cfg("semantic_search", False)) and bool(_ollama_base())


def _model() -> str:
    return str(_cfg("embed_model", _DEFAULT_MODEL)) or _DEFAULT_MODEL


def _ollama_base() -> Optional[str]:
    raw = (_cfg("embed_base_url", "") or _cfg("ollama_base_url", "")
           or _cfg("llm_base_url", ""))
    base = str(raw).strip().rstrip("/")
    if not base:
        return None
    if base.endswith("/v1"):
        base = base[:-3]
    return base


# ── vector math ─────────────────────────────────────────────────────────────

def _pack(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return -1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


# ── vector store ────────────────────────────────────────────────────────────

def _connect(row_factory: bool = True):
    return _store.connect(_db_path(), mkdir=False, busy_timeout_ms=None,
                          row_factory=row_factory)


@contextlib.contextmanager
def _db(row_factory: bool = True):
    """One short connection: committed if the block finishes, always closed."""
    conn = _connect(row_factory=row_factory)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_store() -> bool:
    """Create the doc_vectors table if needed."""
    try:
        with _db(row_factory=False) as conn:
            _store.ensure(conn, "doc_vectors")
    except Exception as exc:
        _LOGGER.warning("doc_vectors init failed: %s", exc)
        return False
    return True


def forget_source(source: str) -> None:
    """Remove every stored chunk of one document."""
    try:
        with _db() as conn:
            conn.execute("DELETE FROM doc_vectors WHERE source = ?", (source,))
    except Exception as exc:
        _LOGGER.debug("doc_vectors forget failed: %s", exc)


def store_vectors(source: str, chunks: list, vectors: list, ingested) -> int:
    """Append one row per chunk. Returns how many chunks were given."""
    if not chunks or not vectors or len(chunks) != len(vectors):
        return 0
    try:
        model = _model()
        with _db() as conn:
            conn.executemany(
                "INSERT INTO doc_vectors (source, chunk, content, dim, vec, model, ingested) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(source, n, text, len(vec), _pack(vec), model, ingested)
                 for n, (text, vec) in enumerate(zip(chunks, vectors))])
    except Exception as exc:
        _LOGGER.debug("doc_vectors store failed: %s", exc)
        return 0
    return len(chunks)


def vector_count() -> int:
    try:
        with _db() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM doc_vectors").fetchone()[0])
    except Exception:
        return 0


def search_vectors(query_vec, k: int = 4) -> list[dict]:
    """The k most similar stored chunks with a positive score."""
    if not query_vec:
        return []
    try:
        with _db() as conn:
            stored = conn.execute(
                "SELECT source, chunk, content, vec FROM doc_vectors").fetchall()
    except Exception as exc:
        _LOGGER.debug("doc_vectors read failed: %s", exc)
        return []
    scored = [(_cosine(query_vec, _unpack(r["vec"])), r) for r in stored]
    scored.sort(key=lambda s: s[0], reverse=True)
    return [{"text": r["content"], "source": r["source"], "chunk": r["chunk"],
             "score": round(score, 3)}
            for score, r in scored[:k] if score > 0]


# ── embedding over HTTP ─────────────────────────────────────────────────────

def _record_health(ok: bool, detail: str = "") -> None:
    try:
        from .diagnostics.service_health import record_usage
        record_usage("embeddings", ok, detail)
    except Exception:
        pass


async def _embed_batch(session, base: str, model: str,
                       batch: list) -> Optional[list[list[float]]]:
    """Embed one batch: /api/embed first, then one text at a time through the
    legacy /api/embeddings."""
    import aiohttp

    reason = ""
    try:
        async with session.post(f"{base}/api/embed",
                                json={"model": model, "input": batch},
                                timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                embs = data.get("embeddings")
                if embs and len(embs) == len(batch):
                    return embs
                err = (data or {}).get("error")
                if err:
                    reason = f"model '{model}' returned no embeddings ({err})"
                else:
                    reason = (f"model '{model}' returned no embeddings — is "
                              f"'{model}' pulled? run: ollama pull {model}")
            elif resp.status == 404:
                reason = (f"model '{model}' not found on Ollama (404) — "
                          f"run: ollama pull {model}")
            else:
                try:
                    body = (await resp.text())[:120]
                except Exception:
                    body = ""
                reason = f"Ollama /api/embed HTTP {resp.status}: {body}"
                _LOGGER.debug("Embed batch failed: %s", reason)
    except Exception as exc:
        reason = f"Ollama /api/embed unreachable: {str(exc)[:100]}"
        _LOGGER.debug("Embed batch error: %s", exc)

    out: list[list[float]] = []
    for text in batch:
        try:
            async with session.post(f"{base}/api/embeddings",
                                    json={"model": model, "prompt": text},
                                    timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as resp:
                if resp.status != 200:
                    if resp.status == 404 and not reason:
                        reason = f"model '{model}' not found — run: ollama pull {model}"
                    _EMBED_LAST_ERROR[0] = reason or f"HTTP {resp.status}"
                    return None
                emb = (await resp.json(content_type=None)).get("embedding")
                if not emb:
                    _EMBED_LAST_ERROR[0] = reason or f"model '{model}' returned no embedding"
                    return None
                out.append(emb)
        except Exception as exc:
            _LOGGER.debug("Legacy embed error: %s", exc)
            _EMBED_LAST_ERROR[0] = reason or f"unreachable: {str(exc)[:100]}"
            return None
    return out


async def embed_texts(hass, texts: list,
                      _is_probe: bool = False) -> Optional[list[list[float]]]:
    """Embed texts in batches; None when anything fails."""
    base = _ollama_base()
    if not base or not texts:
        return None
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    model = _model()
    vectors: list[list[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH):
        got = await _embed_batch(session, base, model, texts[i:i + _EMBED_BATCH])
        if got is None:
            if not _is_probe:
                _record_health(False, _EMBED_LAST_ERROR[0]
                               or "Ollama embed call returned no vectors")
            return None
        vectors.extend(got)
    if not _is_probe:
        _EMBED_LAST_ERROR[0] = ""
        _record_health(True)
    return vectors


async def embed_one(hass, text: str,
                    _is_probe: bool = False) -> Optional[list[float]]:
    res = await embed_texts(hass, [text], _is_probe=_is_probe)
    return res[0] if res else None


async def probe(hass) -> dict:
    """Check that Ollama returns an embedding with the configured model."""
    base = _ollama_base()
    if not base:
        return {"ok": False,
                "error": "no Ollama base URL configured (set llm_base_url to your Ollama host)"}
    vec = await embed_one(hass, "nova embedding health check", _is_probe=True)
    if not vec:
        return {"ok": False, "model": _model(), "base": base,
                "error": _EMBED_LAST_ERROR[0]
                or ("Ollama did not return an embedding — is the embed model "
                    "pulled? Try: ollama pull " + _model())}
    return {"ok": True, "model": _model(), "base": base, "dim": len(vec)}
