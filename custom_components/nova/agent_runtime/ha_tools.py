"""Turns Home Assistant LLM API tools into plain JSON-schema tool entries."""
from __future__ import annotations

import json
import logging
from typing import Sequence


# The agent logs under …nova.agent whichever module the line comes from, so
# any filter or level a user set for the agent still applies.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


def _ha_kwargs(cls, **kwargs):
    """Pass on only the keyword arguments `cls` can take.

    Home Assistant keeps changing the fields of its LLM API classes (2026.8
    moved the context from ToolInput into LLMContext and removed
    user_prompt), so the safe call is the overlap between what we have and
    what this version accepts (v7.21.1). If the signature cannot be read, all
    the arguments go through untouched."""
    try:
        import inspect
        accepted = inspect.signature(cls).parameters
    except Exception:
        return kwargs
    return {name: value for name, value in kwargs.items() if name in accepted}


def _json_safe(obj):
    """Copy `obj`, replacing every value that is not plain JSON with None.

    voluptuous_openapi.convert() can return without raising and still leave
    its UNSUPPORTED marker (or another custom object) inside the result, for
    example as a field default or deep in an items or anyOf entry. A try
    around the call does not see that. It only shows up later, when the HTTP
    client fails with "Object of type _Unsupported is not JSON serializable"
    and nothing says which tool caused it. Cleaning the converted schema
    before it reaches a request avoids that."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {key: _json_safe(item) for key, item in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(item) for item in obj]
    return None


def _ha_tools_to_openai_format(ha_tools: Sequence, custom_serializer=None) -> list[dict]:
    """Build OpenAI function-calling entries from HA LLM API tools.

    A tool's parameters are a voluptuous Schema. That cannot go into a JSON
    request ("Object of type Schema is not JSON serializable"), so each one is
    converted with voluptuous_openapi, using the API's own serializer so HA
    selectors (entity ids, areas and so on) come out right. A tool whose
    schema fails to convert, or converts to something that still is not JSON
    (see _json_safe), gets an empty object schema. One odd tool must not take
    the whole call down."""
    try:
        from voluptuous_openapi import convert
    except Exception:
        convert = None

    entries = []
    for tool in ha_tools:
        schema = None
        source = getattr(tool, "parameters", None)
        if convert is not None and source is not None:
            try:
                schema = _json_safe(convert(source, custom_serializer=custom_serializer))
                json.dumps(schema)
            except Exception as exc:
                schema = None
                _LOGGER.debug(
                    "Nova: couldn't convert schema for HA tool %s: %s",
                    getattr(tool, "name", "?"), exc)
        entries.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": schema or {"type": "object", "properties": {}},
            },
        })
    return entries
