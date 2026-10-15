"""Nova's knowledge store: the facts it knows and the links between things.

Nova keeps three kinds of memory. memory.py recalls past conversations,
patterns.db records what tends to happen in the house, and this module holds
the small set of things Nova simply knows: "bins go out on Tuesday", "the
guest room runs cold at night". Every fact belongs to a subject (the
household, or a person), records where it came from and how sure Nova is,
and may expire.

Facts from a source that is not trusted on its own (the model's remember
tool, the routine learner) are stored as pending. Pending facts are never
recalled, never shown to the model and never counted as known until a person
confirms them in the panel.

Relations (8.7.13) link two things ("bike belongs_to guest", "nursery
adjacent_to hallway"). They share the database, always start pending, and
are described in their own section below.

The database is knowledge.db beside patterns.db. Recall scores keywords,
salience and recency; a vector column can be added later without changing
callers. Every function here is blocking: run it with
hass.async_add_executor_job(...).
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

DB_PATH: Optional[str] = None  # tests point this elsewhere; None means paths.py decides

KINDS = ("fact", "preference", "event", "profile")
SOURCES = ("stated", "observed", "inferred")
DEFAULT_SUBJECT = "household"
_STATUSES = ("confirmed", "pending")

# Words too common to say anything about relevance.
_STOPWORDS = {
    "a", "about", "an", "and", "are", "at", "do", "does", "for", "i", "in",
    "is", "it", "me", "my", "of", "on", "or", "our", "that", "the", "their",
    "them", "they", "this", "to", "was", "were", "what", "with", "your",
}

_FACT_FIELDS = ("id", "kind", "subject", "key", "value", "source", "confidence",
                "salience", "status", "created_at", "updated_at", "last_referenced",
                "expires_at")
_ROUNDED = ("confidence", "salience")


def _db_path() -> str:
    return DB_PATH or paths.nova_path("knowledge.db")


def _connect() -> Optional[sqlite3.Connection]:
    """An open connection with every table in place, or None (logged)."""
    try:
        conn = _store.connect(_db_path(), timeout=10)
        _store.ensure(conn, "facts", "fact_vectors", "relations")
        conn.commit()
    except Exception as exc:
        _LOGGER.warning("knowledge: connect failed: %s", exc)
        return None
    return conn


def _with_db(action: str, fallback, work):
    """Run ``work(conn)`` on a fresh connection and close it. Any error is
    logged and ``fallback`` returned instead, so callers never see one."""
    conn = _connect()
    if conn is None:
        return fallback
    try:
        return work(conn)
    except Exception as exc:
        _LOGGER.warning("knowledge: %s failed: %s", action, exc)
        return fallback
    finally:
        conn.close()


def _row_to_fact(row: sqlite3.Row) -> dict:
    return {name: round(row[name], 3) if name in _ROUNDED else row[name]
            for name in _FACT_FIELDS}


def _fact_by_id(conn: sqlite3.Connection, fact_id) -> Optional[dict]:
    row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    return _row_to_fact(row) if row is not None else None


def _tokens(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 1 and w not in _STOPWORDS}


def _clean_subject(subject) -> str:
    return (subject or DEFAULT_SUBJECT).strip() or DEFAULT_SUBJECT


# ── writing facts ────────────────────────────────────────────────────────────

def remember(key: str, value: str, *, subject: str = DEFAULT_SUBJECT,
             kind: str = "fact", source: str = "stated", confidence: float = 1.0,
             salience: float = 1.0, ttl_seconds: Optional[float] = None,
             respect_stated: bool = False, status: str = "confirmed",
             now: Optional[float] = None) -> Optional[dict]:
    """Store a fact, one value per (subject, key): teaching a key again
    updates it in place. Returns the stored fact, or None (empty key or
    value, or a database error).

    ``respect_stated`` protects what a person said: a write from any other
    source then leaves a "stated" fact as it is and returns it unchanged.

    ``status`` is "confirmed" for the trusted paths (the panel's teach form,
    the nova.remember service). The model's remember tool and the routine
    learner pass "pending" (see the module note). Learning the value a
    confirmed fact already has keeps it confirmed (8.39.1).
    """
    key, value = (key or "").strip(), (value or "").strip()
    if not key or not value:
        return None
    kind = kind if kind in KINDS else "fact"
    source = source if source in SOURCES else "stated"
    status = status if status in _STATUSES else "confirmed"
    now = time.time() if now is None else now
    expires_at = now + ttl_seconds if ttl_seconds else None
    subject = _clean_subject(subject)

    def work(conn):
        with conn:
            current = conn.execute(
                "SELECT id, source, value, status FROM facts WHERE subject = ? AND key = ?",
                (subject, key)).fetchone()
            if current is None:
                fact_id = conn.execute(
                    "INSERT INTO facts (kind, subject, key, value, source, confidence, "
                    "salience, status, created_at, updated_at, last_referenced, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (kind, subject, key, value, source, confidence, salience, status,
                     now, now, expires_at)).lastrowid
            else:
                fact_id = current["id"]
                if respect_stated and current["source"] == "stated" and source != "stated":
                    return _fact_by_id(conn, fact_id)
                kept = status
                if (status == "pending" and current["status"] == "confirmed"
                        and current["value"] == value):
                    kept = "confirmed"
                conn.execute(
                    "UPDATE facts SET value = ?, kind = ?, source = ?, confidence = ?, "
                    "salience = ?, status = ?, updated_at = ?, expires_at = ? WHERE id = ?",
                    (value, kind, source, confidence, salience, kept, now, expires_at,
                     fact_id))
            fact = _fact_by_id(conn, fact_id)
        _LOGGER.info("knowledge: stored %s %s/%s = %r (%s)",
                     kind, subject, key, value, fact["status"])
        return fact

    return _with_db("remember", None, work)


def rename_observed(old_key: str, new_key: str, *,
                    subject: str = DEFAULT_SUBJECT) -> bool:
    """Move an observed fact to a new key (the routine learner rewording
    something it learned), keeping its value and history. A stated fact is
    never moved, and nothing moves onto a key that is already taken. True
    when a fact moved."""
    old_key, new_key = (old_key or "").strip(), (new_key or "").strip()
    if not old_key or not new_key or old_key == new_key:
        return False
    subject = _clean_subject(subject)

    def work(conn):
        with conn:
            moved = conn.execute(
                "UPDATE facts SET key = ? WHERE subject = ? AND key = ? AND source = 'observed' "
                "AND NOT EXISTS (SELECT 1 FROM facts WHERE subject = ? AND key = ?)",
                (new_key, subject, old_key, subject, new_key))
        return moved.rowcount > 0

    return _with_db("rename", False, work)


def confirm_fact(fact_id: int) -> bool:
    """A person approves a pending fact. False when there is no such fact or
    it was already confirmed."""
    def work(conn):
        with conn:
            done = conn.execute(
                "UPDATE facts SET status = 'confirmed' WHERE id = ? AND status != 'confirmed'",
                (fact_id,))
        return done.rowcount > 0

    return _with_db("confirm_fact", False, work)


def edit_fact(fact_id: int, value: str, *, now: Optional[float] = None) -> Optional[dict]:
    """Correct a fact's value, for example before confirming it. The status
    stays as it was. The updated fact, or None for an unknown id or an empty
    value."""
    value = (value or "").strip()
    if not value:
        return None
    now = time.time() if now is None else now

    def work(conn):
        with conn:
            changed = conn.execute(
                "UPDATE facts SET value = ?, updated_at = ? WHERE id = ?", (value, now, fact_id))
            if not changed.rowcount:
                return None
            return _fact_by_id(conn, fact_id)

    return _with_db("edit_fact", None, work)


def forget(*, fact_id: Optional[int] = None, subject: Optional[str] = None,
           key: Optional[str] = None) -> int:
    """Delete one fact by id, or by key (within one subject when given).
    Returns how many rows went."""
    if fact_id is not None:
        sql, params = "DELETE FROM facts WHERE id = ?", (fact_id,)
    elif key is not None and subject is not None:
        sql, params = "DELETE FROM facts WHERE subject = ? AND key = ?", (subject, key)
    elif key is not None:
        sql, params = "DELETE FROM facts WHERE key = ?", (key,)
    else:
        return 0

    def work(conn):
        with conn:
            return conn.execute(sql, params).rowcount

    return _with_db("forget", 0, work)


def purge_expired(now: Optional[float] = None) -> int:
    """Delete facts whose expiry has passed. Returns how many went."""
    now = time.time() if now is None else now

    def work(conn):
        with conn:
            return conn.execute(
                "DELETE FROM facts WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,)).rowcount

    return _with_db("purge", 0, work)


# ── reading facts ────────────────────────────────────────────────────────────

def _live_rows(conn: sqlite3.Connection, subject: Optional[str], now: float,
               subjects: Optional[list] = None, status: Optional[str] = None) -> list:
    """Unexpired fact rows. ``subjects`` (a list) wins over ``subject``;
    ``status`` limits them to one status."""
    where, params = ["(expires_at IS NULL OR expires_at >= ?)"], [now]
    if subjects is not None:
        where.append("subject IN (%s)" % (",".join("?" * len(subjects)) or "''"))
        params += list(subjects)
    elif subject is not None:
        where.append("subject = ?")
        params.append(subject)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    return conn.execute("SELECT * FROM facts WHERE " + " AND ".join(where), params).fetchall()


def all_facts(subject: Optional[str] = None, now: Optional[float] = None,
               subjects: Optional[list] = None, status: Optional[str] = None) -> list[dict]:
    """Unexpired facts, most recently updated first. ``status`` None gives
    every status (what the panel and the service expect); anything that
    reaches the model asks for "confirmed"."""
    now = time.time() if now is None else now

    def work(conn):
        facts = [_row_to_fact(r) for r in _live_rows(conn, subject, now, subjects, status)]
        return sorted(facts, key=lambda f: f["updated_at"], reverse=True)

    return _with_db("all_facts", [], work)


def pending_facts(subject: Optional[str] = None, now: Optional[float] = None,
                  subjects: Optional[list] = None) -> list[dict]:
    """Facts waiting for a person to confirm them (the panel's Pending list)."""
    return all_facts(subject=subject, now=now, subjects=subjects, status="pending")


def _relevance(query_words: set, text: str, salience_weight: float,
               updated_at: float, now: float) -> Optional[float]:
    """Score for recall: word overlap counts most, then the weight, then
    freshness. None when a real query shares no word with ``text``."""
    shared = len(query_words & _tokens(text))
    if query_words and not shared:
        return None
    match = shared / len(query_words) if query_words else 0.0
    days_old = max(0.0, (now - updated_at) / 86400.0)
    return 3.0 * match + salience_weight + 0.5 / (1.0 + days_old)


def recall(query: str = "", *, subject: Optional[str] = None, k: int = 5,
           now: Optional[float] = None, touch: bool = True,
           subjects: Optional[list] = None, status: Optional[str] = None) -> list[dict]:
    """The ``k`` facts most relevant to ``query`` (or the weightiest and
    freshest when there is no query). With ``touch`` the returned facts get
    last_referenced set, so facts in use stay warm. ``status`` as all_facts.
    """
    now = time.time() if now is None else now
    words = _tokens(query)

    def work(conn):
        ranked = []
        for row in _live_rows(conn, subject, now, subjects, status):
            fact = _row_to_fact(row)
            score = _relevance(words, fact["key"] + " " + fact["value"],
                               fact["salience"] * fact["confidence"],
                               fact["updated_at"], now)
            if score is not None:
                ranked.append((score, fact))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        best = [fact for _, fact in ranked[:max(1, k)]]
        if touch and best:
            with conn:
                conn.executemany("UPDATE facts SET last_referenced = ? WHERE id = ?",
                                 [(now, fact["id"]) for fact in best])
        return best

    return _with_db("recall", [], work)


# ── the prompt block ─────────────────────────────────────────────────────────

_SUBJECT_LABEL = {"household": "Household", "primary": "About the primary resident"}


def _fence_facts(content: str, *, _token: str | None = None) -> str:
    """Wrap the facts block in prompt_fence's random delimiter and warning.
    The block goes straight into the system prompt, and the model chose what
    to store in the first place, so a stored fact is quoted data, never an
    instruction. ``_token`` is for tests only."""
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
    """The fenced "what you know" block for the system prompt: the facts
    most relevant to ``query``, or the latest ones without a query.
    Confirmed facts only. "" when there are none, so callers can always
    concatenate it."""
    if query:
        facts = recall(query, subject=subject, k=limit, now=now, touch=False,
                       subjects=subjects, status="confirmed")
    else:
        facts = all_facts(subject=subject, now=now, subjects=subjects,
                          status="confirmed")[:limit]
    return format_block(facts)


def format_block(facts: list[dict]) -> str:
    """The fenced "what you know" block for ``facts``, grouped by subject.
    A fact that is not stated with high confidence is marked (~). Callers
    pass confirmed facts only. "" when the list is empty."""
    if not facts:
        return ""
    grouped: dict = {}
    for fact in facts:
        grouped.setdefault(fact["subject"], []).append(fact)
    lines = ["## What you know"]
    for subject, items in grouped.items():
        lines.append(_SUBJECT_LABEL.get(subject, subject))
        for fact in items:
            sure = fact["source"] == "stated" and fact["confidence"] >= 0.9
            lines.append(f"- {fact['key']}: {fact['value']}{'' if sure else ' (~)'}")
    return _fence_facts("\n".join(lines))


# ── panel counts ─────────────────────────────────────────────────────────────

def stats(now: Optional[float] = None) -> dict:
    """Counts only, never names. ``total``, ``by_kind`` and ``by_subject``
    count confirmed facts, the ones Nova uses; ``pending`` counts facts
    waiting for a person (8.49.37). ``relations`` counts live relations by
    status."""
    now = time.time() if now is None else now

    def work(conn):
        by_kind: dict = {}
        by_subject: dict = {}
        total = pending = 0
        for row in _live_rows(conn, None, now):
            if row["status"] != "confirmed":
                pending += 1
                continue
            total += 1
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
            by_subject[row["subject"]] = by_subject.get(row["subject"], 0) + 1
        return {"total": total, "pending": pending, "by_kind": by_kind,
                "by_subject": by_subject, "relations": _relation_counts(conn)}

    empty = {"total": 0, "pending": 0, "by_kind": {}, "by_subject": {},
             "relations": {"pending": 0, "confirmed": 0}}
    return _with_db("stats", empty, work)


# ── Relations (8.7.13) ───────────────────────────────────────────────────────
# Links between two things: subject, predicate, object ("house member owns bike").
#
# Trust model, the same as pending facts but stricter:
#   • EVERY relation starts 'pending', whatever its source. Nothing is read
#     back, shown to the model, or counted as known until a person confirms it.
#   • Nothing infers relations by itself. Nova only learns from confirmations.
#   • Removing a relation is a SOFT delete (deleted_at). A later write that is
#     not user stated can never bring an edge back that a person removed.
#   • Nodes are stored normalized (like identity.normalize), so "Guest" and
#     "guest" are one node; a predicate is lowercase snake case.
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
