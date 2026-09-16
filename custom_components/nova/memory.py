"""
Nova Long-Term Memory (v5.7.00).

Provides semantic search across past conversations so Nova can recall
context from previous interactions. Uses ChromaDB when available, falls
back to SQLite FTS5 keyword search.

Usage:
  - store_memory(text, metadata) — save a conversation turn
  - search_memory(query, k) — retrieve k most relevant past memories
  - get_conversation_context(query) — formatted context string for prompt injection

Storage: /config/nova_memory/ (ChromaDB) or nova.db FTS table (fallback)

Scoped + fenced (v7.87.0, backlog #1 follow-up): search_memory() and
get_conversation_context() now take a conversation_id to scope retrieval to
the asking conversation's own stored turns — store_memory() already recorded
one per turn but nothing ever filtered by it, so retrieval searched every
household member's history regardless of who was asking. And the retrieved
text — which re-enters the LLM call spliced straight into the persona/system
prompt with zero framing at all, not even a "this is historical" note — is
now wrapped between a random per-call delimiter with a hardened instruction
that it's inert data, never a live command. Same defence, same reasoning as
memory_thread.py's reseed fencing; this is a different code path (semantic
search vs. cross-session reseed) with the identical shape of gap.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger(__name__)

MEMORY_DIR = "/config/nova_memory"
DB_PATH = "/config/nova.db"
_chromadb_available = False
_collection = None
_fts_available = False


# ── ChromaDB initialization ─────────────────────────────────────────────────

def _init_chromadb():
    """Try to initialize ChromaDB. Returns True on success."""
    global _chromadb_available, _collection
    try:
        import chromadb
        os.makedirs(MEMORY_DIR, exist_ok=True)
        client = chromadb.PersistentClient(path=MEMORY_DIR)
        _collection = client.get_or_create_collection(
            name="nova_memory",
            metadata={"hnsw:space": "cosine"},
        )
        _chromadb_available = True
        _LOGGER.info("Nova memory: ChromaDB initialized at %s", MEMORY_DIR)
        return True
    except ImportError:
        _LOGGER.debug("Nova memory: ChromaDB not available, using FTS5 fallback")
        return False
    except Exception as exc:
        _LOGGER.warning("Nova memory: ChromaDB init failed: %s", exc)
        return False


# ── SQLite FTS5 fallback ─────────────────────────────────────────────────────

def _init_fts():
    """Initialize FTS5 virtual table in the existing Nova database."""
    global _fts_available
    try:
        import sqlite3
        db_path = DB_PATH
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
            USING fts5(content, metadata, timestamp)
        """)
        conn.commit()
        conn.close()
        _fts_available = True
        _LOGGER.info("Nova memory: FTS5 fallback initialized")
        return True
    except Exception as exc:
        _LOGGER.warning("Nova memory: FTS5 init failed: %s", exc)
        return False


def _ensure_initialized():
    """Lazy init — try ChromaDB first, then FTS5."""
    global _chromadb_available, _fts_available
    if _chromadb_available or _fts_available:
        return
    if not _init_chromadb():
        _init_fts()


# ── Public API ───────────────────────────────────────────────────────────────

def store_memory(
    text: str,
    *,
    role: str = "user",
    device_id: str = "",
    conversation_id: str = "",
    subject: Optional[str] = None,
    turn_id: Optional[str] = None,
) -> bool:
    """
    Store a conversation turn in long-term memory.
    Called from conversation.py after each user/assistant message.

    `subject` (Phase 2) — the resolved person this turn belongs to, for
    person-scoped continuity across a conversation_id change. `turn_id`
    (Phase 2) — the triggering user message's conversations.id (as a
    string), shared by both halves of one exchange, for coherent pairing on
    retrieval. Both are added to metadata ONLY when a real value is given —
    never written as null/None, so legacy records (neither key present)
    keep reading back exactly as before.
    """
    if not text or len(text.strip()) < 5:
        return False

    _ensure_initialized()
    ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    doc_id = f"{ts}_{role}_{hash(text) % 100000}"

    metadata = {
        "role": role,
        "device_id": device_id or "",
        "conversation_id": conversation_id or "",
        "timestamp": ts,
    }
    if subject:
        metadata["subject"] = subject
    if turn_id:
        metadata["turn_id"] = str(turn_id)

    if _chromadb_available and _collection is not None:
        try:
            _collection.add(
                documents=[text],
                metadatas=[metadata],
                ids=[doc_id],
            )
            return True
        except Exception as exc:
            _LOGGER.debug("ChromaDB store failed: %s", exc)

    if _fts_available:
        try:
            import sqlite3, json
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO memory_fts (content, metadata, timestamp) VALUES (?, ?, ?)",
                (text, json.dumps(metadata), ts),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as exc:
            _LOGGER.debug("FTS5 store failed: %s", exc)

    return False


