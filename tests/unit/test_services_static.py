"""Phase 4 process-lifetime service registration — the static half.

services.py registers Nova's 32 services once, from async_setup at
integration scope. Registration survives reload, unload and setup failure,
so every handler must resolve the loaded entry and its NovaRuntime per call.
The PHACC half (tests/integration/test_services_lifecycle.py) drives the
real lifecycle; this file proves from source that:

* every registration lives in services.py, and nothing removes a service,
* __init__.py defines async_setup, which calls async_setup_services,
* the 32 names and schemas are the pinned contract, and lockdown, remember,
  forget and create_automation stay schema-less,
* registration captures no entry, client, sentinel or runtime config, and
  every ordinary handler calls the loaded-entry resolver first,
* nova.speak buffers only through the narrow SETUP_IN_PROGRESS check,
* no Nova state or registration flag goes into hass.data,
* nova.routine still delegates through routines.py's safety path, and
  nova.lockdown passes the lifecycle check before touching cognitive_core,
* the service-state error translations are aligned in every language.

Focused run:
    python -m pytest tests/unit/test_services_static.py -q
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest

import contract_extract as ce

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
SERVICES = COMP / "services.py"
INIT = COMP / "__init__.py"

SERVICE_ERROR_KEYS = {"no_entry", "not_loaded", "reloading", "setup_failed",
                      "multiple_entries"}
SCHEMA_LESS_WITH_FIELDS = {"lockdown", "remember", "forget", "create_automation"}
RESOLVER = "async_resolve_loaded"

_SERVICES_TREE = ast.parse(SERVICES.read_text(encoding="utf-8"))
_INIT_TREE = ast.parse(INIT.read_text(encoding="utf-8"))


def _func(tree: ast.AST, name: str) -> ast.AST:
    found = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(found) == 1, (name, len(found))
    return found[0]


def _body(fn) -> list[ast.stmt]:
    """fn's statements without its docstring."""
    return fn.body[1:] if ast.get_docstring(fn) else fn.body


def _is_call(node: ast.AST, name: str) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == name)


def _setup_services() -> ast.FunctionDef:
    return _func(_SERVICES_TREE, "async_setup_services")


def _handlers() -> dict[str, ast.AsyncFunctionDef]:
    """{service name: handler} from the _register(...) calls."""
    setup = _setup_services()
    local = {n.name: n for n in setup.body if isinstance(n, ast.AsyncFunctionDef)}
    consts = ce._string_consts()
    out = {}
    for stmt in setup.body:
        if isinstance(stmt, ast.Expr) and _is_call(stmt.value, "_register"):
            name_node, handler = stmt.value.args[0], stmt.value.args[1]
            name = (name_node.value if isinstance(name_node, ast.Constant)
                    else consts[name_node.id])
            out[name] = local[handler.id]
    return out


def _package_modules():
    for path in sorted(COMP.rglob("*.py")):
        yield str(path.relative_to(COMP)), ast.parse(path.read_text(encoding="utf-8"))


def _services_calls(tree: ast.AST, attr: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == attr
            and ast.unparse(n.func.value).endswith("services")]


# ── 1. Registration lives only in services.py ───────────────────────────────

def test_all_service_registrations_are_in_services_py():
    where = {rel: len(_services_calls(tree, "async_register"))
             for rel, tree in _package_modules()}
    assert {rel: n for rel, n in where.items() if n} == {"services.py": 1}
    # ... and that one call is the has_service-guarded _register helper.
    register = _func(_SERVICES_TREE, "_register")
    guard = register.body[0]
    assert isinstance(guard, ast.If)
    assert ast.unparse(guard.test) == "not hass.services.has_service(DOMAIN, name)"
    assert _services_calls(guard, "async_register")


def test_every_service_is_registered_exactly_once():
    names = [name for name, _ in ce.service_registrations()]
    assert len(names) == 32 and len(set(names)) == 32


# ── 2. async_setup ──────────────────────────────────────────────────────────

def test_init_defines_async_setup_that_registers_services():
    setup = _func(_INIT_TREE, "async_setup")
    assert isinstance(setup, ast.AsyncFunctionDef)
    assert [a.arg for a in setup.args.args] == ["hass", "config"]
    stmts = _body(setup)
    assert ast.unparse(stmts[0]) == "async_setup_services(hass)"
    assert ast.unparse(stmts[-1]) == "return True"
    src = INIT.read_text(encoding="utf-8")
    assert "CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)" in src


