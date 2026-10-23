"""
Nova Long-Term Memory (v5.7.00).

Provides semantic search across past conversations so Nova can recall
context from previous interactions, using SQLite FTS5 keyword search.

Usage:
  - store_memory(text, metadata) — save a conversation turn
  - search_memory(query, k) — retrieve k most relevant past memories
  - get_conversation_context(query) — formatted context string for prompt injection

Storage: the memory_fts table in nova.db

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

import contextlib
import logging
from datetime import datetime, timezone
from typing import Optional

from .persistence import sqlite as _store
from . import paths

_LOGGER = logging.getLogger(__name__)

DB_PATH: Optional[str] = None  # override; None resolves via paths.py


def _db_path() -> str:
    return DB_PATH or paths.nova_db()


_fts_available = False


# ── SQLite FTS5 ─────────────────────────────────────────────────────

def _init_fts():
    """Initialize FTS5 virtual table in the existing Nova database."""
    global _fts_available
    try:
        db_path = _db_path()
        with contextlib.closing(_store.connect(db_path, row_factory=False, mkdir=False,
                                               busy_timeout_ms=None)) as conn:
            _store.ensure(conn, "memory_fts")
            conn.commit()
        _fts_available = True
        _LOGGER.info("Nova memory: FTS5 initialized")
        return True
    except Exception as exc:
        _LOGGER.warning("Nova memory: FTS5 init failed: %s", exc)
        return False


def _ensure_initialized():
    """Lazy init of the FTS5 table."""
    if not _fts_available:
        _init_fts()


# ── Public API ───────────────────────────────────────────────────────────────

def store_memory(text: str, *, role: str = "user", device_id: str = "",
                 conversation_id: str = "", subject: Optional[str] = None,
                 turn_id: Optional[str] = None) -> bool:
    """
    Keep one conversation turn in long-term memory. conversation.py calls
    this after every user and assistant message. True means it was stored.

    `subject` (Phase 2) names the person the turn belongs to, so continuity
    can follow them when the conversation_id changes. `turn_id` (Phase 2) is
    the conversations.id of the user message that started the exchange, as a
    string. Both halves of an exchange share it, which lets retrieval pair
    them. Each goes into the metadata only when it has a real value and is
    never written as null, so older records, which have neither key, read
    back exactly as they did.
    """
    if not text or len(text.strip()) < 5:
        return False

    _ensure_initialized()
    stamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    meta = {
        "role": role,
        "device_id": device_id or "",
        "conversation_id": conversation_id or "",
        "timestamp": stamp,
    }
    if subject:
        meta["subject"] = subject
    if turn_id:
        meta["turn_id"] = str(turn_id)

    if not _fts_available:
        return False
    try:
        import json
        with contextlib.closing(_store.connect(_db_path(), row_factory=False, mkdir=False,
                                               busy_timeout_ms=None)) as conn:
            conn.execute(
                "INSERT INTO memory_fts (content, metadata, timestamp) VALUES (?, ?, ?)",
                (text, json.dumps(meta), stamp),
            )
            conn.commit()
    except Exception as exc:
        _LOGGER.debug("FTS5 store failed: %s", exc)
        return False
    return True


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


def _hit_from_row(row, conversation_id: Optional[str],
                  subject: Optional[str]) -> Optional[dict]:
    """The search hit for one FTS row, or None when its stored metadata puts it
    outside the requested conversation or subject. The check here is the
    authoritative one; the SQL LIKE pre-filter only narrows the candidates."""
    import json
    try:
        meta = json.loads(row["metadata"])
    except Exception:
        meta = {}
    if conversation_id and meta.get("conversation_id") != conversation_id:
        return None
    if subject and meta.get("subject") != subject:
        return None
    rank = row["rank"]
    return {
        "text": row["content"],
        "role": meta.get("role", ""),
        "timestamp": meta.get("timestamp", ""),
        "score": round(abs(rank) * 0.1, 3) if rank else 0,
        "conversation_id": meta.get("conversation_id") or None,
        "subject": meta.get("subject") or None,
        "turn_id": meta.get("turn_id") or None,
    }


def search_memory(
    query: str,
    k: int = 5,
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

    if _fts_available:
        try:
            with contextlib.closing(_store.connect(_db_path(), mkdir=False,
                                                   busy_timeout_ms=None)) as conn:
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

            memories = []
            for row in rows:
                hit = _hit_from_row(row, conversation_id, subject)
                if hit is None:
                    continue
                memories.append(hit)
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

    if _fts_available:
        try:
            import json
            with contextlib.closing(_store.connect(_db_path(), mkdir=False,
                                                   busy_timeout_ms=None)) as conn:
                rows = conn.execute(
                    "SELECT content, metadata FROM memory_fts WHERE metadata LIKE ? ESCAPE '\\'",
                    (_metadata_like_pattern("turn_id", turn_id),),
                ).fetchall()
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
    single-message format — never invented or summarised. Raw search results
    are over-fetched at 2*k so that up to k pairs can collapse without
    reducing the number of distinct exchanges returned below k when more
    exist; the output is still capped at k distinct exchanges/records.

    The returned string is fenced against prompt injection
    (_fence_retrieved_text) exactly once, after all formatting/pairing is
    done, since it re-enters the conversation with no other framing at all.
    """
    # Over-fetch raw results: a paired exchange consumes TWO raw slots (the
    # user half and the assistant half) but counts as ONE final result below,
    # so requesting only `k` raw hits could under-fill the k-exchange output
    # whenever pairs collapse — fewer than k distinct exchanges returned even
    # though more exist. At most two records ever share one turn_id, so 2*k
    # is always sufficient headroom, regardless of how many of the eventual
    # k exchanges turn out to be paired. This does not add a second search
    # pass — still one call (or the same conversation-then-subject fallback
    # pair of calls as before) — only the requested count changes; search
    # results remain sorted by relevance/rank, so the top-k prefix a caller
    # ultimately sees is unaffected, just backed by a larger candidate pool.
    fetch_k = k * 2
    memories = search_memory(query, k=fetch_k, conversation_id=conversation_id)
    if not memories and subject:
        memories = search_memory(query, k=fetch_k, subject=subject)
    if not memories:
        return ""

    parts = ["## Relevant past conversations"]
    seen_turn_ids: set[str] = set()
    exchange_count = 0
    for m in memories:
        if exchange_count >= k:
            break  # at most k distinct exchanges/legacy records in the output
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
        exchange_count += 1

    return _fence_retrieved_text("\n".join(parts))


def get_memory_stats() -> dict:
    """Memory figures for the panel: which backend answers and how many
    memories it holds."""
    _ensure_initialized()
    report = {"backend": "none", "total_memories": 0}
    if not _fts_available:
        return report
    try:
        with contextlib.closing(_store.connect(_db_path(), row_factory=False, mkdir=False,
                                               busy_timeout_ms=None)) as conn:
            counted = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()
    except Exception:
        return report
    report["backend"] = "fts5"
    report["total_memories"] = counted[0] if counted else 0
    return report
