"""
Nova Knowledge — curated semantic memory (v6.25.0).

This is the *semantic* memory layer: durable, curated facts and preferences that
Nova knows and can reason over — distinct from two stores that already exist:

  • memory.py        episodic transcript recall ("what we said before")
  • patterns.db      high-volume behavioural telemetry ("what tends to happen")

knowledge.py holds the low-volume, high-value middle: discrete facts a butler
would simply *know* — "trash is Tuesday", "Username runs cold at night", "Username3's
pickup is 3 PM today". Each is attributed to a subject so per-person identity can
slot in later untouched, carries a source + confidence so observed/inferred facts
rank below stated ones, and can expire so ephemeral facts clean themselves up.

Storage: /config/nova/knowledge.db (sibling of patterns.db). Pure SQLite —
keyword + recency + salience recall now; an embedding column can be added later
for semantic search without reshaping callers.

Relations (8.7.13) are the other half: simple links between things ("sam owns
car.jeep", "kitchen adjacent_to garage"). They live in the same file, start
PENDING whatever their source, and are only read, or shown to the model, once a
person has confirmed them. See the "Relations" section below.

All DB functions are SYNC — call them via hass.async_add_executor_job(...).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
import unicodedata
from typing import Optional

from .persistence import sqlite as _store
from . import paths

_LOGGER = logging.getLogger(__name__)

DB_PATH: Optional[str] = None  # override; None resolves via paths.py


def _db_path() -> str:
    return DB_PATH or paths.nova_path("knowledge.db")

KINDS = ("fact", "preference", "event", "profile")
SOURCES = ("stated", "observed", "inferred")
DEFAULT_SUBJECT = "household"

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "at",
    "for", "and", "or", "my", "your", "our", "i", "me", "do", "does", "what",
    "that", "this", "it", "with", "about", "they", "them", "their",
}


# ── connection / schema ──────────────────────────────────────────────────────

def _connect() -> Optional[sqlite3.Connection]:
    try:
        conn = _store.connect(_db_path(), timeout=10)
        _store.ensure(conn, "facts", "fact_vectors", "relations")
        conn.commit()
        return conn
    except Exception as exc:
        _LOGGER.warning("knowledge: connect failed: %s", exc)
        return None


def _row_to_fact(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "subject": row["subject"],
        "key": row["key"],
        "value": row["value"],
        "source": row["source"],
        "confidence": round(row["confidence"], 3),
        "salience": round(row["salience"], 3),
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_referenced": row["last_referenced"],
        "expires_at": row["expires_at"],
    }


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if t not in _STOPWORDS and len(t) > 1}


# ── write ────────────────────────────────────────────────────────────────────

def remember(
    key: str,
    value: str,
    *,
    subject: str = DEFAULT_SUBJECT,
    kind: str = "fact",
    source: str = "stated",
    confidence: float = 1.0,
    salience: float = 1.0,
    ttl_seconds: Optional[float] = None,
    respect_stated: bool = False,
    status: str = "confirmed",
    now: Optional[float] = None,
) -> Optional[dict]:
    """
    Upsert a fact. One value per (subject, key) — re-teaching the same key updates
    it in place (and a stated fact overrides a previously observed/inferred one).

    respect_stated: when True, an incoming non-"stated" write (e.g. an observation
    from the pattern analyzer) will NOT overwrite an existing fact the user
    explicitly stated — the stated fact is returned unchanged. This keeps machine
    inference from clobbering things the user told us directly.

    status (v7.88.0): 'confirmed' by default — this covers every trusted write
    path (the panel's own TEACH form, the `nova.remember` HA service, and
    pattern_analyzer's observed facts all require their own pre-existing
    authorization, so none of them need gating here). Pass status='pending' ONLY
    from a path that isn't already trusted on its own — today, that's just
    agent.py's `remember` tool, since the model decides on its own when to call
    it based on everything it's seen in the conversation, including content it
    merely read aloud. A pending fact is written but excluded from recall()/
    all_facts()'s default (confirmed-only) view until confirm_fact() promotes it.

    Returns the stored fact, or None on failure. SYNC — call via executor.
    """
    key = (key or "").strip()
    value = (value or "").strip()
    if not key or not value:
        return None
    if kind not in KINDS:
        kind = "fact"
    if source not in SOURCES:
        source = "stated"
    if status not in ("confirmed", "pending"):
        status = "confirmed"
    now = now if now is not None else time.time()
    expires_at = (now + ttl_seconds) if ttl_seconds else None
    subject = (subject or DEFAULT_SUBJECT).strip() or DEFAULT_SUBJECT

    conn = _connect()
    if conn is None:
        return None
    try:
        with conn:
            existing = conn.execute(
                "SELECT id, source FROM facts WHERE subject = ? AND key = ?",
                (subject, key),
            ).fetchone()
            if existing:
                if respect_stated and existing["source"] == "stated" and source != "stated":
                    row = conn.execute(
                        "SELECT * FROM facts WHERE id = ?", (existing["id"],)).fetchone()
                    return _row_to_fact(row)  # don't clobber a user-stated fact
                conn.execute(
                    """
                    UPDATE facts SET value = ?, kind = ?, source = ?, confidence = ?,
                        salience = ?, status = ?, updated_at = ?, expires_at = ?
                    WHERE id = ?
                    """,
                    (value, kind, source, confidence, salience, status,
                     now, expires_at, existing["id"]),
                )
                fid = existing["id"]
            else:
                cur = conn.execute(
                    """
                    INSERT INTO facts
                        (kind, subject, key, value, source, confidence, salience,
                         status, created_at, updated_at, last_referenced, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (kind, subject, key, value, source, confidence, salience,
                     status, now, now, None, expires_at),
                )
                fid = cur.lastrowid
            row = conn.execute("SELECT * FROM facts WHERE id = ?", (fid,)).fetchone()
        _LOGGER.info("knowledge: remembered [%s] %s/%s = %r (status=%s)",
                    kind, subject, key, value, status)
        return _row_to_fact(row)
    except Exception as exc:
        _LOGGER.warning("knowledge: remember failed: %s", exc)
        return None
    finally:
        conn.close()