def test_setup_entry_does_not_register_services():
    entry_setup = _func(_INIT_TREE, "async_setup_entry")
    calls = {ast.unparse(n.func) for n in ast.walk(entry_setup) if isinstance(n, ast.Call)}
    assert "async_setup_services" not in calls
    assert "_register_services" not in calls
    assert not _services_calls(entry_setup, "async_register")


# ── 3. Nothing removes a service ────────────────────────────────────────────

def test_unload_never_removes_nova_services():
    unload = _func(_INIT_TREE, "async_unload_entry")
    assert not _services_calls(unload, "async_remove")
    for rel, tree in _package_modules():
        assert not _services_calls(tree, "async_remove"), rel


def test_proactive_audio_neither_registers_nor_removes_services():
    tree = ast.parse((COMP / "proactive_audio.py").read_text(encoding="utf-8"))
    assert not _services_calls(tree, "async_register")
    assert not _services_calls(tree, "async_remove")
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "async_register_services" not in names


# ── 4 & 5. The public contract ──────────────────────────────────────────────

# Exact source of every registered schema, taken from the pre-Phase-4
# registrations (__init__.py and proactive_audio.py at f4d1091). Any
# change to a field, default, validator or range fails here.
PINNED_SCHEMAS = {
    'add_reminder': "vol.Schema({vol.Required('label'): cv.string, vol.Required('trigger_at'): cv.string, vol.Optional('repeat'): vol.In(['daily', 'weekly', 'hourly']), vol.Optional('require_home', default=True): cv.boolean, vol.Optional('respect_quiet', default=True): cv.boolean})",
    'analyze_camera': "vol.Schema({vol.Required('entity_id'): cv.entity_id, vol.Optional('prompt'): cv.string, vol.Optional('announce', default=True): cv.boolean, vol.Optional('frames'): vol.All(vol.Coerce(int), vol.Range(min=1, max=6)), vol.Optional('interval'): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=5))})",
    'analyze_on_event': "vol.Schema({vol.Required('entity_id'): cv.entity_id, vol.Optional('reason', default='Activity detected'): cv.string})",
    'backup': None,
    'briefing': "vol.Schema({vol.Optional('announce', default=True): cv.boolean, vol.Optional('include_weather', default=True): cv.boolean, vol.Optional('include_calendar', default=True): cv.boolean, vol.Optional('include_presence', default=True): cv.boolean, vol.Optional('include_events', default=True): cv.boolean, vol.Optional('include_energy', default=True): cv.boolean, vol.Optional('hours', default=12): vol.All(int, vol.Range(min=1, max=48))})",
    'check_packages': "vol.Schema({vol.Optional('entity_id'): cv.entity_id})",
    'conversation_summary': "vol.Schema({vol.Optional('hours', default=24): vol.All(int, vol.Range(min=1, max=168)), vol.Optional('device_id'): cv.string, vol.Optional('announce', default=True): cv.boolean, vol.Optional('store', default=True): cv.boolean})",
    'create_automation': None,
    'database_purge': "vol.Schema({vol.Optional('days', default=30): vol.All(int, vol.Range(min=1, max=365))})",
    'database_stats': None,
    'diagnose_doorbell': None,
    'forget': None,
    'lockdown': None,
    'nap': "vol.Schema({vol.Optional('duration_minutes', default=30): vol.All(int, vol.Range(min=1, max=480))})",
    'observer_start': None,
    'observer_status': None,
    'observer_stop': None,
    'process_intent': 'PROCESS_INTENT_SCHEMA',
    'remember': None,
    'replay_policy': "vol.Schema({vol.Required('kind'): str, vol.Optional('min_samples'): vol.All(int, vol.Range(min=1, max=100000))})",
    'restore': "vol.Schema({vol.Optional('archive'): str})",
    'routine': "vol.Schema({vol.Required('name'): cv.string})",
    'scene_by_intent': "vol.Schema({vol.Required('intent'): cv.string, vol.Optional('announce', default=True): cv.boolean})",
    'sentinel_start': None,
    'sentinel_stop': None,
    'shush': "vol.Schema({vol.Optional('entity_id'): cv.string, vol.Optional('category'): cv.string, vol.Optional('all'): cv.boolean})",
    'speak': 'SPEAK_SCHEMA',
    'test_notify': None,
    'test_routing': None,
    'test_tts': None,
    'train_doorbell_backlog': "vol.Schema({vol.Optional('limit', default=40): vol.Coerce(int)})",
    'unshush': "vol.Schema({vol.Optional('entity_id'): cv.string, vol.Optional('category'): cv.string})",
}
PINNED_PROACTIVE_SCHEMAS = {
    'PROCESS_INTENT_SCHEMA': "vol.Schema({vol.Required('phrase'): cv.string, vol.Required('target_area'): cv.string, vol.Optional('user_id'): cv.string})",
    'SPEAK_SCHEMA': "vol.Schema({vol.Required('message'): cv.string, vol.Required('target_area'): cv.string, vol.Optional('critical', default=False): cv.boolean, vol.Optional('user_id'): cv.string, vol.Optional('expect_response', default=False): cv.boolean, vol.Optional('confirm_intent'): cv.string})",
}


