"""Home Assistant's LLM tools describe their parameters with voluptuous
schemas, and those cannot be sent as JSON.

_ha_tools_to_openai_format turns each one into a JSON Schema dict. Without
that, the request fails with "Object of type Schema is not JSON serializable"
and Nova drops back to the connectivity line (seen once, when the
conversation handler was un-orphaned in 7.53.4). A tool whose schema cannot be
converted gets an empty object schema so the rest still go out.
"""
import ast
import inspect
import json
import logging
import sys
import types
from pathlib import Path

import pytest

SOURCE = (Path(__file__).resolve().parents[2] / "custom_components" / "nova"
          / "agent_runtime" / "ha_tools.py")
EMPTY = {"type": "object", "properties": {}}


def _extract(*names):
    """Run only the named top-level functions from ha_tools.py in a bare
    namespace, with no Home Assistant and no package around them. If either
    function leans on anything else, a call fails here instead of quietly
    falling back to the empty schema."""
    text = SOURCE.read_text()
    space = types.ModuleType("ha_tools_bare")
    space.__dict__.update(_LOGGER=logging.getLogger("bare"), Sequence=list, json=json)
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(ast.get_source_segment(text, node), "<ha_tools>", "exec"),
                 space.__dict__)
    return [space.__dict__[n] for n in names]


class _Tool:
    def __init__(self, name, parameters, description="does a thing"):
        self.name = name
        self.description = description
        self.parameters = parameters


class _Unsupported:
    """Stands in for the converter's own marker for a type it cannot show."""


@pytest.fixture
def converter(monkeypatch):
    """Replace voluptuous_openapi with a converter the test controls."""
    seen = []
    box = {"result": {"type": "object", "properties": {"x": {"type": "string"}}}}

    def convert(raw, custom_serializer=None):
        seen.append((raw, custom_serializer))
        if isinstance(box["result"], Exception):
            raise box["result"]
        return box["result"]

    module = types.ModuleType("voluptuous_openapi")
    module.convert = convert
    monkeypatch.setitem(sys.modules, "voluptuous_openapi", module)
    return types.SimpleNamespace(seen=seen, box=box)


# ── real voluptuous schemas ──────────────────────────────────────────────────

def test_a_voluptuous_schema_comes_out_as_serialisable_json_schema():
    vol = pytest.importorskip("voluptuous")
    pytest.importorskip("voluptuous_openapi")
    to_openai = _extract("_json_safe", "_ha_tools_to_openai_format")[1]
    schema = vol.Schema({vol.Required("name"): str, vol.Optional("count"): int})
    out = to_openai([_Tool("do_thing", schema)])
    json.dumps(out)
    params = out[0]["function"]["parameters"]
    assert isinstance(params, dict)
    assert params.get("type") == "object"
    assert "name" in params.get("properties", {})
    assert params["properties"]["count"]["type"] == "integer"
    assert params["required"] == ["name"]


def test_something_that_is_not_a_schema_falls_back_to_the_empty_object():
    pytest.importorskip("voluptuous_openapi")
    to_openai = _extract("_json_safe", "_ha_tools_to_openai_format")[1]

    class Weird:
        pass

    out = to_openai([_Tool("weird", Weird())])
    json.dumps(out)
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}


# ── the wrapper, with the converter faked ────────────────────────────────────

def test_each_tool_becomes_a_function_entry(converter):
    to_openai = _extract("_json_safe", "_ha_tools_to_openai_format")[1]
    out = to_openai([_Tool("a", "schema-a", "first"), _Tool("b", "schema-b", "second")])
    params = {"type": "object", "properties": {"x": {"type": "string"}}}
    assert out == [
        {"type": "function",
         "function": {"name": "a", "description": "first", "parameters": params}},
        {"type": "function",
         "function": {"name": "b", "description": "second", "parameters": params}},
    ]
    assert list(out[0]) == ["type", "function"]
    assert list(out[0]["function"]) == ["name", "description", "parameters"]


def test_the_serializer_is_handed_to_the_converter(converter):
    to_openai = _extract("_json_safe", "_ha_tools_to_openai_format")[1]
    ser = object()
    to_openai([_Tool("a", "raw-schema")], custom_serializer=ser)
    assert converter.seen == [("raw-schema", ser)]
    to_openai([_Tool("a", "raw-schema")])
    assert converter.seen[-1] == ("raw-schema", None)


def test_no_tools_gives_an_empty_list(converter):
    to_openai = _extract("_json_safe", "_ha_tools_to_openai_format")[1]
    assert to_openai([]) == []
    assert converter.seen == []


def test_a_missing_description_becomes_an_empty_string(converter):
    to_openai = _extract("_json_safe", "_ha_tools_to_openai_format")[1]
    out = to_openai([_Tool("a", "s", None), _Tool("b", "s", "")])
    assert [t["function"]["description"] for t in out] == ["", ""]


def test_a_tool_with_no_parameters_gets_the_empty_object_schema(converter):
    to_openai = _extract("_json_safe", "_ha_tools_to_openai_format")[1]
    bare = types.SimpleNamespace(name="bare", description="d")
    out = to_openai([bare, _Tool("none", None)])
    assert [t["function"]["parameters"] for t in out] == [EMPTY, EMPTY]
    assert converter.seen == []


def test_an_empty_conversion_gets_the_empty_object_schema(converter):
    to_openai = _extract("_json_safe", "_ha_tools_to_openai_format")[1]
    converter.box["result"] = {}
    assert to_openai([_Tool("a", "s")])[0]["function"]["parameters"] == EMPTY


