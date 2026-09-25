"""Learned aliases, pending facts, ignore rules and household documents."""
from __future__ import annotations

import json
import logging
import os

from homeassistant.core import HomeAssistant

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


# ── Learning memory ─────────────────────────────────────────────────────────

_LEARN_FILE = "/config/.nova_learned.json"


def _load_learned() -> dict:
    """Load persistent learned data."""
    try:
        if os.path.exists(_LEARN_FILE):
            with open(_LEARN_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"alias": {}, "preference": {}, "routine": {}}


def _save_learned(data: dict) -> None:
    """Save learned data to disk."""
    try:
        with open(_LEARN_FILE, "w") as f:
            json.dump(data, f, indent=2)
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
    fact just stays pending — visible in the panel's Memory tab for Abi to
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
        fact_id = int(args.get("fact_id"))
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
        fact_id = int(args.get("fact_id"))
    except (TypeError, ValueError):
        return json.dumps({"error": "fact_id is required and must be an integer"})
    from ... import knowledge
    removed = await hass.async_add_executor_job(lambda: knowledge.forget(fact_id=fact_id))
    return json.dumps({"success": bool(removed), "rejected": fact_id})


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
