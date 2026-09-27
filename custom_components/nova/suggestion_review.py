"""Nova — AI review of learned automation suggestions (v7.126.0).

Before a newly learned suggestion reaches the panel, the "Suggestion Review"
model is asked one question: would a person living here actually want this
automation? It sees the suggestion's trigger, action, conditions and the
evidence behind it, with entities named as the household knows them.

Veto only. The model can reject a suggestion, never create or change one.
A rejected suggestion is stored as ``rejected`` with the model's reason, so
it is never suggested again (it can be restored from the panel), and a
suggestion the review cannot judge (provider down, unreadable reply) is not
stored this pass and is reviewed again on the next one.

Opt in (``suggestion_review_enabled``, off by default): it sends device and
room names to whichever provider the Suggestion Review role uses; a local
Ollama model keeps them in the house. The provider and model are chosen in
Settings like every other AI role, and default to the Main Agent's.
"""
from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any, Callable, Optional

_LOGGER = logging.getLogger(__name__)

ENABLED_KEY = "suggestion_review_enabled"
PROVIDER_KEY = "suggestion_review_provider"
MODEL_KEY = "suggestion_review_model"
ROLE = "suggestion_review"
MAX_REASON_CHARS = 300

KEEP, REJECT = "keep", "reject"

_SYSTEM = (
    "You review home-automation suggestions that a smart-home assistant "
    "learned from a household's history, before anyone sees them. Decide "
    "whether a person living in this home would plausibly want the "
    "suggested automation installed.\n"
    "Reject a suggestion when:\n"
    "- the trigger has no believable causal link to the action (for example "
    "a sensor in one room driving a device in another, or two things that "
    "only happen at the same time of day);\n"
    "- the action would surprise or annoy someone, or undo something the "
    "household does on purpose;\n"
    "- it duplicates what the listed existing automations already do;\n"
    "- it controls something that should not run unattended (locks, doors, "
    "covers, heating) without a clear reason in the evidence.\n"
    "Keep it only when the link is believable and the automation would be "
    "useful. When unsure, reject.\n"
    'Reply with JSON only: {"keep": true or false, "reason": "one short '
    'sentence a homeowner would understand"}.'
)


def enabled(config: dict) -> bool:
    """Whether suggestions are reviewed. Off unless explicitly enabled."""
    value = (config or {}).get(ENABLED_KEY, False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def role_choice(config: dict) -> tuple[str, str]:
    """(provider, model) for the Suggestion Review role, defaulting to the
    Main Agent's so enabling the review needs no other setting."""
    cfg = config or {}
    provider = str(cfg.get(PROVIDER_KEY) or cfg.get("llm_provider") or "").strip().lower()
    model = str(cfg.get(MODEL_KEY) or "").strip()
    if not model and provider == str(cfg.get("llm_provider") or "").strip().lower():
        model = str(cfg.get("model") or "").strip()
    return provider, model


def _names(details: dict) -> dict:
    names = details.get("names") if isinstance(details, dict) else None
    return dict(names) if isinstance(names, dict) else {}


def build_messages(pattern: Any, *, fence: Callable[..., str]) -> list[dict]:
    """The chat messages for one suggestion. Everything taken from the home
    (names, descriptions, automation names) goes inside a prompt-injection
    fence as data. Pure apart from `fence`'s random delimiter."""
    from .automation.suggestions import explain_suggestion, generate_automation
    from .cognitive.naming import humanize_record, humanize_text

    details = pattern.details if isinstance(pattern.details, dict) else {}
    names = _names(details)
    why = explain_suggestion(pattern.pattern_type, details, pattern.occurrences)
    try:
        automation = json.loads(generate_automation(pattern))
    except Exception:
        automation = {}
    match = details.get("automation_match") if isinstance(
        details.get("automation_match"), dict) else {}
    existing = [str(m.get("name") or m.get("entity_id") or "")
                for m in (match.get("matches") or []) if isinstance(m, dict)]
    data = {
        "suggestion": humanize_text(pattern.description, names),
        "kind": pattern.pattern_type,
        "why": humanize_text(why.get("headline", ""), names),
        "evidence": [humanize_text(e, names) for e in why.get("evidence", [])],
        "automation": humanize_record(automation, names),
        "existing_automations_touching_the_same_devices": existing[:5],
    }
    body = json.dumps(data, indent=2, ensure_ascii=False)
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": fence(
            body, label="SUGGESTION",
            noun="is one learned automation suggestion and its evidence",
            callback_noun="suggestion data")},
    ]