def test_without_the_converter_every_tool_gets_the_empty_object_schema(monkeypatch):
    to_openai = _extract("_json_safe", "_ha_tools_to_openai_format")[1]
    monkeypatch.setitem(sys.modules, "voluptuous_openapi", None)
    out = to_openai([_Tool("a", "s"), _Tool("b", "s")])
    assert [t["function"]["parameters"] for t in out] == [EMPTY, EMPTY]
    assert [t["function"]["name"] for t in out] == ["a", "b"]


def test_a_failed_conversion_is_logged_at_debug_and_the_rest_still_go_out(
        converter, caplog):
    to_openai = _extract("_json_safe", "_ha_tools_to_openai_format")[1]
    converter.box["result"] = ValueError("cannot show this")
    with caplog.at_level(logging.DEBUG, logger="bare"):
        out = to_openai([_Tool("odd", "s")])
    assert out[0]["function"]["parameters"] == EMPTY
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.DEBUG, "Nova: couldn't convert schema for HA tool odd: cannot show this")]


def test_the_log_line_names_a_tool_with_no_name_as_a_question_mark(converter, caplog):
    to_openai = _extract("_json_safe", "_ha_tools_to_openai_format")[1]
    converter.box["result"] = ValueError("bad")

    class Nameless:
        description = "d"
        parameters = "s"
        name = property(lambda self: (_ for _ in ()).throw(AttributeError()))

    with caplog.at_level(logging.DEBUG, logger="bare"):
        with pytest.raises(AttributeError):
            to_openai([Nameless()])
    assert caplog.records[0].getMessage() == (
        "Nova: couldn't convert schema for HA tool ?: bad")


def test_a_sentinel_left_inside_a_converted_schema_is_nulled(converter):
    to_openai = _extract("_json_safe", "_ha_tools_to_openai_format")[1]
    converter.box["result"] = {
        "type": "object",
        "properties": {"x": {"type": "string"}, "y": _Unsupported()},
    }
    out = to_openai([_Tool("a", "s")])
    json.dumps(out)
    props = out[0]["function"]["parameters"]["properties"]
    assert props["x"] == {"type": "string"}
    assert props["y"] is None


# ── _json_safe ───────────────────────────────────────────────────────────────

def test_json_safe_keeps_plain_json_values():
    (json_safe,) = _extract("_json_safe")
    plain = {"a": [1, 2.5, "x", True, None], "b": {"c": {"d": False}}, "e": ""}
    assert json_safe(plain) == plain
    assert json_safe(0) == 0 and json_safe("") == "" and json_safe(None) is None


def test_json_safe_turns_tuples_into_lists_and_copies_containers():
    (json_safe,) = _extract("_json_safe")
    source = {"t": (1, (2, 3)), "l": [1]}
    out = json_safe(source)
    assert out == {"t": [1, [2, 3]], "l": [1]}
    assert out is not source and out["l"] is not source["l"]


@pytest.mark.parametrize("value", [b"raw", {1, 2}, object(), _Unsupported(), len])
def test_json_safe_nulls_anything_that_is_not_plain_json(value):
    (json_safe,) = _extract("_json_safe")
    assert json_safe(value) is None
    assert json_safe({"k": value, "n": [value, 1]}) == {"k": None, "n": [None, 1]}


def test_json_safe_leaves_the_key_in_place_when_it_nulls_the_value():
    (json_safe,) = _extract("_json_safe")
    out = json_safe({"type": "object", "properties": {"b": _Unsupported()},
                     "c": [1, _Unsupported(), "x"], "d": _Unsupported(), "keep": None})
    json.dumps(out)
    assert out == {"type": "object", "properties": {"b": None},
                   "c": [1, None, "x"], "d": None, "keep": None}


# ── the real module ──────────────────────────────────────────────────────────

def test_the_agent_logger_keeps_the_name_it_always_had(load):
    mod = load("agent_runtime.ha_tools")
    assert mod._LOGGER.name == "jc.agent"


class _Wide:
    def __init__(self, a, b=1, *, c=2):
        pass


def test_ha_kwargs_keeps_only_what_the_constructor_accepts(load):
    mod = load("agent_runtime.ha_tools")
    assert mod._ha_kwargs(_Wide, a=1, c=3, user_prompt="x", extra=None) == {"a": 1, "c": 3}
    assert mod._ha_kwargs(_Wide) == {}


def test_ha_kwargs_with_a_var_keyword_constructor_drops_nothing_named_in_it(load):
    mod = load("agent_runtime.ha_tools")

    class Open:
        def __init__(self, **kw):
            pass

    assert mod._ha_kwargs(Open, a=1) == {}


def test_ha_kwargs_hands_back_everything_when_the_signature_cannot_be_read(load):
    mod = load("agent_runtime.ha_tools")
    assert mod._ha_kwargs(None, a=1, b=2) == {"a": 1, "b": 2}
    assert mod._ha_kwargs(42, a=1) == {"a": 1}


def test_ha_kwargs_works_on_a_dataclass_like_the_ones_home_assistant_ships(load):
    import dataclasses
    mod = load("agent_runtime.ha_tools")

    @dataclasses.dataclass
    class ToolInput:
        tool_name: str
        tool_args: dict

    assert mod._ha_kwargs(ToolInput, tool_name="t", tool_args={}, user_prompt="p") == {
        "tool_name": "t", "tool_args": {}}


def test_the_module_keeps_the_names_other_modules_import(load):
    mod = load("agent_runtime.ha_tools")
    for name in ("_ha_kwargs", "_json_safe", "_ha_tools_to_openai_format"):
        assert callable(getattr(mod, name))
    assert not inspect.iscoroutinefunction(mod._ha_tools_to_openai_format)