def test_every_schema_is_exactly_the_pre_phase_4_schema():
    current = {name: (None if schema is None else ast.unparse(schema))
               for name, schema in ce.service_registrations()}
    assert current == PINNED_SCHEMAS


def test_proactive_audio_schemas_are_unchanged():
    tree = ast.parse((COMP / "proactive_audio.py").read_text(encoding="utf-8"))
    current = {n.targets[0].id: ast.unparse(n.value) for n in tree.body
               if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
               and n.targets[0].id in PINNED_PROACTIVE_SCHEMAS}
    assert current == PINNED_PROACTIVE_SCHEMAS


def test_names_and_schemas_match_the_pinned_contract():
    fixture = ce.load_fixture("services")
    assert set(ce.registered_services()) == set(fixture["documented"])
    assert ce.canonical(ce.service_schema_keys()) == ce.canonical(fixture["registered_schemas"])


def test_speak_and_process_intent_keep_their_schemas():
    regs = dict(ce.service_registrations())
    assert ast.unparse(regs["speak"]) == "SPEAK_SCHEMA"
    assert ast.unparse(regs["process_intent"]) == "PROCESS_INTENT_SCHEMA"


def test_four_documented_services_stay_schema_less():
    regs = dict(ce.service_registrations())
    documented = ce.services_contract()
    for name in SCHEMA_LESS_WITH_FIELDS:
        assert regs[name] is None, name
        assert documented[name]["fields"], name     # services.yaml documents fields
    pinned = ce.load_fixture("services")["registered_schemas"]
    assert {n for n in SCHEMA_LESS_WITH_FIELDS if pinned[n] is None} == SCHEMA_LESS_WITH_FIELDS


def _own_nodes(fn) -> list[ast.AST]:
    """fn's nodes, not descending into nested functions or lambdas."""
    out, stack = [], list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        out.append(n)
        stack.extend(ast.iter_child_nodes(n))
    return out


def test_no_service_supports_a_response():
    src = SERVICES.read_text(encoding="utf-8")
    assert "SupportsResponse" not in src and "supports_response" not in src
    register = ast.unparse(_func(_SERVICES_TREE, "_register"))
    assert "hass.services.async_register(DOMAIN, name, handler, schema=schema)" in register
    for name, handler in _handlers().items():
        returns = [n for n in _own_nodes(handler) if isinstance(n, ast.Return)
                   and n.value is not None]
        assert returns == [], name


# ── 6. No capture at registration ───────────────────────────────────────────

CAPTURE_NAMES = {"entry", "runtime", "client", "llm_client", "groq_client",
                 "sentinel", "runtime_config", "rc", "cfg", "config"}


def test_registration_scope_binds_nothing_but_handlers():
    """async_setup_services' own scope defines handlers and registers them.
    The only names a handler can close over are hass and those
    definitions, so none can hold an entry, client, sentinel or config."""
    setup = _setup_services()
    for stmt in _body(setup):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assert isinstance(stmt, ast.Expr) and _is_call(stmt.value, "_register"), (
            ast.unparse(stmt))
    assert [a.arg for a in setup.args.args] == ["hass"]
    for node in ast.walk(_SERVICES_TREE):
        assert not isinstance(node, (ast.Global, ast.Nonlocal))


