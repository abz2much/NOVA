"""Home Assistant LLM API tool translation into normalized JSON-schema tools."""
from __future__ import annotations

import json
import logging
from typing import Sequence


# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


def _ha_kwargs(cls, **kwargs):
    """Keep only the kwargs that cls's constructor accepts. Home Assistant's LLM
    API changed fields across versions — 2026.8 moved the context out of
    ToolInput into LLMContext and dropped user_prompt — so we pass the
    intersection and stay compatible with old and new HA (v7.21.1). Never raises."""
    try:
        import inspect
        allowed = inspect.signature(cls).parameters
        return {k: v for k, v in kwargs.items() if k in allowed}
    except Exception:
        return kwargs


# ── Main agent loop ─────────────────────────────────────────────────────────

def _json_safe(obj):
    """Recursively strip anything that isn't plain JSON-serializable data.

    Belt-and-braces alongside the try/except in _ha_tools_to_openai_format
    below: voluptuous_openapi.convert() can return *successfully* — no
    exception — while still leaving its own UNSUPPORTED sentinel (or some
    other custom object) embedded as a value somewhere inside the schema
    it hands back, e.g. a field's "default" or buried in "items"/"anyOf"
    for one selector type it doesn't fully know how to represent. The
    try/except only catches convert() *raising*; it does nothing for a
    schema that "successfully" comes back already broken. That one bad
    leaf then only surfaces much later, as an opaque "Object of type
    _Unsupported is not JSON serializable" when the HTTP client tries to
    encode the whole request — with no indication of which tool or field
    caused it. So instead of trusting the library's output, walk it and
    drop anything that isn't a plain JSON type before it goes anywhere
    near a request.
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return None


def _ha_tools_to_openai_format(ha_tools: Sequence, custom_serializer=None) -> list[dict]:
    """Convert HA LLM API tool definitions to OpenAI function-calling format.

    HA tools carry their parameters as a voluptuous Schema, which is NOT JSON
    serializable — passing it straight through makes the whole LLM request fail
    with "Object of type Schema is not JSON serializable". Convert each schema to
    a JSON Schema dict via voluptuous_openapi, using the API's custom serializer
    so HA's selector types (entity ids, areas, etc.) render correctly. If a single
    tool's schema can't be converted — or converts but still isn't actually
    JSON-safe (see _json_safe) — fall back to an empty object schema for that
    tool so one odd tool never breaks the entire call.
    """
    try:
        from voluptuous_openapi import convert
    except Exception:
        convert = None
    tools = []
    for t in ha_tools:
        params = None
        raw = getattr(t, "parameters", None)
        if raw is not None and convert is not None:
            try:
                params = _json_safe(convert(raw, custom_serializer=custom_serializer))
                json.dumps(params)  # verify — _json_safe should guarantee this
            except Exception as exc:
                _LOGGER.debug(
                    "Nova: couldn't convert schema for HA tool %s: %s",
                    getattr(t, "name", "?"), exc)
                params = None
        if not params:
            params = {"type": "object", "properties": {}}
        tools.append({
            "type": "function",
            "function": {
                "name":        t.name,
                "description": t.description or "",
                "parameters":  params,
            },
        })
    return tools