def parse_verdict(text: Any) -> Optional[dict]:
    """{"verdict": "keep"|"reject", "reason": str} from the model's reply, or
    None when it is unreadable (the suggestion is then reviewed again next
    pass, never kept by default)."""
    if not isinstance(text, str) or not text.strip():
        return None
    from .cognitive.provider import extract_json_text
    try:
        data = json.loads(extract_json_text(text))
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("keep"), bool):
        return None
    reason = " ".join(str(data.get("reason") or "").split())[:MAX_REASON_CHARS]
    return {"verdict": KEEP if data["keep"] else REJECT,
            "reason": reason or ("Looks useful." if data["keep"]
                                 else "Not a useful automation.")}


def _entry(hass):
    try:
        from .const import DOMAIN
        for entry in hass.config_entries.async_entries(DOMAIN):
            return entry
    except Exception:
        pass
    return None


async def _config(hass) -> dict:
    """The effective configuration (entry, panel config, credentials and
    live panel values), built in the executor from an event-loop snapshot,
    as the camera roles do."""
    from . import nova_config
    from .runtime import runtime_config_snapshot
    entry = _entry(hass)
    snapshot = runtime_config_snapshot(entry) if entry is not None else {}
    return await hass.async_add_executor_job(
        nova_config.effective_config_with_runtime, entry, snapshot)


def _spec(config: dict, provider: str, model: str):
    """The ProviderSpec for the role, or None without a provider and model,
    or for a cloud provider without its own credential. Each provider gets
    only its own credential and endpoint."""
    from .providers.registry import descriptor
    from .providers.routing import (
        ProviderSpec,
        resolve_provider_credential,
        resolve_provider_endpoint,
    )
    desc = descriptor(provider)
    if desc is None or not model:
        return None
    api_key = resolve_provider_credential(config, provider)
    if not api_key and not desc.self_hosted:
        return None
    return ProviderSpec(provider=provider, model=str(model), api_key=api_key,
                        base_url=resolve_provider_endpoint(config, provider))


async def reviewer_for(hass):
    """The review callable for one analysis pass, or None when the review
    is off or its role cannot be resolved. Never raises."""
    try:
        config = await _config(hass)
    except Exception as exc:
        _LOGGER.debug("suggestion review: config unavailable (%s)", type(exc).__name__)
        return None
    if not enabled(config):
        return None
    provider, model = role_choice(config)
    try:
        spec = _spec(config, provider, model)
    except Exception:
        spec = None
    if spec is None:
        try:
            from .websocket import nova_log
            nova_log("LEARN", "suggestion review is on but its AI role has no "
                              "usable provider/model; new suggestions wait")
        except Exception:
            pass

        async def _unavailable(_hass, _pattern):
            return None
        return _unavailable

    async def _review(hass_, pattern) -> Optional[dict]:
        return await review(hass_, pattern, spec)
    return _review


async def review(hass, pattern, spec) -> Optional[dict]:
    """Ask the Suggestion Review model about one suggestion. Returns the
    verdict with provider, model and time, or None when it can't be judged.
    Never raises."""
    from . import llm_provider
    from .prompt_fence import fence
    from .providers.activity import execute_chat
    from .providers.manager import provider_scope

    try:
        messages = build_messages(pattern, fence=fence)
    except Exception as exc:
        _LOGGER.warning("suggestion review: prompt failed (%s: %s)",
                        type(exc).__name__, exc)
        return None
    try:
        async with AsyncExitStack() as stack:
            manager = await stack.enter_async_context(
                provider_scope(hass, entry=_entry(hass)))
            client = await stack.enter_async_context(manager.lease(
                spec, binding=ROLE,
                factory=lambda: llm_provider.create_provider(
                    spec.provider, spec.api_key, spec.model, spec.base_url)))
            result = await execute_chat(
                hass, client, messages, role=ROLE, data_category="text",
                max_tokens=200, temperature=0.0, model_override=spec.model)
    except Exception as exc:
        _LOGGER.warning("suggestion review: %s/%s call failed (%s: %s)",
                        spec.provider, spec.model, type(exc).__name__, exc)
        return None
    verdict = parse_verdict(getattr(result, "text", None))
    if verdict is None:
        return None
    verdict.update({"provider": spec.provider, "model": spec.model,
                    "ts": datetime.now().isoformat(timespec="seconds")})
    return verdict
