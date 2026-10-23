"""Short lived, narrowly scoped sub-agents (delegate_task)."""
from __future__ import annotations

import json
import logging

from .grants import (
    _DELEGATION_MAX_TURNS, _LEGACY_CAPABILITY_PROFILE_ALIASES, _resolve_capability,
    _resolve_profile, AGENT_PROFILES, CAPABILITY_GROUPS, MAX_DELEGATION_DEPTH,
)

# The agent logs under …nova.agent from every module, so filters and levels
# set for it keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


def _turn_limit(args: dict, ceiling: int) -> int:
    """The sub-agent's turn limit: what was asked, at least 1 and never more
    than `ceiling`. The ceiling is used when nothing, or nothing usable, was
    asked for."""
    try:
        asked = int(args.get("max_turns", ceiling) or ceiling)
    except Exception:
        asked = ceiling
    return max(1, min(asked, ceiling))


async def _run_delegated(hass, args: dict, *, persona: str, provider_name: str,
                         api_key: str, model: str, base_url, config, depth: int) -> str:
    """Run one short lived sub-agent on a delegated objective. The reply is a
    JSON string, a result or an error, for the parent's tool result. This
    never raises."""
    objective = str(args.get("objective", "")).strip()
    capability = str(args.get("capability", "")).strip()
    profile_name = str(args.get("profile", "")).strip()
    if not objective:
        return json.dumps({"error": "delegate_task needs an objective"})
    if depth >= MAX_DELEGATION_DEPTH:
        return json.dumps({"error": "delegation depth limit reached — a sub-agent "
                                    "cannot delegate further; handle this directly"})

    # A named profile (HOMER) overrides 'capability' completely. It brings its
    # own fixed tools, turn cap and directive, is matched without regard to
    # case, and nothing else in `args` can change it. A profile name nobody
    # recognises is refused outright and never drops back to the generic
    # capability groups, so a mistyped or hostile name cannot quietly widen the
    # tools.
    #
    # Before the generic CAPABILITY_GROUPS route, 'capability' is looked up as a
    # legacy alias for a profile (only "diagnostics" for "homer" today). A caller
    # that passes capability="diagnostics" therefore gets exactly the tools,
    # turn cap and directive of profile="homer": one implementation under two
    # names.
    directive = None
    profile_label = None
    legacy_alias = None
    if not profile_name:
        legacy_alias = _LEGACY_CAPABILITY_PROFILE_ALIASES.get(capability)
    chosen_profile = profile_name or legacy_alias

    if chosen_profile:
        found = _resolve_profile(chosen_profile)
        if found is None:
            return json.dumps({"error": "unknown profile '%s'. Options: %s"
                                        % (profile_name, ", ".join(sorted(AGENT_PROFILES)))})
        allowed, profile_cap, profile_label, directive = found
        turns = _turn_limit(args, profile_cap)
    else:
        allowed = _resolve_capability(capability)
        if not allowed:
            return json.dumps({"error": "unknown capability '%s'. Options: %s"
                                        % (capability, ", ".join(sorted(CAPABILITY_GROUPS)))})
        turns = _turn_limit(args, _DELEGATION_MAX_TURNS)

    try:
        from . import loop as _loop
        answer = await _loop.run_agent(
            hass,
            messages=[{"role": "user", "content": objective}],
            persona=persona, provider_name=provider_name, api_key=api_key,
            model=model, base_url=base_url, config=config,
            allowed_tools=allowed, max_iterations=turns, depth=depth + 1,
            profile_directive=directive,
        )
        reply = {"objective": objective, "result": answer}
        if not profile_label:
            reply["capability"] = capability
        else:
            reply["profile"] = profile_label
            if legacy_alias:
                reply["capability"] = capability  # the field an old caller reads
        return json.dumps(reply)
    except Exception as exc:
        # The raw exception is never put into a tool result. That JSON goes
        # back into the model's context and may be spoken to the user, and a
        # provider error string can hold more detail than should leave the
        # server. The full detail goes to the log only.
        _LOGGER.warning("delegate_task sub-agent failed: %s", exc)
        return json.dumps({"error": "sub-agent failed — could not complete "
                                    "the delegated objective"})
