"""Nova's public compatibility contracts, pinned against reviewed fixtures.

These tests exist to DETECT public contract changes: service names and
fields, nova/* WebSocket commands, configuration keys, agent tool schemas
and grants, persisted store identities, and the panel/backend boundary.
They are characterization tests of current behaviour, not a design.

An intentional contract change must update, in the same change:
  1. the implementation,
  2. migration or compatibility handling for existing installs, and
  3. the matching fixture in tests/fixtures/contracts/.
Regenerate a fixture only after reviewing the diff it produces:
    NOVA_WRITE_CONTRACTS=1 python -m pytest tests/unit/test_public_contracts.py
A failure here names the service, command, key or tool that changed.

Focused run:
    python -m pytest tests/unit/test_public_contracts.py -q
"""
import ast
import importlib.util
import os

import pytest

import contract_extract as ce

_WRITE = os.environ.get("NOVA_WRITE_CONTRACTS") == "1"


def _check(name: str, current) -> dict:
    """Compare `current` with fixture `name`, or (re)write it when asked."""
    current = ce.canonical(current)
    path = ce.FIXTURES / f"{name}.json"
    if _WRITE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ce.dump(current), encoding="utf-8")
    return ce.canonical(ce.load_fixture(name)), current


def _diff_keys(pinned: dict, current: dict, what: str):
    missing = sorted(set(pinned) - set(current))
    added = sorted(set(current) - set(pinned))
    assert not missing and not added, (
        f"{what} inventory changed — removed: {missing}, added: {added}")
    for key in sorted(pinned):
        assert pinned[key] == current[key], (
            f"{what} {key!r} changed:\n  pinned:  {pinned[key]}\n  current: {current[key]}")


# ── Services ────────────────────────────────────────────────────────────────

def _lifecycle_helpers():
    """Reuse the service inventory the Phase 0B lifecycle tests own."""
    spec = importlib.util.spec_from_file_location(
        "_lifecycle_cleanup_for_contracts",
        os.path.join(os.path.dirname(__file__), "test_lifecycle_cleanup.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_services_documented_contract():
    pinned, current = _check("services", {
        "documented": ce.services_contract(),
        "registered_schemas": ce.service_schema_keys(),
    })
    _diff_keys(pinned["documented"], current["documented"], "service (services.yaml)")
    _diff_keys(pinned["registered_schemas"], current["registered_schemas"],
               "service schema (services.py)")


def test_service_inventories_agree():
    lc = _lifecycle_helpers()
    registered = set(lc._registered_services())
    assert registered == set(ce.registered_services())
    assert registered == set(ce.services_contract()), "services.yaml out of step with registrations"
    # Phase 4: services live for the process lifetime; nothing removes one.
    assert lc._unloaded_services() == set()


@pytest.mark.parametrize("service", sorted(
    s for s, keys in ce.service_schema_keys().items() if keys is not None))
def test_documented_fields_match_registered_schema(service):
    """Every field services.yaml documents is accepted by the registered
    schema, and every schema field is documented (analyze_camera's clip
    fields and shush's `all` were the gaps this used to record)."""
    documented = {f: ("required" if v["required"] else "optional")
                  for f, v in ce.services_contract()[service]["fields"].items()}
    assert documented == ce.service_schema_keys()[service], service


# ── WebSocket ───────────────────────────────────────────────────────────────

def test_websocket_contract():
    pinned, current = _check("websocket", ce.websocket_contract())
    _diff_keys(pinned, current, "WebSocket command")


def test_every_websocket_command_is_registered_and_namespaced():
    ws = ce.websocket_contract()
    assert ws and all(t.startswith("nova/") for t in ws)
    assert [t for t, c in ws.items() if not c["registered"]] == []


# ── Configuration ───────────────────────────────────────────────────────────

def test_config_contract():
    pinned, current = _check("config", ce.config_contract())
    for key in ("domain", "manifest", "config_entry_schema_version",
                "config_entry_unique_id_calls"):
        assert pinned[key] == current[key], f"config {key} changed: {pinned[key]} -> {current[key]}"
    _diff_keys(pinned["conf_constants"], current["conf_constants"], "CONF_ constant")
    removed = sorted(set(pinned["panel_writable_keys"]) - set(current["panel_writable_keys"]))
    added = sorted(set(current["panel_writable_keys"]) - set(pinned["panel_writable_keys"]))
    assert not removed and not added, f"PANEL_WRITABLE_KEYS changed — removed: {removed}, added: {added}"


def test_config_json_is_a_flat_object_under_the_config_dir(load, tmp_path, monkeypatch):
    """config.json lives at <config dir>/nova/config.json and is one flat
    JSON object; non-object content is rejected, not trusted."""
    import json
    import types
    nc = load("nova_config")
    monkeypatch.setattr(nc, "CONFIG_PATH", nc.CONFIG_PATH)
    hass = types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *p: str(tmp_path.joinpath(*p))))
    nc.configure(hass)
    try:
        assert nc.CONFIG_PATH == tmp_path / "nova" / "config.json"
        nc.set("banter_level", 2)
        assert json.loads(nc.CONFIG_PATH.read_text()) == {"banter_level": 2}
        nc.CONFIG_PATH.write_text("[1, 2]")
        nc._cache, nc._loaded = {}, False
        assert nc.get("banter_level") is None
        assert nc.last_load_error
    finally:
        nc._cache, nc._loaded = {}, False
        nc.last_load_error = None