def rename_observed(old_key: str, new_key: str, *,
                    subject: str = DEFAULT_SUBJECT) -> bool:
    """Move an OBSERVED fact to a new key (the pattern analyzer rewording
    what it learned), keeping its value, confidence and history. A fact the
    user stated, or a new key that already exists, is never touched. Returns
    True when a fact moved. SYNC — call via executor. Never raises."""
    old_key, new_key = (old_key or "").strip(), (new_key or "").strip()
    subject = (subject or DEFAULT_SUBJECT).strip() or DEFAULT_SUBJECT
    if not old_key or not new_key or old_key == new_key:
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        with conn:
            cur = conn.execute(
                "UPDATE facts SET key = ? WHERE subject = ? AND key = ? "
                "AND source = 'observed' AND NOT EXISTS ("
                "SELECT 1 FROM facts WHERE subject = ? AND key = ?)",
                (new_key, subject, old_key, subject, new_key))
            return cur.rowcount > 0
    except Exception as exc:
        _LOGGER.warning("knowledge: rename failed: %s", exc)
        return False
    finally:
        conn.close()


def confirm_fact(fact_id: int) -> bool:
    """Promote a pending fact to confirmed (v7.88.0) — the human-approval step
    for agent.py's `remember` tool. Returns True if a row was actually
    updated (False if fact_id doesn't exist or was already confirmed).
    SYNC — call via executor."""
    conn = _connect()
    if conn is None:
        return False
    try:
        with conn:
            cur = conn.execute(
                "UPDATE facts SET status = 'confirmed' WHERE id = ? AND status != 'confirmed'",
                (fact_id,),
            )
            return cur.rowcount > 0
    except Exception as exc:
        _LOGGER.warning("knowledge: confirm_fact failed: %s", exc)
        return False
    finally:
        conn.close()