def test_services_module_holds_no_live_state():
    module_names = set()
    for stmt in _SERVICES_TREE.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            module_names |= {t.id for t in targets if isinstance(t, ast.Name)}
    assert module_names == {"_LOGGER", "_STATE_ERRORS"}


@pytest.mark.parametrize("service", sorted(ce.registered_services()))
def test_handler_reads_dependencies_only_from_the_resolved_runtime(service):
    handler = _handlers()[service]
    loads = {n.id for n in ast.walk(handler)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    stores = {n.id for n in ast.walk(handler)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    stores |= {a.arg for n in ast.walk(handler)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
               for a in n.args.args}
    # Any capture-shaped name a handler reads is one it bound itself.
    assert (loads & CAPTURE_NAMES) <= stores, service
    # The client and sentinel are only ever reached through the runtime.
    for n in ast.walk(handler):
        if isinstance(n, ast.Attribute) and n.attr in ("client", "sentinel"):
            assert ast.unparse(n.value) == "runtime", (service, ast.unparse(n))


# ── 7. Ordinary handlers resolve the loaded entry first ─────────────────────

@pytest.mark.parametrize("service", sorted(set(ce.registered_services()) - {"speak"}))
def test_ordinary_handler_resolves_the_loaded_entry_first(service):
    first = _body(_handlers()[service])[0]
    assert ast.unparse(first) == f"entry, runtime = {RESOLVER}(hass)", service


def test_resolver_checks_lifecycle_then_runtime():
    resolve = _func(_SERVICES_TREE, RESOLVER)
    assert [ast.unparse(s) for s in _body(resolve)] == [
        "entry = async_get_loaded_entry(hass)",
        "return (entry, get_runtime(entry))",
    ]
    loaded = ast.unparse(_func(_SERVICES_TREE, "async_get_loaded_entry"))
    assert "entries = _nova_entries(hass)" in loaded
    assert "async_entries(DOMAIN, include_ignore=False)" in ast.unparse(
        _func(_SERVICES_TREE, "_nova_entries"))
    assert "_service_error('no_entry')" in loaded
    assert "_service_error('multiple_entries')" in loaded
    assert "entry.state is ConfigEntryState.LOADED" in loaded
    err = ast.unparse(_func(_SERVICES_TREE, "_service_error"))
    assert "ServiceValidationError(translation_domain=DOMAIN, translation_key=translation_key)" in err


def test_state_errors_cover_every_lifecycle_state():
    mapping = next(s for s in _SERVICES_TREE.body if isinstance(s, ast.AnnAssign)
                   and ast.unparse(s.target) == "_STATE_ERRORS")
    got = {ast.unparse(k).split(".")[-1]: v.value
           for k, v in zip(mapping.value.keys, mapping.value.values)}
    assert got == {
        "NOT_LOADED": "not_loaded",
        "SETUP_IN_PROGRESS": "reloading",
        "UNLOAD_IN_PROGRESS": "reloading",
        "SETUP_ERROR": "setup_failed",
        "SETUP_RETRY": "setup_failed",
        "MIGRATION_ERROR": "setup_failed",
        "FAILED_UNLOAD": "setup_failed",
    }


def test_speak_buffers_only_through_the_boot_window_then_resolves():
    body = [ast.unparse(s) for s in _body(_handlers()["speak"])]
    assert body[0] == "buffer = _boot_alert_buffer(hass)"
    assert body[1].startswith("if buffer is not None:")
    assert body[2] == f"entry, runtime = {RESOLVER}(hass)"
    assert body[3] == "await proactive_audio._dispatch_speak(hass, runtime, call.data)"
    window = ast.unparse(_func(_SERVICES_TREE, "_boot_alert_buffer"))
    assert "entry.state is not ConfigEntryState.SETUP_IN_PROGRESS" in window
    assert "runtime = lifecycle_runtime(entry)" in window
    assert "buffer = runtime.alert_buffer" in window
    assert "if buffer is None or buffer.ready" in window
    # It never builds a buffer (proactive_audio._alert_buffer would).
    calls = {ast.unparse(n.func) for n in ast.walk(_func(_SERVICES_TREE, "_boot_alert_buffer"))
             if isinstance(n, ast.Call)}
    assert not {c for c in calls if c.endswith("_alert_buffer") or c.endswith("AlertBuffer")}


def test_process_intent_needs_a_loaded_entry():
    src = ast.unparse(_handlers()["process_intent"])
    assert "_boot_alert_buffer" not in src and "SETUP_IN_PROGRESS" not in src


# ── 8. hass.data ────────────────────────────────────────────────────────────

def test_services_and_init_never_touch_hass_data():
    for path in (SERVICES, INIT, COMP / "proactive_audio.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                and n.attr == "data" and isinstance(n.value, ast.Name)
                and n.value.id == "hass"]
        assert hits == [], path.name
        src = path.read_text(encoding="utf-8")
        assert "HassKey" not in src, path.name


# ── 9 & 10. Safety paths ────────────────────────────────────────────────────

def test_routine_delegates_through_its_existing_safety_path():
    handler = _handlers()["routine"]
    calls = [ast.unparse(n.func) for n in ast.walk(handler) if isinstance(n, ast.Call)]
    assert "async_run_routine" in calls
    assert not [c for c in calls if c.endswith("async_call")]
    routines = (COMP / "routines.py").read_text(encoding="utf-8")
    for guard in ("policy.requires_confirmation(", "policy.confirm_gate(",
                  "action_log.start_many(", "action_log.set_execution("):
        assert guard in routines, guard


def test_lockdown_resolves_lifecycle_before_cognitive_core():
    stmts = _body(_handlers()["lockdown"])
    first_core = next(i for i, s in enumerate(stmts) if "cognitive_core" in ast.unparse(s))
    resolver = next(i for i, s in enumerate(stmts)
                    if ast.unparse(s) == f"entry, runtime = {RESOLVER}(hass)")
    assert resolver < first_core
    assert "cognitive_core.request_lockdown(" in ast.unparse(_handlers()["lockdown"])


def test_create_automation_keeps_its_safety_entry_point():
    src = ast.unparse(_handlers()["create_automation"])
    assert "from .automation_creator import create_automation" in src
    assert "source='ha_service'" in src
    assert "requested_by_user_id=" in src


def test_diagnose_doorbell_readiness_needs_a_loaded_entry():
    src = ast.unparse(_handlers()["diagnose_doorbell"])
    assert ("hass.services.has_service(DOMAIN, 'analyze_on_event') "
            "and entry.state is ConfigEntryState.LOADED") in src


# ── 11. Translations ────────────────────────────────────────────────────────

def _translation_files():
    return [COMP / "strings.json", *sorted((COMP / "translations").glob("*.json"))]


def test_service_errors_used_in_code_are_translated():
    used = {n.args[0].value for n in ast.walk(_SERVICES_TREE)
            if _is_call(n, "_service_error") and isinstance(n.args[0], ast.Constant)}
    mapping = next(s for s in _SERVICES_TREE.body if isinstance(s, ast.AnnAssign)
                   and ast.unparse(s.target) == "_STATE_ERRORS")
    used |= {v.value for v in mapping.value.values}
    used.add("not_loaded")       # the fallback for an unknown state
    assert used == SERVICE_ERROR_KEYS
    strings = json.loads((COMP / "strings.json").read_text(encoding="utf-8"))
    assert set(strings["exceptions"]) == SERVICE_ERROR_KEYS


@pytest.mark.parametrize("path", _translation_files(), ids=lambda p: p.name)
def test_exceptions_section_is_aligned(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    exceptions = data["exceptions"]
    assert set(exceptions) == SERVICE_ERROR_KEYS
    for key, value in exceptions.items():
        assert set(value) == {"message"}, (path.name, key)
        msg = value["message"]
        assert isinstance(msg, str) and msg.strip(), (path.name, key)
        assert "{" not in msg, (path.name, key)     # no placeholders to leak details


def test_translations_mirror_strings_json_top_level_sections():
    strings = json.loads((COMP / "strings.json").read_text(encoding="utf-8"))
    for path in _translation_files()[1:]:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert list(data) == list(strings), path.name


def test_non_english_service_errors_are_localized():
    en = json.loads((COMP / "translations" / "en.json").read_text(encoding="utf-8"))["exceptions"]
    for path in sorted((COMP / "translations").glob("*.json")):
        if path.name == "en.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))["exceptions"]
        for key in SERVICE_ERROR_KEYS:
            assert data[key]["message"] != en[key]["message"], (path.name, key)
