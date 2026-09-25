"""Ephemeral scoped sub-agents (delegate_task)."""
from __future__ import annotations

import json
import logging


from .grants import (
    AGENT_PROFILES,
    CAPABILITY_GROUPS,
    MAX_DELEGATION_DEPTH,
    _DELEGATION_MAX_TURNS,
    _LEGACY_CAPABILITY_PROFILE_ALIASES,
    _resolve_capability,
    _resolve_profile,
)

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


async def _run_delegated(hass, args: dict, *, persona: str, provider_name: str,
                         api_key: str, model: str, base_url, config, depth: int) -> str:
    """Run one ephemeral sub-agent for a delegated objective. Returns a JSON
    string (result or error) for the parent's tool-result slot. Never raises."""
    objective = str(args.get("objective", "")).strip()
    capability = str(args.get("capability", "")).strip()
    profile_name = str(args.get("profile", "")).strip()
    if not objective:
        return json.dumps({"error": "delegate_task needs an objective"})
    if depth >= MAX_DELEGATION_DEPTH:
        return json.dumps({"error": "delegation depth limit reached — a sub-agent "
                                    "cannot delegate further; handle this directly"})

    # A named profile (HOMER) takes full precedence over 'capability' — its own
    # fixed tool set, turn cap, and directive, resolved case-insensitively and
    # ignoring anything else in `args`. An unrecognized profile is rejected
    # outright; it never falls back to the generic capability groups (a typo'd
    # or hostile profile name must not silently grant a broader tool set).
    #
    # 'capability' is checked for a legacy profile alias (currently just
    # "diagnostics" -> "homer") BEFORE falling through to the generic
    # CAPABILITY_GROUPS path, so an existing caller using
    # capability="diagnostics" gets the exact same tool grant, turn cap, and
    # directive as profile="homer" — one implementation, two accepted names.
    directive = None
    profile_label = None
    legacy_alias = None if profile_name else _LEGACY_CAPABILITY_PROFILE_ALIASES.get(capability)
    effective_profile = profile_name or legacy_alias

    if effective_profile:
        resolved = _resolve_profile(effective_profile)
        if resolved is None:
            return json.dumps({"error": "unknown profile '%s'. Options: %s"
                                        % (profile_name, ", ".join(sorted(AGENT_PROFILES)))})
        allowed, profile_cap, profile_label, directive = resolved
        try:
            turns = int(args.get("max_turns", profile_cap) or profile_cap)
        except Exception:
            turns = profile_cap
        turns = max(1, min(turns, profile_cap))
    else:
        allowed = _resolve_capability(capability)
        if not allowed:
            return json.dumps({"error": "unknown capability '%s'. Options: %s"
                                        % (capability, ", ".join(sorted(CAPABILITY_GROUPS)))})
        try:
            turns = int(args.get("max_turns", _DELEGATION_MAX_TURNS) or _DELEGATION_MAX_TURNS)
        except Exception:
            turns = _DELEGATION_MAX_TURNS
        turns = max(1, min(turns, _DELEGATION_MAX_TURNS))

    try:
        from . import loop as _loop
        result = await _loop.run_agent(
            hass,
            messages=[{"role": "user", "content": objective}],
            persona=persona, provider_name=provider_name, api_key=api_key,
            model=model, base_url=base_url, config=config,
            allowed_tools=allowed, max_iterations=turns, depth=depth + 1,
            profile_directive=directive,
        )
        out = {"objective": objective, "result": result}
        if profile_label:
            out["profile"] = profile_label
            if legacy_alias:
                out["capability"] = capability  # preserve the field an existing caller reads
        else:
            out["capability"] = capability
        return json.dumps(out)
    except Exception as exc:
        # Never echo the raw exception into a tool-result JSON that flows
        # back into the model's context (and potentially gets narrated to
        # the user) — it can carry a provider error string with more detail
        # than should leave the server. Full detail goes to the log only.
        _LOGGER.warning("delegate_task sub-agent failed (objective=%r): %s",
                        objective[:120], exc)
        return json.dumps({"error": "sub-agent failed — could not complete "
                                    "the delegated objective"})
