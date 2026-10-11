"""Voice phrases (8.32.0): run what a matched phrase is linked to.

Same logging and authority as the agent's run_scene_or_script tool: every
target gets an action log row, every target goes through policy, and the
service is called once, only when every target is allowed. Nothing here
ever raises: a turn that raised would lose its history.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any, Optional

from . import voice_phrases

_LOGGER = logging.getLogger(__name__)

_INDIRECTION = ("scene", "script", "automation")
# Calls that may take a long time return as soon as Home Assistant has them.
_NON_BLOCKING = ("script", "automation", voice_phrases.SHELL_DOMAIN)


@dataclass
class RunOutcome:
    result: Any = None        # local_engine.LocalResult, or None when deferred
    deferred: bool = False    # held for confirmation on the fast path; nothing ran
    # accepted (sent), refused (not confirmed), failed, or deferred.
    status: str = "failed"


@dataclass
class PhraseStep:
    result: Any = None        # local_engine.LocalResult when a phrase ran, was refused or failed
    defer_hint: Optional[str] = None   # added to the agent's prompt when it must ask first


def _addr(honorific: str) -> str:
    return f", {honorific}" if honorific else ""


def _fill(template: str, honorific: str) -> str:
    """The reply, with {honorific} and {addr} filled by plain replacement,
    never str.format: a reply is user text."""
    text = (template or "").strip() or "Done{addr}."
    return text.replace("{addr}", _addr(honorific)).replace("{honorific}", honorific or "")


def _result(text: str, success: bool):
    from .local_engine import LocalResult
    return LocalResult(text=text, success=success, handled=True)


def _target_name(hass, key: str, domain: str, name: str) -> str:
    """How a confirmation question names this target: the device's own
    name, or the phrase's name for a shell command or a call with none."""
    if not key or domain == voice_phrases.SHELL_DOMAIN:
        return name
    try:
        st = hass.states.get(key)
        friendly = st.attributes.get("friendly_name") if st is not None else None
    except Exception:  # pylint: disable=broad-except
        friendly = None
    return str(friendly or key.split(".", 1)[-1].replace("_", " "))


def _keys(domain: str, service: str, entry: dict) -> list[str]:
    """One row per target; one row named after a shell command; one blank
    row for a call with no target."""
    if domain == voice_phrases.SHELL_DOMAIN:
        return [f"{voice_phrases.SHELL_DOMAIN}.{service}"]
    return voice_phrases.target_ids(entry) or [""]


async def run_entry(hass, entry: dict, *, device_id: Optional[str], user_id: Optional[str],
                    honorific: str, may_ask: bool) -> RunOutcome:
    """Decide every target first, then call the service once. With may_ask
    False (the conversation fast path) nothing is asked: a target that needs
    confirming defers the whole phrase. Never raises."""
    name = str(entry.get("name") or "that") if isinstance(entry, dict) else "that"
    addr = _addr(honorific)
    try:
        return await _run(hass, entry, name=name, addr=addr, device_id=device_id,
                          user_id=user_id, honorific=honorific, may_ask=may_ask)
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.warning("Nova voice phrase '%s' failed: %s", name, type(exc).__name__)
        try:
            return RunOutcome(_result(f"I couldn't run {name}{addr}.", False), False, "failed")
        except Exception:  # pylint: disable=broad-except
            return RunOutcome(None, False, "failed")


async def _run(hass, entry, *, name, addr, device_id, user_id, honorific, may_ask) -> RunOutcome:
    from . import action_log, policy
    from .safe_errors import safe_error_message
    from .voice_confirm import is_voice_satellite_device

    action = entry.get("action") or {}
    domain, _, service = str(action.get("service") or "").partition(".")
    if not domain or not service:
        return RunOutcome(_result(f"I couldn't run {name}{addr}.", False), False, "failed")
    data = dict(action.get("data") or {})
    target = {}
    ids = voice_phrases.target_ids(entry)
    if ids and domain != voice_phrases.SHELL_DOMAIN:
        target = {"entity_id": ids if len(ids) > 1 else ids[0]}
    keys = _keys(domain, service, entry)

    request_id = action_log.new_request_id()
    log_source = "voice" if is_voice_satellite_device(hass, device_id or "") else "chat"
    rows = await hass.async_add_executor_job(
        lambda: action_log.start_many(
            request_id, "voice_phrase", log_source,
            [{"key": k, "domain": domain, "service": service, "entity_id": k or None}
             for k in keys],
            requested_by_user_id=user_id or None,
            request_device_id=device_id or None,
        )
    )

    async def _log(fn, rid, *args, **kw):
        if rid is not None:
            await hass.async_add_executor_job(lambda: fn(rid, *args, **kw))

    # Decide every target before anything runs.
    for key in keys:
        rid = rows.get(key)
        await _log(action_log.mark_awaiting_approval, rid)
        request = policy.AuthorityRequest(
            domain, service, key, device_id=device_id or "", user_id=user_id or "",
            target_name=_target_name(hass, key, domain, name))
        if may_ask:
            decision = await policy.authorize(hass, request)
        else:
            decision = policy.authorize_now(hass, request)
        approval = decision.approval
        await _log(action_log.set_approval, rid, approval,
                   approval_required=(approval != "not_required"))
        if decision.allowed:
            continue
        deferred = (not may_ask) and approval == "deferred"
        reason_text = "handed to agent" if deferred else None
        # Nothing runs, so every row is blocked: this one, the ones not yet
        # decided, and the ones already allowed.
        for k in keys:
            await _log(action_log.set_execution, rows.get(k), "blocked",
                       reason_code="confirmation_not_approved", reason_text=reason_text)
        if deferred:
            return RunOutcome(None, True, "deferred")
        return RunOutcome(_result(f"I need you to confirm {name} first{addr}.", False), False,
                          "refused")

    # Every target is allowed: one call.
    try:
        await hass.services.async_call(domain, service, {**data, **target},
                                       blocking=domain not in _NON_BLOCKING)
    except Exception as exc:  # pylint: disable=broad-except
        for k in keys:
            await _log(action_log.set_execution, rows.get(k), "failed",
                       reason_code="service_call_failed")
        safe_error_message(exc, where=f"voice phrase {name}", log=True)
        return RunOutcome(_result(f"I couldn't run {name}{addr}.", False), False, "failed")
    for k in keys:
        # Home Assistant can't say when a script or automation finished:
        # accepted, never a confirmed result.
        await _log(action_log.set_execution, rows.get(k), "accepted")
        await _log(action_log.mark_not_checkable, rows.get(k))
    return RunOutcome(_result(_fill(str(entry.get("reply") or ""), honorific), True), False,
                      "accepted")


