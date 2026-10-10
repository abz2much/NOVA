"""Learned aliases, pending facts, ignore rules and household documents."""
from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from ...persistence.files import CORRUPT, OK, read_json, write_json_atomic
from ... import paths
from typing import Optional

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


# ── Learning memory ─────────────────────────────────────────────────────────

_LEARN_FILE: Optional[str] = None  # override; None resolves via paths.py


def _learn_file() -> str:
    return _LEARN_FILE or paths.learned_file()


def _load_learned() -> dict:
    """Load persistent learned data."""
    read = read_json(_learn_file())
    if read.status == CORRUPT:
        _LOGGER.warning("Learned data unreadable, starting empty: %s", read.error)
    if read.status == OK and read.value is not None:
        return read.value
    return {"alias": {}, "preference": {}, "routine": {}}


def _save_learned(data: dict) -> None:
    """Save learned data to disk."""
    try:
        write_json_atomic(_learn_file(), data, indent=2)
    except Exception as exc:
        _LOGGER.warning("Failed to save learned data: %s", exc)


async def _exec_remember(hass: HomeAssistant, args: dict) -> str:
    """Learn and persist a user preference, routine, or alias.

    Preferences/routines require human confirmation before going live
    (v7.88.0, memory-write hardening). This tool is called by the MODEL,
    mid-conversation, based on everything it's seen — including content Nova
    merely read aloud (an email, a calendar event, a web result). Fencing
    (already shipped) stops a stored fact from being read back as a live
    instruction; it does nothing to stop a false fact from being written in
    the first place and then trusted as if the user had actually said it.
    So a new preference/routine is staged as status='pending' in the
    knowledge store (invisible to prompt_block() until confirmed) rather than
    taking effect immediately, and the model is told to ask the user to
    confirm in its reply — the same conversation the fact came up in, whether
    that's voice, the chat panel, or Telegram, all of which reach Nova
    through this same tool-calling loop. If the user doesn't respond, the
    fact just stays pending — visible in the panel's Memory tab for the user to
    confirm, reject, or edit later.

    Aliases (an entity-name lookup for search_entities, not a "fact" the
    model reasons over or that reaches a prompt) are unaffected — still
    written immediately, as before.
    """
    key = args.get("key", "")
    name = args.get("name", "").lower().strip()
    value = args.get("value", "")

    if key not in ("alias", "preference", "routine"):
        return json.dumps({"error": f"Unknown category: {key}"})

    if key == "alias":
        data = await hass.async_add_executor_job(_load_learned)
        data.setdefault("alias", {})[name] = value
        await hass.async_add_executor_job(_save_learned, data)
        _LOGGER.info("Nova learned an alias")
        return json.dumps({"success": True, "learned": f"alias: '{name}' → '{value}'"})

    # preference/routine: stage as pending in the knowledge store (the single
    # source of truth for both — the old parallel _LEARN_FILE write for these
    # two categories is gone; it was redundant with (and less safe than)
    # knowledge.py's confirmed-only, fenced, subject-scoped prompt_block()).
    try:
        from ... import knowledge
        if key == "preference":
            from ... import identity
            k_subject = identity.resolve_subject(hass)  # this person, or "primary"
            k_kind = "preference"
        else:
            k_subject = knowledge.DEFAULT_SUBJECT
            k_kind = "fact"
        fact = await hass.async_add_executor_job(
            lambda: knowledge.remember(name, value, subject=k_subject,
                                       kind=k_kind, source="stated", status="pending"))
    except Exception as exc:
        _LOGGER.warning("Nova remember (pending) failed: %s", exc)
        return json.dumps({"error": f"failed to save: {exc}"})

    if not fact:
        return json.dumps({"error": "failed to stage fact for confirmation"})

    _LOGGER.info("Nova staged a pending %s (fact_id=%s)", key, fact["id"])
    return json.dumps({
        "success": True,
        "status": "pending",
        "fact_id": fact["id"],
        "enforced": False,
        "message": (
            f"Saved '{name}: {value}' as PENDING, not yet trusted — ask the "
            f"user to confirm this is actually correct before relying on it "
            f"again. If they confirm, call confirm_pending_fact with "
            f"fact_id={fact['id']}. If they say no or correct it, call "
            f"reject_pending_fact with the same fact_id instead. This is a "
            f"conversational memory only — it does not change any alerting "
            f"or automation code. Report it to the user as a saved "
            f"preference, never as an installed or enforced rule."
        ),
    })