def _like_escape(value: str) -> str:
    """Escape SQL LIKE wildcards (and the escape char itself) in a literal
    substring, so `%`/`_` inside the value are matched literally, not as
    wildcards. Pair with `LIKE ? ESCAPE '\\'`."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _metadata_like_pattern(key: str, value: str) -> str:
    """Build a LIKE pattern matching the exact JSON-serialised `"<key>":
    <value>` fragment inside memory_fts.metadata. Built from json.dumps() so
    embedded quotes/backslashes in `value` are escaped exactly as
    store_memory()'s own json.dumps(metadata) would encode them, then
    LIKE-escaped so a literal `%`/`_` in the value can't be misread as a
    wildcard. The fragment is context-independent (same substring regardless
    of key order/position in the object) — verified in
    tests/unit/test_memory.py against adversarial values (embedded quote,
    backslash, %, _) and against >25 higher-ranked decoy rows from another
    subject/turn_id."""
    import json
    fragment = f'"{key}": {json.dumps(value)}'
    return "%" + _like_escape(fragment) + "%"


def search_memory(
    query: str,
    k: int = 5,
    hours: Optional[int] = None,
    conversation_id: Optional[str] = None,
    subject: Optional[str] = None,
) -> list[dict]:
    """
    Search long-term memory for relevant past conversations.

    `conversation_id` (v7.87.0) scopes results to turns stored under that
    same id — the first-choice scope. `subject` (Phase 2) is a person-scoped
    fallback filter — callers use at most ONE of the two per call; this
    function does not combine them (see get_conversation_context, which
    orchestrates conversation-id-first-then-subject-fallback as two
    separate calls, never a union).

    Returns list of {"text", "role", "timestamp", "score", "conversation_id",
    "subject", "turn_id"} — the last three are the record's own stored
    metadata values (None when absent), used by get_conversation_context to
    validate and pair exchange siblings.
    """
    if not query:
        return []

    _ensure_initialized()

    if _chromadb_available and _collection is not None:
        try:
            conditions = []
            if hours:
                cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)).isoformat()
                conditions.append({"timestamp": {"$gte": cutoff}})
            if conversation_id:
                conditions.append({"conversation_id": conversation_id})
            if subject:
                conditions.append({"subject": subject})
            where = None
            if len(conditions) == 1:
                where = conditions[0]
            elif len(conditions) > 1:
                where = {"$and": conditions}

            results = _collection.query(
                query_texts=[query],
                n_results=min(k, 20),
                where=where,
            )

            memories = []
            if results and results.get("documents"):
                docs = results["documents"][0]
                metas = results["metadatas"][0] if results.get("metadatas") else [{}] * len(docs)
                dists = results["distances"][0] if results.get("distances") else [0] * len(docs)
                for doc, meta, dist in zip(docs, metas, dists):
                    memories.append({
                        "text": doc,
                        "role": meta.get("role", ""),
                        "timestamp": meta.get("timestamp", ""),
                        "score": round(1.0 - dist, 3),  # cosine distance → similarity
                        "conversation_id": meta.get("conversation_id") or None,
                        "subject": meta.get("subject") or None,
                        "turn_id": meta.get("turn_id") or None,
                    })
            return memories
        except Exception as exc:
            _LOGGER.debug("ChromaDB search failed: %s", exc)

    if _fts_available:
        try:
            import sqlite3, json
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            # FTS5 MATCH query
            query_clean = " OR ".join(query.split()[:8])  # limit query terms
            # FTS5 can't filter into the JSON metadata column directly, so
            # when conversation_id-scoping is requested, over-fetch and
            # filter in Python, then truncate to k — unchanged from before
            # Phase 2 (out of scope to rewrite here).
            #
            # subject-scoping is different: proven empirically (see
            # tests/unit/test_memory.py) that this same over-fetch approach
            # can EXCLUDE the target subject's row when 25+ higher-ranked
            # rows belong to another subject — the SQL LIMIT truncates
            # before the Python filter ever runs. So subject additionally
            # gets a safely-escaped metadata LIKE pre-filter combined with
            # the MATCH query, narrowing candidates BEFORE the rank-based
            # LIMIT. The LIKE result is never trusted on its own — the
            # Python-side equality check below is still authoritative.
            fetch_limit = max(k * 5, 25) if (conversation_id or subject) else k
            sql = "SELECT content, metadata, rank FROM memory_fts WHERE memory_fts MATCH ?"
            params: list = [query_clean]
            if subject:
                sql += " AND metadata LIKE ? ESCAPE '\\'"
                params.append(_metadata_like_pattern("subject", subject))
            sql += " ORDER BY rank LIMIT ?"
            params.append(fetch_limit)
            rows = conn.execute(sql, params).fetchall()
            conn.close()

            memories = []
            for row in rows:
                meta = {}
                try:
                    meta = json.loads(row["metadata"])
                except Exception:
                    pass
                if conversation_id and meta.get("conversation_id") != conversation_id:
                    continue
                if subject and meta.get("subject") != subject:
                    continue
                memories.append({
                    "text": row["content"],
                    "role": meta.get("role", ""),
                    "timestamp": meta.get("timestamp", ""),
                    "score": round(abs(row["rank"]) * 0.1, 3) if row["rank"] else 0,
                    "conversation_id": meta.get("conversation_id") or None,
                    "subject": meta.get("subject") or None,
                    "turn_id": meta.get("turn_id") or None,
                })
                if len(memories) >= k:
                    break
            return memories
        except Exception as exc:
            _LOGGER.debug("FTS5 search failed: %s", exc)

    return []


def _find_sibling(
    turn_id: str,
    hit_conversation_id: Optional[str],
    hit_subject: Optional[str],
    hit_role: str,
) -> Optional[dict]:
    """Look up the opposite-role record sharing this turn_id — validated
    against the ORIGINAL HIT's own stored conversation_id/subject, never the
    live request's current conversation_id (a subject-scoped fallback hit
    may legitimately come from an older, different conversation than the
    one live right now). turn_id alone locates candidates — it's derived
    from conversations.id, globally unique, so it needs no conversation_id
    to narrow the search; conversation_id/subject/role are the defensive
    privacy check applied to whatever turn_id finds. Never queries the
    conversations table — works even after the anchor row is purged, since
    everything needed lives in the semantic-memory metadata itself."""
    _ensure_initialized()

    if _chromadb_available and _collection is not None:
        try:
            res = _collection.get(where={"turn_id": turn_id})
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            for doc, meta in zip(docs, metas):
                if (meta.get("conversation_id") == hit_conversation_id
                        and meta.get("subject") == hit_subject
                        and meta.get("role")
                        and meta.get("role") != hit_role):
                    return {"text": doc, "role": meta.get("role", ""),
                            "timestamp": meta.get("timestamp", "")}
        except Exception as exc:
            _LOGGER.debug("ChromaDB sibling lookup failed: %s", exc)

    if _fts_available:
        try:
            import sqlite3, json
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT content, metadata FROM memory_fts WHERE metadata LIKE ? ESCAPE '\\'",
                (_metadata_like_pattern("turn_id", turn_id),),
            ).fetchall()
            conn.close()
            for row in rows:
                try:
                    meta = json.loads(row["metadata"])
                except Exception:
                    continue
                if (meta.get("turn_id") == turn_id
                        and meta.get("conversation_id") == hit_conversation_id
                        and meta.get("subject") == hit_subject
                        and meta.get("role")
                        and meta.get("role") != hit_role):
                    return {"text": row["content"], "role": meta.get("role", ""),
                            "timestamp": meta.get("timestamp", "")}
        except Exception as exc:
            _LOGGER.debug("FTS5 sibling lookup failed: %s", exc)

    return None


def _fence_retrieved_text(content: str, *, _token: str | None = None) -> str:
    """Wrap retrieved memory text with a random per-call delimiter and a
    hardened anti-injection instruction (v7.87.0, consolidated into
    prompt_fence.py at v7.89.0) — same defence, same reasoning as
    memory_thread.format_seed_message's reseed fencing, applied here because
    get_conversation_context's output is spliced straight into the
    persona/system prompt with NO framing at all otherwise, not even a
    "this is historical" note. `_token` is test-only for a deterministic
    delimiter; production callers never pass it."""
    from .prompt_fence import fence
    return fence(
        content,
        label="MEMORY",
        noun="are snippets retrieved from past conversations",
        callback_noun="retrieved text",
        _token=_token,
    )


def _format_memory_line(m: dict) -> str:
    ts = m.get("timestamp", "")[:16]  # trim seconds
    role = m.get("role", "")
    text = m.get("text", "")[:300]  # cap length
    return f"[{ts}] {role}: {text}"


def get_conversation_context(query: str, k: int = 3,
                             conversation_id: Optional[str] = None,
                             subject: Optional[str] = None) -> str:
    """
    Format retrieved memories as a context string for the system prompt.
    Called from conversation.py before each LLM call.

    `conversation_id` (v7.87.0) scopes retrieval to the asking conversation's
    own history — the first-choice scope. `subject` (Phase 2) is a
    person-scoped fallback, tried only when the conversation-id-scoped
    search returns nothing usable, and only when `subject` is a confidently
    resolved person (never for an unresolved identity or the shared
    "primary" bucket — callers pass None in both those cases, and this
    function has no way to distinguish "unresolved" from "primary" from
    "no subject given" because it never sees that distinction — it only
    ever receives a real subject string or None). The two scopes are never
    combined into one search.

    Coherent exchange pairing (Phase 2): when a hit carries a `turn_id`, its
    opposite-role sibling (if any) is looked up and formatted together, in
    chronological order, counted as ONE result. Duplicate hits from both
    halves of the same exchange collapse to one pairing. A hit with no
    turn_id, or whose sibling can't be found, falls back to the existing
    single-message format — never invented or summarised.

    The returned string is fenced against prompt injection
    (_fence_retrieved_text) exactly once, after all formatting/pairing is
    done, since it re-enters the conversation with no other framing at all.
    """
    memories = search_memory(query, k=k, conversation_id=conversation_id)
    if not memories and subject:
        memories = search_memory(query, k=k, subject=subject)
    if not memories:
        return ""

    parts = ["## Relevant past conversations"]
    seen_turn_ids: set[str] = set()
    for m in memories:
        turn_id = m.get("turn_id")
        if turn_id and turn_id in seen_turn_ids:
            continue  # already emitted as part of an earlier pair this call
        sibling = None
        if turn_id:
            seen_turn_ids.add(turn_id)
            sibling = _find_sibling(
                turn_id,
                hit_conversation_id=m.get("conversation_id"),
                hit_subject=m.get("subject"),
                hit_role=m.get("role", ""),
            )
        if sibling:
            pair = [m, sibling] if m.get("role") == "user" else [sibling, m]
            for entry in pair:
                parts.append(_format_memory_line(entry))
        else:
            parts.append(_format_memory_line(m))

    return _fence_retrieved_text("\n".join(parts))


def get_memory_stats() -> dict:
    """Return memory system stats for the panel."""
    _ensure_initialized()
    stats = {
        "backend": "none",
        "total_memories": 0,
    }

    if _chromadb_available and _collection is not None:
        try:
            stats["backend"] = "chromadb"
            stats["total_memories"] = _collection.count()
        except Exception:
            pass
    elif _fts_available:
        try:
            import sqlite3
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()
            conn.close()
            stats["backend"] = "fts5"
            stats["total_memories"] = row[0] if row else 0
        except Exception:
            pass

    return stats