def _defer_hint(entry: dict) -> Optional[str]:
    """The note for the agent when a phrase needs confirming: run it with a
    tool that asks. None when no tool can (another domain, or offline)."""
    from . import connectivity, prompt_fence
    # is_online, never allow_request: that would use the breaker's one probe.
    if not connectivity.is_online():
        return None
    domain = str((entry.get("action") or {}).get("service") or "").partition(".")[0]
    if domain not in _INDIRECTION:
        return None
    ids = voice_phrases.target_ids(entry)
    if entry.get("allow_ai") is True:
        how = (f"call run_voice_phrase with phrase_id {entry.get('phrase_id')}, "
               "which asks the user to confirm first")
    elif ids:
        how = ("call run_scene_or_script once for each of these entity ids, "
               f"which asks the user to confirm first: {', '.join(ids)}")
    else:
        return None
    return prompt_fence.fence(
        f"Voice phrase name: {entry.get('name') or ''}",
        label="VOICE_PHRASE_MATCH",
        noun="is the name of the user's own voice phrase that their live message matched",
        callback_noun="a phrase name",
        extra_instruction=(
            "but the user's live message did match this voice phrase, and it needs "
            f"confirmation before it runs. To run it, {how}. Run nothing else for it"),
    )


def prompt_block() -> Optional[str]:
    """The phrases the AI may pick by meaning, for the agent's prompt, or
    None when there are none. The user's names and phrases sit inside a
    prompt fence; the phrase ids and the instruction sit outside it. At most
    voice_phrases.MAX_PROMPT_ENTRIES. Never raises."""
    try:
        from . import nova_config, prompt_fence
        cfg = copy.deepcopy(nova_config.get(voice_phrases.CONFIG_KEY))
        lines, ids = voice_phrases.prompt_lines(cfg)
        if not ids:
            return None
        mapping = ", ".join(f"{i} = {pid}" for i, pid in enumerate(ids, start=1))
        return prompt_fence.fence(
            lines,
            label="VOICE_PHRASES",
            noun="are the names of the user's own voice phrases, numbered, each with one way of saying it",
            callback_noun="a phrase name",
            extra_instruction=(
                "but when the user's live message clearly means one of these phrases, "
                "you may run it with run_voice_phrase, passing the phrase_id for its "
                f"number: {mapping}. If unsure, ask"),
        )
    except Exception:  # pylint: disable=broad-except
        return None


async def try_phrase(hass, text: str, *, honorific: str, device_id: Optional[str],
                     user_id: Optional[str]) -> PhraseStep:
    """The conversation fast path: match the words, then run the phrase or
    hand it to the agent to ask. Never raises."""
    try:
        from . import nova_config
        cfg = copy.deepcopy(nova_config.get(voice_phrases.CONFIG_KEY))
        found = voice_phrases.match(text, cfg)
        if found is None:
            return PhraseStep()
        entry = voice_phrases.find_entry(cfg, found.phrase_id)
        if entry is None:
            return PhraseStep()
        try:
            from .websocket import nova_log
            nova_log("PHRASE", f"matched '{found.name}' tier={found.tier}")
        except Exception:  # pylint: disable=broad-except
            pass
        _LOGGER.info("Nova voice phrase: matched '%s'", found.name)
        outcome = await run_entry(hass, entry, device_id=device_id, user_id=user_id,
                                  honorific=honorific, may_ask=False)
        if not outcome.deferred:
            return PhraseStep(outcome.result, None)
        hint = _defer_hint(entry)
        if hint:
            return PhraseStep(None, hint)
        return PhraseStep(_result(
            f"{found.name} needs confirming first, and I can't ask for that right now"
            f"{_addr(honorific)}.", False), None)
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.warning("Nova voice phrase step failed: %s", type(exc).__name__)
        return PhraseStep()