def edit_fact(fact_id: int, value: str, *, now: Optional[float] = None) -> Optional[dict]:
    """Correct a fact's value in place, e.g. before confirming a pending one
    (v7.88.0). Does not change status. Returns the updated fact, or None if
    fact_id doesn't exist or value is empty. SYNC — call via executor."""
    value = (value or "").strip()
    if not value:
        return None
    now = now if now is not None else time.time()
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn:
            cur = conn.execute(
                "UPDATE facts SET value = ?, updated_at = ? WHERE id = ?",
                (value, now, fact_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return _row_to_fact(row)
    except Exception as exc:
        _LOGGER.warning("knowledge: edit_fact failed: %s", exc)
        return None
    finally:
        conn.close()


def forget(
    *,
    fact_id: Optional[int] = None,
    subject: Optional[str] = None,
    key: Optional[str] = None,
) -> int:
    """Delete by id, or by (subject, key). Returns rows removed. SYNC."""
    conn = _connect()
    if conn is None:
        return 0
    try:
        with conn:
            if fact_id is not None:
                cur = conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            elif key is not None:
                if subject is not None:
                    cur = conn.execute(
                        "DELETE FROM facts WHERE subject = ? AND key = ?", (subject, key))
                else:
                    cur = conn.execute("DELETE FROM facts WHERE key = ?", (key,))
            else:
                return 0
            return cur.rowcount
    except Exception as exc:
        _LOGGER.warning("knowledge: forget failed: %s", exc)
        return 0
    finally:
        conn.close()


def purge_expired(now: Optional[float] = None) -> int:
    """Remove facts past their expiry. Returns rows removed. SYNC."""
    now = now if now is not None else time.time()
    conn = _connect()
    if conn is None:
        return 0
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM facts WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
            return cur.rowcount
    except Exception as exc:
        _LOGGER.warning("knowledge: purge failed: %s", exc)
        return 0
    finally:
        conn.close()


# ── read ─────────────────────────────────────────────────────────────────────

def _live_rows(conn: sqlite3.Connection, subject: Optional[str], now: float,
               subjects: Optional[list] = None, status: Optional[str] = None) -> list:
    sql = "SELECT * FROM facts WHERE (expires_at IS NULL OR expires_at >= ?)"
    params: list = [now]
    if subjects is not None:
        placeholders = ",".join("?" for _ in subjects) or "''"
        sql += f" AND subject IN ({placeholders})"
        params.extend(subjects)
    elif subject is not None:
        sql += " AND subject = ?"
        params.append(subject)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    return conn.execute(sql, params).fetchall()


def all_facts(subject: Optional[str] = None, now: Optional[float] = None,
               subjects: Optional[list] = None, status: Optional[str] = None) -> list[dict]:
    """All non-expired facts (optionally for one subject), newest first. SYNC.

    `status` (v7.88.0): None (default) returns every status, matching this
    function's original behavior — callers that care about trust (recall(),
    prompt_block()) pass status='confirmed' explicitly rather than relying on
    a changed default here, so existing callers (the panel, the `nova.remember`
    service, tests) keep seeing everything they always did."""
    now = now if now is not None else time.time()
    conn = _connect()
    if conn is None:
        return []
    try:
        rows = _live_rows(conn, subject, now, subjects, status)
        facts = [_row_to_fact(r) for r in rows]
        facts.sort(key=lambda f: f["updated_at"], reverse=True)
        return facts
    except Exception as exc:
        _LOGGER.warning("knowledge: all_facts failed: %s", exc)
        return []
    finally:
        conn.close()


def pending_facts(subject: Optional[str] = None, now: Optional[float] = None,
                  subjects: Optional[list] = None) -> list[dict]:
    """Facts awaiting human confirmation (v7.88.0) — for the panel's Pending
    view. Thin wrapper over all_facts(status='pending'). SYNC."""
    return all_facts(subject=subject, now=now, subjects=subjects, status="pending")


def recall(
    query: str = "",
    *,
    subject: Optional[str] = None,
    k: int = 5,
    now: Optional[float] = None,
    touch: bool = True,
    subjects: Optional[list] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """
    Retrieve the k most relevant facts. Scored by query-term overlap (key+value),
    then salience·confidence, then recency. Empty query → most salient/recent.
    Bumps last_referenced on returned facts so referenced knowledge stays warm.
    SYNC — call via executor.

    `status` (v7.88.0): None (default) matches this function's original
    behavior; prompt_block() passes status='confirmed' explicitly so a
    pending, unconfirmed fact is never surfaced to the model.
    """
    now = now if now is not None else time.time()
    conn = _connect()
    if conn is None:
        return []
    try:
        rows = _live_rows(conn, subject, now, subjects, status)
        if not rows:
            return []
        q_tokens = _tokens(query)
        scored = []
        for r in rows:
            f = _row_to_fact(r)
            text_tokens = _tokens(f["key"] + " " + f["value"])
            overlap = len(q_tokens & text_tokens)
            match = (overlap / len(q_tokens)) if q_tokens else 0.0
            if q_tokens and overlap == 0:
                continue  # a real query that hits nothing is not relevant
            age_days = max(0.0, (now - f["updated_at"]) / 86400.0)
            recency = 1.0 / (1.0 + age_days)
            score = (3.0 * match) + (f["salience"] * f["confidence"]) + (0.5 * recency)
            scored.append((score, f))
        scored.sort(key=lambda s: s[0], reverse=True)
        top = [f for _, f in scored[:max(1, k)]]
        if touch and top:
            ids = [f["id"] for f in top]
            with conn:
                conn.executemany(
                    "UPDATE facts SET last_referenced = ? WHERE id = ?",
                    [(now, i) for i in ids],
                )
        return top
    except Exception as exc:
        _LOGGER.warning("knowledge: recall failed: %s", exc)
        return []
    finally:
        conn.close()


# ── prompt injection ─────────────────────────────────────────────────────────

_SUBJECT_LABEL = {"household": "Household", "primary": "About the primary resident"}


def _fence_facts(content: str, *, _token: str | None = None) -> str:
    """Wrap curated facts with a random per-call delimiter and a hardened
    anti-injection instruction (v7.87.0, consolidated into prompt_fence.py at
    v7.89.0) — same defence, same reasoning as memory_thread.py's reseed
    fencing and memory.py's semantic-recall fencing, applied here because
    prompt_block()'s output is spliced straight into the persona/system
    prompt with no framing otherwise. Facts are user-stated more often than
    the other two stores (someone has to explicitly ask Nova to remember
    something), but the model itself decides when to call the `remember`
    tool and what to store — content earlier in a conversation could still
    influence it into persisting a poisoned fact that a future conversation
    would otherwise trust unfenced. `_token` is test-only; production
    callers never pass it."""
    from .prompt_fence import fence
    return fence(
        content,
        label="KNOWLEDGE",
        noun="are facts Nova has previously stored",
        callback_noun="a stored fact",
        _token=_token,
    )


def prompt_block(query: str = "", *, subject: Optional[str] = None,
                 limit: int = 12, now: Optional[float] = None,
                 subjects: Optional[list] = None) -> str:
    """
    A compact "what you know" block for the system prompt. If a query is given,
    the most relevant facts; otherwise the most salient. Returns "" when empty so
    callers can concatenate unconditionally. Fenced against prompt injection
    (v7.87.0) — see _fence_facts. Confirmed facts only (v7.88.0) — a fact
    agent.py's `remember` tool wrote as pending must never reach the model
    until a human has approved it; see confirm_fact().
    """
    facts = (recall(query, subject=subject, k=limit, now=now, touch=False,
                    subjects=subjects, status="confirmed")
             if query else all_facts(subject=subject, now=now, subjects=subjects,
                                     status="confirmed")[:limit])
    return format_block(facts)


def format_block(facts: list[dict]) -> str:
    """The fenced "what you know" block for a list of facts. Callers pass
    confirmed facts only. Returns "" when there are none."""
    if not facts:
        return ""
    by_subject: dict = {}
    for f in facts:
        by_subject.setdefault(f["subject"], []).append(f)
    lines = ["## What you know"]
    for subj, items in by_subject.items():
        lines.append(_SUBJECT_LABEL.get(subj, subj))
        for f in items:
            hedge = "" if f["source"] == "stated" and f["confidence"] >= 0.9 else " (~)"
            lines.append(f"- {f['key']}: {f['value']}{hedge}")
    return _fence_facts("\n".join(lines))


# ── stats (panel) ────────────────────────────────────────────────────────────

def stats(now: Optional[float] = None) -> dict:
    """Counts only, never names. `relations` is the number of live pending and
    confirmed relations."""
    now = now if now is not None else time.time()
    empty = {"total": 0, "by_kind": {}, "by_subject": {},
             "relations": {"pending": 0, "confirmed": 0}}
    conn = _connect()
    if conn is None:
        return empty
    try:
        rows = _live_rows(conn, None, now)
        by_kind: dict = {}
        by_subject: dict = {}
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
            by_subject[r["subject"]] = by_subject.get(r["subject"], 0) + 1
        return {"total": len(rows), "by_kind": by_kind, "by_subject": by_subject,
                "relations": _relation_counts(conn)}
    except Exception as exc:
        _LOGGER.warning("knowledge: stats failed: %s", exc)
        return empty
    finally:
        conn.close()


# ── Relations (8.7.13) ───────────────────────────────────────────────────────
# Links between two things: subject, predicate, object ("sam owns car.jeep").
#
# Trust model, the same as pending facts but stricter:
#   • EVERY relation starts 'pending', whatever its source. Nothing is read
#     back, shown to the model, or counted as known until a person confirms it.
#   • Nothing infers relations by itself. Nova only learns from confirmations.
#   • Removing a relation is a SOFT delete (deleted_at). A later write that is
#     not user stated can never bring an edge back that a person removed.
#   • Nodes are stored normalized (like identity.normalize), so "Sam" and "sam"
#     are one node; a predicate is lowercase snake case.
# Every write path validates, and none raises.

RELATION_LIVE_CAP = 500            # live (non removed) rows, pending and confirmed
RELATION_PROMPT_LIMIT = 12         # edges shown to the model at most
RELATION_PROMPT_MAX_CHARS = 1200   # and the block's content never exceeds this
_NODE_MAX = 80
_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# What a backend or a model writes for "nobody": never a node.
_BAD_NODES = {"unknown", "unknown_person", "unknown_face", "unrecognized",
              "unrecognised", "none", "null", "unavailable", "nan", "undefined"}


def _normalize_node(value) -> Optional[str]:
    """A node's stored form (lowercase, spaces to underscores, as
    identity.normalize does), or None when it is not acceptable: it must be a
    string of 1 to 80 characters once trimmed, with no control characters, and
    not "unknown" or one of its relatives."""
    if type(value) is not str:
        return None
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        return None
    text = value.strip()
    if not text or len(text) > _NODE_MAX:
        return None
    node = "_".join(text.lower().split())
    if not node or node in _BAD_NODES:
        return None
    return node


def _normalize_predicate(value) -> Optional[str]:
    """A predicate, or None: lowercase snake case, 2 to 40 characters,
    starting with a letter ("owns", "adjacent_to"). Not corrected: "Owns" or
    "adjacent to" are refused."""
    if type(value) is not str:
        return None
    text = value.strip()
    return text if _PREDICATE_RE.match(text) else None


def validate_relation(subject, predicate, obj) -> tuple[Optional[tuple], str]:
    """((subject, predicate, object) normalized, "") or (None, error code).
    Codes: invalid_subject, invalid_predicate, invalid_object, self_relation."""
    s_node = _normalize_node(subject)
    if s_node is None:
        return None, "invalid_subject"
    pred = _normalize_predicate(predicate)
    if pred is None:
        return None, "invalid_predicate"
    o_node = _normalize_node(obj)
    if o_node is None:
        return None, "invalid_object"
    if s_node == o_node:
        return None, "self_relation"
    return (s_node, pred, o_node), ""


def _row_to_relation(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "subject": row["subject"],
        "predicate": row["predicate"],
        "object": row["object"],
        "source": row["source"],
        "status": row["status"],
        "confidence": round(row["confidence"], 3),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _relation_counts(conn: sqlite3.Connection) -> dict:
    out = {"pending": 0, "confirmed": 0}
    for status, n in conn.execute(
            "SELECT status, COUNT(*) FROM relations WHERE deleted_at IS NULL "
            "GROUP BY status"):
        out[status] = int(n)
    return out


def _live_relation_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM relations WHERE deleted_at IS NULL").fetchone()[0])


def _unit(value, default: float = 1.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, v)) if v == v else default


def propose_relation(subject, predicate, obj, *, source: str = "stated",
                     confidence: float = 1.0, now: Optional[float] = None) -> dict:
    """Stage a relation as PENDING. Never confirmed here, whatever `source` is.

    Returns {"ok", "created", "relation", "error"}. ok False carries a code:
    invalid_subject, invalid_predicate, invalid_object, self_relation,
    relation_cap (500 live rows: nothing is evicted, new proposals are refused
    until some are removed), removed_by_user (a non stated write meets an edge a
    person removed), failed.

    A relation that already exists and is live is returned unchanged (created
    False), pending or confirmed. A removed one is only brought back by a
    "stated" write, and then as PENDING again, so a person decides again.
    SYNC — call via executor."""
    fields, error = validate_relation(subject, predicate, obj)
    if fields is None:
        return {"ok": False, "created": False, "relation": None, "error": error}
    subj, pred, o = fields
    if source not in SOURCES:
        source = "stated"
    conf = _unit(confidence)
    now = now if now is not None else time.time()
    conn = _connect()
    if conn is None:
        return {"ok": False, "created": False, "relation": None, "error": "failed"}
    try:
        with conn:
            row = conn.execute(
                "SELECT * FROM relations WHERE subject = ? AND predicate = ? AND object = ?",
                (subj, pred, o)).fetchone()
            if row is not None and row["deleted_at"] is None:
                return {"ok": True, "created": False,
                        "relation": _row_to_relation(row), "error": None}
            if row is not None and source != "stated":
                return {"ok": False, "created": False, "relation": None,
                        "error": "removed_by_user"}
            if _live_relation_count(conn) >= RELATION_LIVE_CAP:
                return {"ok": False, "created": False, "relation": None,
                        "error": "relation_cap"}
            if row is not None:
                conn.execute(
                    "UPDATE relations SET status = 'pending', source = ?, confidence = ?, "
                    "updated_at = ?, deleted_at = NULL WHERE id = ?",
                    (source, conf, now, row["id"]))
                rid = row["id"]
            else:
                rid = conn.execute(
                    "INSERT INTO relations (subject, predicate, object, source, status, "
                    "confidence, created_at, updated_at, deleted_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, NULL)",
                    (subj, pred, o, source, conf, now, now)).lastrowid
            out = conn.execute("SELECT * FROM relations WHERE id = ?", (rid,)).fetchone()
        return {"ok": True, "created": True, "relation": _row_to_relation(out), "error": None}
    except Exception as exc:
        _LOGGER.warning("knowledge: propose_relation failed: %s", exc)
        return {"ok": False, "created": False, "relation": None, "error": "failed"}
    finally:
        conn.close()


def confirm_relation(relation_id, *, now: Optional[float] = None) -> bool:
    """Promote a PENDING, live relation to confirmed. False for anything else
    (unknown id, already confirmed, removed). SYNC — call via executor."""
    if type(relation_id) is not int:
        return False
    now = now if now is not None else time.time()
    conn = _connect()
    if conn is None:
        return False
    try:
        with conn:
            cur = conn.execute(
                "UPDATE relations SET status = 'confirmed', updated_at = ? "
                "WHERE id = ? AND status = 'pending' AND deleted_at IS NULL",
                (now, relation_id))
            return cur.rowcount > 0
    except Exception as exc:
        _LOGGER.warning("knowledge: confirm_relation failed: %s", exc)
        return False
    finally:
        conn.close()


def remove_relation(relation_id, *, only_pending: bool = False,
                    now: Optional[float] = None) -> bool:
    """Soft delete a live relation (reject a pending one, or remove a
    confirmed one). The row stays, marked removed, so it cannot come back from
    a non stated write. `only_pending` limits it to pending rows (the reject
    path). True when a row was removed. SYNC — call via executor."""
    if type(relation_id) is not int:
        return False
    now = now if now is not None else time.time()
    conn = _connect()
    if conn is None:
        return False
    try:
        with conn:
            sql = "UPDATE relations SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL"
            if only_pending:
                sql += " AND status = 'pending'"
            return conn.execute(sql, (now, now, relation_id)).rowcount > 0
    except Exception as exc:
        _LOGGER.warning("knowledge: remove_relation failed: %s", exc)
        return False
    finally:
        conn.close()


def edit_relation(relation_id, subject=None, predicate=None, obj=None, *,
                  now: Optional[float] = None) -> dict:
    """Correct a PENDING relation before confirming it. Only the fields given
    change; the result is validated as a whole. Returns {"ok", "relation",
    "error"}: the validation codes, not_found (unknown, removed or already
    confirmed), duplicate (it would equal another relation, live or removed),
    failed. Status never changes. SYNC — call via executor."""
    if type(relation_id) is not int:
        return {"ok": False, "relation": None, "error": "not_found"}
    now = now if now is not None else time.time()
    conn = _connect()
    if conn is None:
        return {"ok": False, "relation": None, "error": "failed"}
    try:
        with conn:
            row = conn.execute(
                "SELECT * FROM relations WHERE id = ? AND status = 'pending' "
                "AND deleted_at IS NULL", (relation_id,)).fetchone()
            if row is None:
                return {"ok": False, "relation": None, "error": "not_found"}
            fields, error = validate_relation(
                row["subject"] if subject is None else subject,
                row["predicate"] if predicate is None else predicate,
                row["object"] if obj is None else obj)
            if fields is None:
                return {"ok": False, "relation": None, "error": error}
            clash = conn.execute(
                "SELECT id FROM relations WHERE subject = ? AND predicate = ? "
                "AND object = ? AND id != ?", (*fields, relation_id)).fetchone()
            if clash is not None:
                return {"ok": False, "relation": None, "error": "duplicate"}
            conn.execute(
                "UPDATE relations SET subject = ?, predicate = ?, object = ?, "
                "updated_at = ? WHERE id = ?", (*fields, now, relation_id))
            out = conn.execute("SELECT * FROM relations WHERE id = ?",
                               (relation_id,)).fetchone()
        return {"ok": True, "relation": _row_to_relation(out), "error": None}
    except Exception as exc:
        _LOGGER.warning("knowledge: edit_relation failed: %s", exc)
        return {"ok": False, "relation": None, "error": "failed"}
    finally:
        conn.close()


def list_relations(status: Optional[str] = None, node=None,
                   limit: int = 500) -> list[dict]:
    """Live (not removed) relations, newest first. `status` 'pending' or
    'confirmed' (None: both, for the panel's review queue). `node` limits it to
    edges touching that node. Never raises. SYNC — call via executor."""
    conn = _connect()
    if conn is None:
        return []
    try:
        sql = "SELECT * FROM relations WHERE deleted_at IS NULL"
        params: list = []
        if status in ("pending", "confirmed"):
            sql += " AND status = ?"
            params.append(status)
        n = _normalize_node(node) if node is not None else None
        if node is not None:
            if n is None:
                return []
            sql += " AND (subject = ? OR object = ?)"
            params.extend([n, n])
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), RELATION_LIVE_CAP)))
        return [_row_to_relation(r) for r in conn.execute(sql, params).fetchall()]
    except Exception as exc:
        _LOGGER.warning("knowledge: list_relations failed: %s", exc)
        return []
    finally:
        conn.close()