async def _exec_confirm_pending_fact(hass: HomeAssistant, args: dict) -> str:
    """Promote a pending fact (from `remember`) to confirmed, once the user
    has actually approved it in conversation (v7.88.0)."""
    try:
        raw_id: Any = args.get("fact_id")
        fact_id = int(raw_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "fact_id is required and must be an integer"})
    from ... import knowledge
    ok = await hass.async_add_executor_job(knowledge.confirm_fact, fact_id)
    if not ok:
        return json.dumps({"error": f"no pending fact with id {fact_id} (already "
                                    f"confirmed, rejected, or never existed)"})
    return json.dumps({
        "success": True, "confirmed": fact_id, "enforced": False,
        "message": "Fact confirmed and now trusted in conversation — still a "
                   "memory only, not a change to any alerting or automation code.",
    })


async def _exec_reject_pending_fact(hass: HomeAssistant, args: dict) -> str:
    """Discard a pending fact (from `remember`) the user did not confirm, or
    explicitly said was wrong (v7.88.0)."""
    try:
        raw_id: Any = args.get("fact_id")
        fact_id = int(raw_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "fact_id is required and must be an integer"})
    from ... import knowledge
    removed = await hass.async_add_executor_job(lambda: knowledge.forget(fact_id=fact_id))
    return json.dumps({"success": bool(removed), "rejected": fact_id})


# ── Relations (8.7.13) ──────────────────────────────────────────────────────
# Links between things ("house member owns bike"). Same trust model as pending
# facts, stricter: propose_relation can only STAGE a pending relation, nothing
# reads it until a person confirms it in the panel's Memory tab (8.7.14: there
# is deliberately NO agent tool that confirms a relation), and a confirmed one
# is shown to the model only inside a fenced block.

_RELATION_ERRORS = {
    "invalid_subject": "subject must be 1 to 80 characters with no control "
                       "characters, and cannot be 'unknown'",
    "invalid_object": "object must be 1 to 80 characters with no control "
                      "characters, and cannot be 'unknown'",
    "invalid_predicate": "predicate must be lowercase snake case, 2 to 40 "
                         "characters, starting with a letter (for example "
                         "owns or adjacent_to)",
    "self_relation": "a thing cannot be related to itself",
    "relation_cap": "the relation store is full (500); ask the user to remove "
                    "some in the Memory tab before adding more",
    "removed_by_user": "the user removed this relation earlier; it is not added again",
    "failed": "the relation could not be saved",
}


def _relation_id(args: dict) -> Optional[int]:
    raw: Any = args.get("relation_id")
    if isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _exec_propose_relation(hass: HomeAssistant, args: dict) -> str:
    """Stage a relation as PENDING. This tool can never confirm what it
    stages: it takes no status, and the result tells the model to ask the user."""
    from ... import knowledge
    res = await hass.async_add_executor_job(
        lambda: knowledge.propose_relation(
            args.get("subject"), args.get("predicate"), args.get("object"),
            source="stated"))
    if not res["ok"]:
        return json.dumps({"error": _RELATION_ERRORS.get(res["error"], res["error"]),
                           "code": res["error"]})
    rel = res["relation"]
    if rel["status"] == "confirmed":
        return json.dumps({
            "success": True, "status": "confirmed", "relation_id": rel["id"],
            "created": False,
            "message": f"'{rel['subject']} {rel['predicate']} {rel['object']}' "
                       f"is already a confirmed relation.",
        })
    _LOGGER.info("Nova staged a pending relation (relation_id=%s)", rel["id"])
    return json.dumps({
        "success": True, "status": "pending", "relation_id": rel["id"],
        "created": res["created"], "enforced": False,
        "message": (
            f"Saved '{rel['subject']} {rel['predicate']} {rel['object']}' as "
            f"PENDING, not yet trusted and not shown to you again until the "
            f"user confirms it. You cannot confirm it yourself: tell the user "
            f"to confirm it in the Memory tab of the Nova panel (Relations). "
            f"If they say it is wrong, call reject_pending_relation with "
            f"relation_id={rel['id']}. It is a memory only and changes no "
            f"alert, automation or device."),
    })


