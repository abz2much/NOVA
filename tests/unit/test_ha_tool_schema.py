"""Guard: HA LLM tools carry voluptuous schemas, which are NOT JSON serializable.

_ha_tools_to_openai_format must convert each tool's parameters to a JSON Schema
dict, or the whole LLM request dies with "Object of type Schema is not JSON
serializable" and Nova falls back to the connectivity line (regression seen
once the conversation handler was un-orphaned in 7.53.4).
"""
import json
import importlib.util
from pathlib import Path

import pytest

vol = pytest.importorskip("voluptuous")
pytest.importorskip("voluptuous_openapi")

AGENT = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "agent.py"


def _load_fn():
    # Load just the function's source region to avoid importing the whole agent
    # (which needs HA). Exec the module-level defs it depends on: _json_safe
    # (the sentinel-stripping helper _ha_tools_to_openai_format calls) and
    # _ha_tools_to_openai_format itself. Missing either one here doesn't error
    # loudly - the function's own try/except swallows the resulting NameError
    # and silently falls back to an empty schema, which made this harness pass
    # even while under-testing the real function (fixed alongside adding
    # test_agent_tool_schema.py, which exercises the sentinel-stripping path
    # directly).
    import types, ast, logging, json as json_mod
    src = AGENT.read_text()
    tree = ast.parse(src)
    mod = types.ModuleType("agent_stub")
    mod.__dict__["_LOGGER"] = logging.getLogger("stub")
    mod.__dict__["Sequence"] = list
    mod.__dict__["json"] = json_mod
    wanted = {"_json_safe", "_ha_tools_to_openai_format"}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in wanted:
            code = ast.get_source_segment(src, n)
            exec(compile(code, "<agent_fn>", "exec"), mod.__dict__)
    return mod.__dict__["_ha_tools_to_openai_format"]


class _FakeTool:
    def __init__(self, name, params):
        self.name = name
        self.description = "does a thing"
        self.parameters = params


def test_voluptuous_schema_is_converted_and_serializable():
    fn = _load_fn()
    schema = vol.Schema({vol.Required("name"): str, vol.Optional("count"): int})
    out = fn([_FakeTool("do_thing", schema)])
    # Must be JSON serializable — this is the exact failure being fixed
    json.dumps(out)
    params = out[0]["function"]["parameters"]
    assert isinstance(params, dict)
    assert params.get("type") == "object"
    assert "name" in params.get("properties", {})


def test_bad_schema_falls_back_without_raising():
    fn = _load_fn()

    class Weird:  # not a real schema; convert should fail -> fallback
        pass

    out = fn([_FakeTool("weird", Weird())])
    json.dumps(out)  # still serializable
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}