def relation_counts() -> dict:
    """{"pending": n, "confirmed": n} of live relations. Counts only."""
    conn = _connect()
    if conn is None:
        return {"pending": 0, "confirmed": 0}
    try:
        return _relation_counts(conn)
    except Exception as exc:
        _LOGGER.warning("knowledge: relation_counts failed: %s", exc)
        return {"pending": 0, "confirmed": 0}
    finally:
        conn.close()


def confirmed_relations(query: str = "", *, node=None,
                        limit: int = RELATION_PROMPT_LIMIT) -> list[dict]:
    """The CONFIRMED, live relations most relevant to a query or a node, best
    first, at most `limit`. This is the only read the model's prompt and tool
    use: a pending or removed relation is never returned. With a query, an edge
    must share a word with it (or touch the node); without one the most
    recently confirmed come first. SYNC — call via executor. Never raises."""
    try:
        limit = max(1, min(int(limit), RELATION_PROMPT_LIMIT * 4))
    except (TypeError, ValueError):
        limit = RELATION_PROMPT_LIMIT
    rows = list_relations(status="confirmed", node=node, limit=RELATION_LIVE_CAP)
    if not rows:
        return []
    q_tokens = _tokens(query if isinstance(query, str) else "")
    scored = []
    now = time.time()
    for r in rows:
        text = _tokens(f"{r['subject']} {r['predicate']} {r['object']}".replace("_", " "))
        overlap = len(q_tokens & text)
        if q_tokens and node is None and overlap == 0:
            continue
        match = (overlap / len(q_tokens)) if q_tokens else 0.0
        age_days = max(0.0, (now - r["updated_at"]) / 86400.0)
        score = 3.0 * match + r["confidence"] + 0.5 / (1.0 + age_days)
        scored.append((score, r["id"], r))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [r for _, _, r in scored[:limit]]


