"""Meaning based recall for saved facts (opt in, with semantic search).

Keyword recall misses paraphrase: "who runs cold at night" shares no word with
a fact saved as "sleep temperature: 17C". When semantic search is on (the same
Ollama embedding setting documents use), saved facts are also matched by
meaning, mixed with the existing keyword, salience and recency score.

Rules this keeps:

  * Confirmed facts only. A pending fact is never embedded, ranked or shown.
  * Same fencing as keyword recall (the result goes through
    ``knowledge.format_block``).
  * Embedding happens in the background. The only call on the chat path is the
    one that embeds the question, and it has a short timeout.
  * One vector per fact, stored with its model, size and a hash of the text, so
    an edited fact or a changed embedding model is simply stale and re-embedded.
    A stale vector is never used.
  * Any failure means "no semantic result" and the caller uses keyword recall.
    After a failure semantic recall pauses for a couple of minutes, so a slow or
    absent Ollama cannot slow every message.
  * A forgotten or expired fact takes its vector with it (database trigger).

The fact text goes only to the Ollama host the user already configured.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Optional

from . import knowledge

_LOGGER = logging.getLogger(__name__)

QUERY_TIMEOUT_S = 3.0        # longest the chat path waits to embed a question
BREAKER_S = 120.0            # pause semantic recall this long after a failure
SEM_FLOOR = 0.55             # cosine below this adds nothing
SEM_WEIGHT = 4.0             # most a perfect match adds to the keyword score
INDEX_LIMIT = 100            # facts embedded per background pass
KICK_INTERVAL_S = 60.0       # at most one background pass a minute

_STATE = {"fail_until": 0.0, "indexing": False, "last_kick": 0.0}


def enabled() -> bool:
    try:
        from . import embeddings
        return embeddings.is_enabled()
    except Exception:
        return False


def _model() -> str:
    from . import embeddings
    return embeddings._model()


def _text(fact) -> str:
    return f"{fact['key']}: {fact['value']}"


def _hash(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\n{text}".encode("utf-8")).hexdigest()[:16]


def _breaker_open(now: Optional[float] = None) -> bool:
    return (now if now is not None else time.time()) < _STATE["fail_until"]


def _trip(now: Optional[float] = None) -> None:
    _STATE["fail_until"] = (now if now is not None else time.time()) + BREAKER_S


def reset() -> None:
    """Clear module state (used by tests)."""
    _STATE.update(fail_until=0.0, indexing=False, last_kick=0.0)


# ── blocking database helpers (call from the executor) ───────────────────────

def _stale_facts(model: str, limit: int, now: float) -> list[dict]:
    """Confirmed, live facts with no fresh vector for ``model``. Newest first."""
    conn = knowledge._connect()
    if conn is None:
        return []
    try:
        rows = knowledge._live_rows(conn, None, now, None, "confirmed")
        have = {r["fact_id"]: (r["model"], r["text_hash"]) for r in conn.execute(
            "SELECT fact_id, model, text_hash FROM fact_vectors")}
        stale = []
        for r in rows:
            f = knowledge._row_to_fact(r)
            if have.get(f["id"]) != (model, _hash(model, _text(f))):
                stale.append(f)
        stale.sort(key=lambda f: f["updated_at"], reverse=True)
        return stale[:max(0, limit)]
    except Exception as exc:
        _LOGGER.debug("fact_recall: stale lookup failed: %s", exc)
        return []
    finally:
        conn.close()


def _store_vectors(model: str, items: list) -> int:
    """items: (fact_id, text_hash, vector). Skips facts deleted meanwhile."""
    from . import embeddings
    conn = knowledge._connect()
    if conn is None:
        return 0
    stored = 0
    try:
        with conn:
            for fact_id, text_hash, vec in items:
                cur = conn.execute(
                    "INSERT OR REPLACE INTO fact_vectors "
                    "(fact_id, model, dim, vec, text_hash, updated_at) "
                    "SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS "
                    "(SELECT 1 FROM facts WHERE id = ?)",
                    (fact_id, model, len(vec), embeddings._pack(vec), text_hash,
                     time.time(), fact_id))
                stored += max(0, cur.rowcount)
        return stored
    except Exception as exc:
        _LOGGER.debug("fact_recall: storing vectors failed: %s", exc)
        return stored
    finally:
        conn.close()


def _rank(q_vec: list, query: str, model: str, subjects, subject, now: float,
          k: int) -> list[dict]:
    """Confirmed facts in scope, ranked by the keyword score plus a meaning
    bonus from fresh vectors. A fact needs a keyword hit or a meaning match."""
    from . import embeddings
    conn = knowledge._connect()
    if conn is None:
        return []
    try:
        rows = knowledge._live_rows(conn, subject, now, subjects, "confirmed")
        vecs = {r["fact_id"]: r for r in conn.execute(
            "SELECT fact_id, model, dim, vec, text_hash FROM fact_vectors")}
        q_tokens = knowledge._tokens(query)
        scored = []
        for r in rows:
            f = knowledge._row_to_fact(r)
            overlap = len(q_tokens & knowledge._tokens(f["key"] + " " + f["value"]))
            match = (overlap / len(q_tokens)) if q_tokens else 0.0
            sem = 0.0
            v = vecs.get(f["id"])
            if (v is not None and v["model"] == model
                    and v["dim"] == len(q_vec)
                    and v["text_hash"] == _hash(model, _text(f))):
                cos = embeddings._cosine(q_vec, embeddings._unpack(v["vec"]))
                if cos >= SEM_FLOOR:
                    sem = min(SEM_WEIGHT,
                              SEM_WEIGHT * (cos - SEM_FLOOR) / (1.0 - SEM_FLOOR))
            if overlap == 0 and sem <= 0.0:
                continue
            age_days = max(0.0, (now - f["updated_at"]) / 86400.0)
            score = (3.0 * match + f["salience"] * f["confidence"]
                     + 0.5 / (1.0 + age_days) + sem)
            scored.append((score, f))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [f for _, f in scored[:max(1, k)]]
    except Exception as exc:
        _LOGGER.debug("fact_recall: ranking failed: %s", exc)
        return []
    finally:
        conn.close()


# ── background indexing ──────────────────────────────────────────────────────

async def async_index(hass, limit: int = INDEX_LIMIT) -> int:
    """Embed confirmed facts that have no fresh vector. Returns how many were
    stored. Single flight. Never raises."""
    if _STATE["indexing"] or not enabled():
        return 0
    _STATE["indexing"] = True
    try:
        from . import embeddings
        model = _model()
        stale = await hass.async_add_executor_job(
            _stale_facts, model, limit, time.time())
        if not stale:
            return 0
        vectors = await embeddings.embed_texts(hass, [_text(f) for f in stale])
        if not vectors or len(vectors) != len(stale):
            _trip()
            return 0
        items = [(f["id"], _hash(model, _text(f)), vec)
                 for f, vec in zip(stale, vectors)]
        return int(await hass.async_add_executor_job(_store_vectors, model, items))
    except Exception as exc:
        _LOGGER.debug("fact_recall: indexing failed: %s", exc)
        return 0
    finally:
        _STATE["indexing"] = False


def _kick(hass) -> None:
    """Start a background indexing pass, at most once a minute."""
    now = time.time()
    if _STATE["indexing"] or now - _STATE["last_kick"] < KICK_INTERVAL_S:
        return
    _STATE["last_kick"] = now
    try:
        hass.async_create_task(async_index(hass))
    except Exception as exc:
        _LOGGER.debug("fact_recall: could not start indexing: %s", exc)


# ── chat path ────────────────────────────────────────────────────────────────

async def semantic_prompt_block(hass, query: str, *, subjects: Optional[list] = None,
                                subject: Optional[str] = None, limit: int = 12,
                                now: Optional[float] = None) -> Optional[str]:
    """The fenced facts block ranked by meaning, or None when semantic recall is
    off, paused, failed or found nothing. None means "use keyword recall"."""
    if not query or not enabled():
        return None
    now = now if now is not None else time.time()
    if _breaker_open(now):
        return None
    try:
        from . import embeddings
        _kick(hass)
        q_vec = await asyncio.wait_for(embeddings.embed_one(hass, query),
                                       timeout=QUERY_TIMEOUT_S)
        if not q_vec:
            _trip(now)
            return None
        facts = await hass.async_add_executor_job(
            _rank, q_vec, query, _model(), subjects, subject, now, limit)
        _STATE["fail_until"] = 0.0
        return knowledge.format_block(facts) if facts else None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOGGER.debug("fact_recall: semantic recall skipped: %s", exc)
        _trip(now)
        return None