# ── Agent tools ─────────────────────────────────────────────────────────────

@pytest.fixture
def agent(load):
    return load("agent")


def test_agent_tool_contract(agent):
    pinned, current = _check("agent_tools", ce.agent_contract(agent))
    _diff_keys(pinned["tools"], current["tools"], "agent tool")
    for key in sorted(k for k in pinned if k != "tools"):
        assert pinned[key] == current[key], (
            f"agent {key} changed:\n  pinned:  {pinned[key]}\n  current: {current[key]}")


def test_every_offered_tool_is_dispatchable(agent):
    offered = {t["function"]["name"] for t in agent.NOVA_TOOLS}
    # delegate_task is handled by the agent loop itself, not _TOOL_MAP.
    assert offered - set(agent._TOOL_MAP) == {"delegate_task"}
    assert set(agent._TOOL_MAP) <= offered


def test_homer_grant_and_alias(agent):
    tools, max_turns, _, directive = agent._resolve_profile("homer")
    assert tools == set(ce.load_fixture("agent_tools")["profiles"]["homer"]["tools"])
    assert max_turns == 4 and directive
    assert agent._resolve_capability("diagnostics") == tools
    assert not (tools & agent._SUBAGENT_DENY)


# ── Stable cross-module imports ─────────────────────────────────────────────

_WEBSOCKET_EXPORTS = ("nova_log", "recent_debug_log", "recent_conversation_log",
                      "PANEL_WRITABLE_KEYS", "async_register", "invalidate_model_cache")


def test_websocket_exports_are_defined_at_module_level():
    """websocket.py can't be imported under the unit fakes; the PHACC twin
    of this test imports them for real."""
    tree = ast.parse((ce.COMP / "websocket.py").read_text(encoding="utf-8"))
    defined = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    assert set(_WEBSOCKET_EXPORTS) <= defined


def test_agent_exports_resolve(agent):
    assert callable(agent.run_agent) and callable(agent._verify_control)


# ── Storage identity ────────────────────────────────────────────────────────

def test_storage_identity_contract():
    pinned, current = _check("storage", ce.storage_contract())
    _diff_keys(pinned, current, "storage owner module")


# ── Panel / backend boundary ────────────────────────────────────────────────

def test_panel_references_exist_in_backend():
    ws = ce.websocket_contract()
    assert set(ce.panel_ws_commands()) <= set(ws), "panel calls an unknown nova/* command"
    assert set(ce.panel_service_calls()) <= set(ce.registered_services()), (
        "panel calls an unregistered nova service")


def _panel_data_reads() -> tuple[set, set]:
    """Top-level get_panel_data keys the panel's _data() mapping reads, and
    the config.* fallbacks it accepts for them."""
    import re
    src = (ce.COMP / "frontend" / "nova-panel.js").read_text(encoding="utf-8")
    start = src.index("  _data() {\n    const live = this._liveData;")
    body = src[start:src.index("\n  }\n", start)]
    return (set(re.findall(r"live\.([a-z_]+)", body)) - {"config"} | {"config"},
            set(re.findall(r"live\.config\?\.([a-z_]+)", body)))


def test_panel_data_sections_are_sent_by_backend():
    backend = set(ce.panel_data_backend_keys())
    reads, fallbacks = _panel_data_reads()
    pinned, _ = _check("panel_boundary", {
        "panel_reads": sorted(reads), "config_fallbacks": sorted(fallbacks),
        "backend_top_level": sorted(backend),
    })
    assert sorted(reads) == pinned["panel_reads"]
    assert sorted(backend) == pinned["backend_top_level"]
    unmet = sorted(k for k in reads if k not in backend and k not in fallbacks)
    assert unmet == [], f"panel reads get_panel_data sections the backend never sends: {unmet}"


def test_panel_label_suggestions_reach_the_panel():
    """The backend sends labels in config.available_labels; the panel's
    _data() mapping must expose them to the exclusions datalist (the panel
    smoke test checks the rendered, escaped options)."""
    src = (ce.COMP / "frontend" / "nova-panel.js").read_text(encoding="utf-8")
    start = src.index("  _data() {\n    const live = this._liveData;")
    body = src[start:src.index("\n  }\n", start)]
    assert "available_labels: live.config?.available_labels || []" in body