async def _exec_reject_pending_relation(hass: HomeAssistant, args: dict) -> str:
    """Discard a pending relation the user did not confirm. It stays removed:
    only the user can bring it back."""
    rid = _relation_id(args)
    if rid is None:
        return json.dumps({"error": "relation_id is required and must be an integer"})
    from ... import knowledge
    removed = await hass.async_add_executor_job(
        lambda: knowledge.remove_relation(rid, only_pending=True))
    return json.dumps({"success": bool(removed), "rejected": rid})


async def _exec_lookup_relations(hass: HomeAssistant, args: dict) -> str:
    """Read only: the CONFIRMED relations touching a thing. Pending and removed
    ones are never returned."""
    entity = args.get("entity")
    from ... import knowledge
    if type(entity) is not str or not entity.strip():
        return json.dumps({"error": "entity is required: the name of a person, "
                                    "place or thing"})
    rels = await hass.async_add_executor_job(
        lambda: knowledge.confirmed_relations(node=entity, limit=knowledge.RELATION_PROMPT_LIMIT * 2))
    return json.dumps({
        "relations": [{"subject": r["subject"], "predicate": r["predicate"],
                       "object": r["object"]} for r in rels],
        "note": "confirmed relations only" if rels else
                "no confirmed relation involves that name",
    })


async def _exec_ignore(hass: HomeAssistant, args: dict) -> str:
    """Add an ignore rule via the cognitive core."""
    try:
        from ... import cognitive_core
        result = cognitive_core.ignore(
            entity_pattern=args.get("entity_pattern", ""),
            duration_minutes=int(args.get("duration_minutes", 0)),
            reason=args.get("reason", "user request"),
        )
        return json.dumps(result)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_unignore(hass: HomeAssistant, args: dict) -> str:
    """Remove an ignore rule."""
    try:
        from ... import cognitive_core
        result = cognitive_core.unignore(args.get("entity_pattern", ""))
        return json.dumps(result)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_search_documents(hass: HomeAssistant, args: dict) -> str:
    """Document RAG agent — retrieve from ingested manuals/receipts (v6.55.0),
    semantic (Ollama) when enabled, else keyword (v6.57.0)."""
    try:
        from ... import documents
        hits = await documents.search_documents_async(hass, args.get("query", ""), 4)
        if not hits:
            return json.dumps({
                "results": [],
                "note": "nothing in the document library matched — it may be "
                        "empty (add files to /config/nova/documents and "
                        "ingest) or the answer isn't in the paperwork",
            })
        try:
            from ...websocket import nova_log
            engine = hits[0].get("engine", "keyword") if hits else "keyword"
            nova_log("AGENT", f"document search '{args.get('query','')}' "
                                f"→ {len(hits)} hits ({engine})")
        except Exception:
            pass
        return json.dumps({"results": hits})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_ingest_documents(hass: HomeAssistant, args: dict) -> str:
    """Re-scan the documents folder (v6.55.0), embedding for semantic search
    when enabled (v6.57.0)."""
    try:
        from ... import documents
        res = await documents.ingest_directory_async(hass)
        try:
            from ...websocket import nova_log
            extra = (f", {res.get('embedded_chunks',0)} embedded"
                     if res.get("semantic") else "")
            nova_log("AGENT", f"document ingest: {res.get('files_ingested',0)} "
                                f"files, {res.get('total_chunks',0)} chunks{extra}")
        except Exception:
            pass
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
