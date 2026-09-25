"""Extract Nova's public compatibility contracts from the current source.

Shared by tests/unit/test_public_contracts.py (compared against the reviewed
fixtures in tests/fixtures/contracts/) and by the PHACC contract tests. Every
extractor returns plain, deterministic, JSON-ready data: sorted keys, sorted
inventories, no timestamps, IDs, reprs or runtime values.

Static (AST / YAML) extraction is used where the module can't be imported
under the unit-test fakes — websocket.py, __init__.py, config_flow.py and the
store modules all pull in Home Assistant at import time. The agent's tool
inventory is read from the real module (loaded through the unit fakes by the
caller) because it is plain data.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
COMP = REPO / "custom_components" / "nova"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "contracts"


def canonical(obj: Any) -> Any:
    """Round-trip through sorted JSON so dict key order never matters."""
    return json.loads(json.dumps(obj, sort_keys=True, default=_jsonable))


def _jsonable(obj: Any):
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError(f"not JSON-serializable in a contract: {type(obj).__name__}")


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def dump(obj: Any) -> str:
    return json.dumps(canonical(obj), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _tree(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _const(node) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


# ── Services ────────────────────────────────────────────────────────────────

def services_contract() -> dict:
    """services.yaml, reduced to the public surface of each service: its
    fields, whether each is required, and documented defaults, selector type,
    options and numeric bounds."""
    import yaml
    doc = yaml.safe_load((COMP / "services.yaml").read_text(encoding="utf-8"))
    out = {}
    for name, spec in sorted(doc.items()):
        fields = {}
        for fname, f in sorted(((spec or {}).get("fields") or {}).items()):
            f = f or {}
            entry = {"required": bool(f.get("required", False))}
            if "default" in f:
                entry["default"] = f["default"]
            sel = f.get("selector") or {}
            if sel:
                (stype, sconf), = sel.items()
                entry["selector"] = stype
                sconf = sconf or {}
                if "options" in sconf:
                    entry["options"] = [o["value"] if isinstance(o, dict) else o
                                        for o in sconf["options"]]
                for bound in ("min", "max", "multiple", "domain"):
                    if bound in sconf:
                        entry[bound] = sconf[bound]
            fields[fname] = entry
        out[name] = {"fields": fields}
    return out


# Since Phase 4 every Nova service is registered by services.py, once, through
# its _register(name, handler, schema) helper (which wraps
# hass.services.async_register with a has_service guard). speak and
# process_intent pass proactive_audio's SERVICE_* names and SPEAK_SCHEMA /
# PROCESS_INTENT_SCHEMA, which live in proactive_audio.py.
_SERVICES_MODULE = COMP / "services.py"
_SERVICE_NAME_MODULES = (COMP / "services.py", COMP / "proactive_audio.py")


def _string_consts() -> dict:
    consts = {}
    for path in _SERVICE_NAME_MODULES:
        for node in ast.walk(_tree(path)):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                consts[node.targets[0].id] = node.value.value
    return consts


def service_registrations() -> list:
    """(name, schema node or None) for every _register(...) call in
    services.py, in source order."""
    consts, out = _string_consts(), []
    for node in ast.walk(_tree(_SERVICES_MODULE)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_register" and len(node.args) >= 2):
            arg = node.args[0]
            name = arg.value if isinstance(arg, ast.Constant) else consts[arg.id]
            schema = node.args[2] if len(node.args) > 2 else next(
                (k.value for k in node.keywords if k.arg == "schema"), None)
            out.append((name, schema))
    return out


def registered_services() -> list:
    """Every nova service name services.py registers."""
    return sorted({name for name, _ in service_registrations()})


def service_schema_keys() -> dict:
    """{service: {field: "required"|"optional"}} from the vol.Schema each
    service is registered with in services.py; services registered without
    a schema map to None. speak and process_intent are left out, as before
    Phase 4: their schemas are proactive_audio's SPEAK_SCHEMA and
    PROCESS_INTENT_SCHEMA, pinned by the PHACC contract test instead."""
    out = {}
    for name, schema in service_registrations():
        if isinstance(schema, ast.Name):
            continue
        out[name] = _vol_keys(schema) if schema is not None else None
    return dict(sorted(out.items()))


def _vol_keys(schema_call) -> dict | None:
    if not (isinstance(schema_call, ast.Call) and schema_call.args
            and isinstance(schema_call.args[0], ast.Dict)):
        return None
    keys = {}
    for k in schema_call.args[0].keys:
        if isinstance(k, ast.Call) and k.args and isinstance(k.args[0], ast.Constant):
            kind = k.func.attr if isinstance(k.func, ast.Attribute) else getattr(k.func, "id", "")
            keys[k.args[0].value] = "required" if kind == "Required" else "optional"
    return dict(sorted(keys.items()))


# ── WebSocket ───────────────────────────────────────────────────────────────

def websocket_contract() -> dict:
    """Every nova/* command declared in websocket.py: request fields
    (required/optional, enum options, defaults), admin gate, its stable error
    codes, and the top-level response keys written as literals in the
    handler (a lower bound: keys added through a variable or ** spread are
    pinned from real calls in websocket_responses.json instead)."""
    tree = _tree(COMP / "websocket.py")
    registered = {
        ast.unparse(n.args[1]) for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "async_register_command"
        and len(n.args) >= 2
    }
    out = {}
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        cmd = next((d for d in fn.decorator_list
                    if isinstance(d, ast.Call) and ast.unparse(d.func).endswith("websocket_command")),
                   None)
        if cmd is None:
            continue
        ctype, fields = None, {}
        for k, v in zip(cmd.args[0].keys, cmd.args[0].values):
            if not (isinstance(k, ast.Call) and k.args and isinstance(k.args[0], ast.Constant)):
                continue
            name = k.args[0].value
            if name == "type":
                ctype = _const(v)
                continue
            field = {"required": k.func.attr == "Required"}
            for kw in k.keywords:
                if kw.arg == "default":
                    field["default"] = _const(kw.value)
            if (isinstance(v, ast.Call) and ast.unparse(v.func) == "vol.In" and v.args):
                opts = _const(v.args[0])
                if isinstance(opts, (list, tuple, set)):
                    field["options"] = sorted(opts)
            fields[name] = field
        admin = any(ast.unparse(d).endswith("require_admin") for d in fn.decorator_list)
        resp, errors = set(), set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr == "send_result" and len(n.args) > 1 and isinstance(n.args[1], ast.Dict):
                    resp |= {k.value for k in n.args[1].keys if isinstance(k, ast.Constant)}
                if n.func.attr == "send_error" and len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                    errors.add(n.args[1].value)
        out[ctype] = {
            "admin": admin,
            "fields": dict(sorted(fields.items())),
            "registered": fn.name in registered,
            "literal_response_keys": sorted(resp),
            "error_codes": sorted(errors),
        }
    return dict(sorted(out.items()))


def panel_writable_keys() -> list:
    tree = _tree(COMP / "websocket.py")
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Set)
                and any(isinstance(t, ast.Name) and t.id == "PANEL_WRITABLE_KEYS" for t in node.targets)):
            # A set, so a key listed twice in the literal is one key.
            return sorted({e.value for e in node.value.elts if isinstance(e, ast.Constant)})
    raise AssertionError("PANEL_WRITABLE_KEYS not found in websocket.py")


def panel_data_backend_keys() -> list:
    """Top-level keys ws_get_panel_data puts in its `result` payload."""
    tree = _tree(COMP / "websocket.py")
    fn = next(f for f in tree.body if isinstance(f, ast.AsyncFunctionDef)
              and f.name == "ws_get_panel_data")
    keys = set()
    for n in ast.walk(fn):
        if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "result"
                                              for t in n.targets)
                and isinstance(n.value, ast.Dict)):
            keys |= {k.value for k in n.value.keys if isinstance(k, ast.Constant)}
        if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                and n.value.id == "result" and isinstance(n.slice, ast.Constant)
                and isinstance(n.ctx, ast.Store)):
            keys.add(n.slice.value)
    return sorted(keys)


def panel_smoke_payload_keys() -> list:
    """Top-level keys of the realistic get_panel_data payload the panel smoke
    test renders (scripts/smoke_panel.js `const PANEL`): what the panel reads."""
    src = (REPO / "scripts" / "smoke_panel.js").read_text(encoding="utf-8")
    start = src.index("const PANEL = {")
    depth, keys, i = 0, set(), start + len("const PANEL = ")
    for m in re.finditer(r"[{}]|\n  ([A-Za-z_][A-Za-z0-9_]*):", src[i:]):
        tok = m.group(0)
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1:
            keys.add(m.group(1))
    return sorted(keys)


def panel_ws_commands() -> list:
    src = (COMP / "frontend" / "nova-panel.js").read_text(encoding="utf-8")
    return sorted(set(re.findall(r"""type:\s*["'](nova/[a-z_]+)["']""", src)))


def panel_service_calls() -> list:
    src = (COMP / "frontend" / "nova-panel.js").read_text(encoding="utf-8")
    calls = set(re.findall(r"""callService\(\s*["']nova["']\s*,\s*["']([a-z_]+)["']""", src))
    calls |= set(re.findall(r"""["']nova\.([a-z_]+)["']""", src))
    return sorted(calls)


# ── Configuration ───────────────────────────────────────────────────────────

def config_contract() -> dict:
    const_tree = _tree(COMP / "const.py")
    conf, domain = {}, None
    for node in const_tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.startswith("CONF_"):
                conf[name] = _const(node.value)
            if name == "DOMAIN":
                domain = _const(node.value)
    schema_version = None
    for node in _tree(COMP / "migrations.py").body:
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "CURRENT_SCHEMA_VERSION"):
            schema_version = _const(node.value)
    flow = (COMP / "config_flow.py").read_text(encoding="utf-8")
    unique_id_calls = sorted(set(re.findall(r"async_set_unique_id\(([^)]*)\)", flow)))
    manifest = json.loads((COMP / "manifest.json").read_text(encoding="utf-8"))
    return {
        "domain": domain,
        "manifest": {k: manifest[k] for k in ("domain", "config_flow", "integration_type", "iot_class")},
        "config_entry_schema_version": schema_version,
        "config_entry_unique_id_calls": unique_id_calls,
        "conf_constants": dict(sorted(conf.items())),
        "panel_writable_keys": panel_writable_keys(),
    }


# ── Agent tools ─────────────────────────────────────────────────────────────

def agent_contract(agent) -> dict:
    """From the real agent module (loaded by the caller)."""
    tools = {}
    for spec in agent.NOVA_TOOLS:
        fn = spec["function"]
        params = fn.get("parameters") or {}
        tools[fn["name"]] = {
            "parameters": params,
            "required": sorted(params.get("required") or []),
        }
    homer = agent.AGENT_PROFILES["homer"]
    return canonical({
        "tools": dict(sorted(tools.items())),
        "dispatch_names": sorted(agent._TOOL_MAP),
        "mutating_tools": sorted(agent._MUTATING_TOOL_NAMES),
        "subagent_denied_tools": sorted(agent._SUBAGENT_DENY),
        "capability_groups": {k: sorted(v) for k, v in sorted(agent.CAPABILITY_GROUPS.items())},
        "capability_profile_aliases": dict(sorted(agent._LEGACY_CAPABILITY_PROFILE_ALIASES.items())),
        "profiles": {
            "homer": {
                "label": homer["label"],
                "max_turns": homer["max_turns"],
                "tools": sorted(homer["tools"]),
            },
        },
        "max_delegation_depth": agent.MAX_DELEGATION_DEPTH,
        "delegation_max_turns": agent._DELEGATION_MAX_TURNS,
        "max_tool_iterations": agent.MAX_TOOL_ITERATIONS,
    })


def agent_tool_specs(agent) -> dict:
    """The exact tool definitions the model is offered, in order — names,
    descriptions and schemas — plus the slim-retry subset and HOMER's
    directive. Stricter than agent_contract(): a reworded description or a
    reordered tool list is a prompt-behaviour change and must show up here."""
    return {
        "tools": [dict(t) for t in agent.NOVA_TOOLS],
        "slim_tools": sorted(agent._SLIM_TOOLS),
        "homer_directive": agent._HOMER_DIRECTIVE,
    }


# ── Storage identity ────────────────────────────────────────────────────────

_CONFIG_ROOT = "/config/"


def storage_contract() -> dict:
    """Persisted file paths (relative to HA's config dir) and SQLite table
    names each module owns, read from literals in the source. Nothing is
    opened: this never touches a real config directory."""
    out = {}
    for path in sorted(COMP.rglob("*.py")):
        rel_mod = str(path.relative_to(COMP).with_suffix("")).replace("/", ".")
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        paths, tables = set(), set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith(_CONFIG_ROOT):
                rel = n.value[len(_CONFIG_ROOT):]
                if rel and "{" not in rel:
                    paths.add(rel)
            if (isinstance(n, ast.Call) and ast.unparse(n.func).endswith("config.path")
                    and n.args and all(isinstance(a, ast.Constant) for a in n.args)):
                paths.add("/".join(a.value for a in n.args))
        tables |= set(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)?\s+([a-z_]+)\s*\(", src))
        if paths or tables:
            out[rel_mod] = {"paths": sorted(paths), "tables": sorted(tables)}
    return out