def _fence_relations(content: str, *, _token: str | None = None) -> str:
    """Wrap the relations block in the shared anti injection fence. The
    relations are things a person confirmed, but their words were first
    written by the model or the user, so they are quoted data, never
    instructions. `_token` is test-only."""
    from .prompt_fence import fence
    return fence(
        content,
        label="RELATIONS",
        noun="are links between things that Nova has previously stored",
        callback_noun="a stored relation",
        _token=_token,
    )


def format_relations_block(relations: list[dict], *, _token: str | None = None) -> str:
    """The fenced "how things relate" block for confirmed relations, capped at
    RELATION_PROMPT_LIMIT edges and RELATION_PROMPT_MAX_CHARS of content (the
    lowest ranked edges are dropped first). "" when there are none."""
    lines: list[str] = []
    size = len("## How things relate")
    for r in relations[:RELATION_PROMPT_LIMIT]:
        line = f"- {r['subject']} {r['predicate']} {r['object']}"
        if size + 1 + len(line) > RELATION_PROMPT_MAX_CHARS:
            break
        lines.append(line)
        size += 1 + len(line)
    if not lines:
        return ""
    return _fence_relations("## How things relate\n" + "\n".join(lines), _token=_token)


def relations_prompt_block(query: str = "", *, node=None,
                           limit: int = RELATION_PROMPT_LIMIT) -> str:
    """The fenced block of confirmed relations relevant to `query` or `node`,
    for the system prompt. "" when there is nothing relevant, so callers can
    concatenate unconditionally. Pending relations never reach it."""
    return format_relations_block(confirmed_relations(query, node=node, limit=limit))


def forget_relations(*, node=None, everything: bool = False) -> int:
    """Permanently delete relations, removed ones included (the forget and wipe
    path: nothing about the person's data is kept). `node` deletes every edge
    touching that node; `everything=True` empties the table. Returns rows
    deleted. SYNC — call via executor. Never raises."""
    conn = _connect()
    if conn is None:
        return 0
    try:
        with conn:
            if everything:
                return conn.execute("DELETE FROM relations").rowcount
            n = _normalize_node(node) if node is not None else None
            if n is None:
                return 0
            return conn.execute(
                "DELETE FROM relations WHERE subject = ? OR object = ?", (n, n)).rowcount
    except Exception as exc:
        _LOGGER.warning("knowledge: forget_relations failed: %s", exc)
        return 0
    finally:
        conn.close()
