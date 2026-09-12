"""Regression test: schema conversion must stay JSON-serializable even when
voluptuous_openapi.convert() returns *successfully* but still leaves a
non-serializable sentinel (UNSUPPORTED / _Unsupported) embedded somewhere
inside the schema it hands back. This is the actual bug fixed in
"Fix persistent tool-schema crash, and two hidden JARVIS leftovers"
(agent.py's _json_safe): the try/except around convert() only catches convert()
*raising*, not convert() succeeding with a broken value buried inside its
result - that surfaced later, opaquely, as "Object of type _Unsupported is
not JSON serializable" when the HTTP client tried to encode the request.

Companion to test_ha_tool_schema.py, which covers the "convert() succeeds
cleanly" and "convert() raises" paths; this file covers the third path those
two miss - "convert() succeeds but hands back a poisoned schema".
"""
import json
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "agent.py"


def _load_fns():
    # Same extraction approach as test_ha_tool_schema.py's _load_fn: pull just
    # the module-level defs these two functions depend on, rather than
    # importing the whole agent (which needs HA). Both _json_safe and
    # _ha_tools_to_openai_format must be present, or _ha_tools_to_openai_format
    # silently falls back to an empty schema instead of exercising the real
    # sentinel-stripping path.
    import types, ast, logging
    src = AGENT.read_text()
    tree = ast.parse(src)
    mod = types.ModuleType("agent_stub")
    mod.__dict__["_LOGGER"] = logging.getLogger("stub")
    mod.__dict__["Sequence"] = list
    mod.__dict__["json"] = json
    wanted = {"_json_safe", "_ha_tools_to_openai_format"}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in wanted:
            code = ast.get_source_segment(src, n)
            exec(compile(code, "<agent_fn>", "exec"), mod.__dict__)
    return mod.__dict__["_json_safe"], mod.__dict__["_ha_tools_to_openai_format"]


class _Unsupported:
    """Stand-in for voluptuous_openapi's real UNSUPPORTED / _Unsupported
    sentinel - a plain, non-JSON-serializable object."""


class _FakeTool:
    name = "test_tool"
    description = "desc"
    parameters = object()  # dummy; convert() is faked per-test


def test_json_safe_neutralises_embedded_sentinels():
    json_safe, _ = _load_fns()
    sentinel = _Unsupported()
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": sentinel},
        "c": [1, sentinel, "x"],
        "d": sentinel,
        "keep_none": None,
    }
    out = json_safe(schema)
    json.dumps(out)  # must not raise
    assert out["properties"]["a"] == {"type": "string"}
    # Nova's _json_safe nulls a bad value in place rather than dropping the
    # key (different from upstream's drop-the-key approach) - either is a
    # valid fix for "not JSON serializable"; assert Nova's actual contract.
    assert out["properties"]["b"] is None
    assert out["c"] == [1, None, "x"]
    assert out["d"] is None
    assert out["keep_none"] is None


def test_ha_tools_format_stays_serializable_when_convert_embeds_a_sentinel(monkeypatch):
    import sys, types
    _, fn = _load_fns()

    sentinel = _Unsupported()
    fake = types.ModuleType("voluptuous_openapi")
    fake.convert = lambda raw, custom_serializer=None: {
        "type": "object",
        "properties": {"x": {"type": "string"}, "y": sentinel},
    }
    monkeypatch.setitem(sys.modules, "voluptuous_openapi", fake)

    out = fn([_FakeTool()])
    json.dumps(out)  # the whole point - must not raise
    props = out[0]["function"]["parameters"]["properties"]
    assert props["x"] == {"type": "string"}
    assert props["y"] is None
