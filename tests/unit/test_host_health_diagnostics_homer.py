"""Host health integration into system_diagnostics, and HOMER's use of it
(Phase 10). HOMER already has the system_diagnostics tool from Phase 7 —
this proves the structured Host Health section rides along inside it
without HOMER needing (or gaining) any new tool, and that HOMER's tool
grant is completely unchanged by this phase.

Also covers the module-level safety properties the task calls out
explicitly: no filesystem/Supervisor access anywhere in host_health.py,
and that only fixed internal labels (never entity_id/friendly_name/raw
attributes) reach prompt_fields(), the one function whose output is meant
to be folded into an LLM prompt.
"""
import importlib.util
import pathlib
import sys
import types

import pytest

from fakes import FakeHass, FakeEntityRegistry, FakeRegistryEntry

_COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
_HOST_HEALTH_PY = _COMP / "host_health.py"


@pytest.fixture
def hh(load):
    return load("host_health")


@pytest.fixture
def sh(monkeypatch):
    """Same loading convention as test_service_health.py's own `sh` fixture
    (a stub jc.nova_config so `from .. import nova_config` resolves without
    pulling the whole tree), extended with get_all() since _check_host_health
    needs it. Reverts via monkeypatch, never a raw sys.modules assignment."""
    if "jc" not in sys.modules:
        pkg = types.ModuleType("jc")
        pkg.__path__ = [str(_COMP)]
        sys.modules["jc"] = pkg
    cfg_store: dict = {}
    jc_cfg = types.ModuleType("jc.nova_config")
    jc_cfg.get = lambda k, d=None: cfg_store.get(k, d)
    jc_cfg.get_all = lambda: dict(cfg_store)
    monkeypatch.setitem(sys.modules, "jc.nova_config", jc_cfg)
    if "jc.diagnostics" not in sys.modules:
        dpkg = types.ModuleType("jc.diagnostics")
        dpkg.__path__ = [str(_COMP / "diagnostics")]
        monkeypatch.setitem(sys.modules, "jc.diagnostics", dpkg)
    key = "jc.diagnostics.service_health"
    if key in sys.modules:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        key, _COMP / "diagnostics" / "service_health.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    mod._cfg_store = cfg_store
    return mod


@pytest.fixture(autouse=True)
def _reset_state(hh):
    hh.reset_state()
    yield
    hh.reset_state()


# ── system_diagnostics integration ──────────────────────────────────────────

async def test_run_service_health_includes_host_health_check(sh, monkeypatch):
    import sys, types
    hh_mod = types.ModuleType("jc.host_health")
    hh_mod.snapshot = lambda hass, config: {
        "enabled": True, "overall": "ok", "available": [{"key": "cpu_percent"}],
        "problems": [], "persistent_problems": [], "missing_or_stale": [],
        "newest_sample_ts": None, "alerts_enabled": False,
    }
    monkeypatch.setitem(sys.modules, "jc.host_health", hh_mod)
    cfg_mod = types.ModuleType("jc.nova_config")
    cfg_mod.get_all = lambda: {}
    monkeypatch.setitem(sys.modules, "jc.nova_config", cfg_mod)

    res = await sh.run_service_health(FakeHass())
    keys = {s["key"] for s in res["services"]}
    assert "host_health" in keys
    hh_entry = next(s for s in res["services"] if s["key"] == "host_health")
    assert hh_entry["status"] == "ok"
    assert hh_entry["snapshot"]["overall"] == "ok"


async def test_host_health_off_by_default_shows_off_status(sh, monkeypatch):
    import sys, types
    hh_mod = types.ModuleType("jc.host_health")
    hh_mod.snapshot = lambda hass, config: {"enabled": False}
    monkeypatch.setitem(sys.modules, "jc.host_health", hh_mod)
    cfg_mod = types.ModuleType("jc.nova_config")
    cfg_mod.get_all = lambda: {}
    monkeypatch.setitem(sys.modules, "jc.nova_config", cfg_mod)

    res = await sh.run_service_health(FakeHass())
    hh_entry = next(s for s in res["services"] if s["key"] == "host_health")
    assert hh_entry["status"] == "off"


async def test_host_health_persistent_problem_surfaces_as_warn_not_down(sh, monkeypatch):
    """Host load is not a household critical-safety alert — the worst this
    check may show in the core-services roll-up is WARN, never DOWN."""
    import sys, types
    hh_mod = types.ModuleType("jc.host_health")
    hh_mod.snapshot = lambda hass, config: {
        "enabled": True, "overall": "problem",
        "persistent_problems": [{"label": "Processor use"}],
        "available": [], "problems": [], "missing_or_stale": [],
        "newest_sample_ts": None, "alerts_enabled": True,
    }
    monkeypatch.setitem(sys.modules, "jc.host_health", hh_mod)
    cfg_mod = types.ModuleType("jc.nova_config")
    cfg_mod.get_all = lambda: {}
    monkeypatch.setitem(sys.modules, "jc.nova_config", cfg_mod)

    res = await sh.run_service_health(FakeHass())
    hh_entry = next(s for s in res["services"] if s["key"] == "host_health")
    assert hh_entry["status"] == "warn"
    assert res["overall"] in ("warn", "down")  # never contributes DOWN itself
    # confirm host_health specifically never emits "down"
    assert hh_entry["status"] != "down"


async def test_host_health_check_error_never_raises_or_shows_down(sh, monkeypatch):
    import sys, types
    hh_mod = types.ModuleType("jc.host_health")
    def _boom(hass, config):
        raise RuntimeError("boom")
    hh_mod.snapshot = _boom
    monkeypatch.setitem(sys.modules, "jc.host_health", hh_mod)
    cfg_mod = types.ModuleType("jc.nova_config")
    cfg_mod.get_all = lambda: {}
    monkeypatch.setitem(sys.modules, "jc.nova_config", cfg_mod)

    res = await sh.run_service_health(FakeHass())  # must not raise
    hh_entry = next(s for s in res["services"] if s["key"] == "host_health")
    assert hh_entry["status"] == "idle"  # a broken CHECK is idle, never down


# ── HOMER uses system_diagnostics; tool grant unchanged ─────────────────────

@pytest.fixture
def agent(load):
    return load("agent")


def test_homer_tool_grant_still_exactly_eight_tools(agent):
    """This phase must not add a HOMER tool or touch its grant at all."""
    tools, max_turns, label, directive = agent._resolve_profile("homer")
    assert tools == {
        "system_diagnostics", "cognitive_status", "connectivity_status",
        "energy_status", "activity_history", "get_entity_state",
        "search_entities", "root_cause",
    }
    assert max_turns == 4
    assert label == "HOMER"


def test_no_host_health_tool_exists(agent):
    names = {t["function"]["name"] for t in agent.NOVA_TOOLS}
    assert "host_health" not in names
    assert not any("host_health" in n for n in names)


async def test_homer_can_reach_host_health_via_system_diagnostics_tool(agent, monkeypatch):
    """HOMER's ONLY path to host-health data is the same system_diagnostics
    tool it already had — no bypass, no new tool."""
    async def fake_system_diagnostics(hass, args):
        import json
        return json.dumps({
            "overall": "warn",
            "services": [{"name": "Host health", "key": "host_health", "status": "warn",
                          "detail": "persistent problem: Processor use",
                          "snapshot": {"overall": "problem"}}],
        })
    # _TOOL_MAP binds function objects at module-load time, not by name — a
    # patch on the standalone function attribute alone wouldn't reach it.
    monkeypatch.setitem(agent._TOOL_MAP, "system_diagnostics", fake_system_diagnostics)
    tools, _, _, _ = agent._resolve_profile("homer")
    assert "system_diagnostics" in tools
    result = await agent._execute_tool(FakeHass(), "system_diagnostics", {}, None, None)
    assert "host_health" in result
    assert "persistent problem" in result


def test_homer_still_cannot_control_or_modify_host(agent):
    """Regression, explicit for this phase: HOMER's grant has no actuator,
    no config tool, and (structurally) nothing that could restart/shutdown
    the host — this phase adds no such tool anywhere in NOVA_TOOLS either."""
    tools, _, _, _ = agent._resolve_profile("homer")
    assert not (tools & agent._SUBAGENT_DENY)
    all_tool_names = {t["function"]["name"] for t in agent.NOVA_TOOLS}
    for forbidden in ("restart_host", "shutdown_host", "reboot", "supervisor"):
        assert not any(forbidden in n for n in all_tool_names)


# ── source guards: no filesystem/Supervisor access, no prompt leakage ──────

def test_host_health_module_never_touches_proc_or_sys():
    """The module docstring legitimately SAYS "never reads /proc, /sys" as
    documentation of the boundary — this checks for actual dangerous call
    patterns, not that documentation sentence itself."""
    src = _HOST_HEALTH_PY.read_text()
    for forbidden in ("open(", "subprocess", "os.popen", "os.system(",
                      "paramiko", "asyncssh", "Supervisor(", "supervisor_api",
                      'Path("/proc', "Path('/proc", 'Path("/sys', "Path('/sys"):
        assert forbidden not in src, f"host_health.py must not reference {forbidden!r}"


def test_host_health_module_never_calls_a_ha_service():
    """No control_device-equivalent call anywhere — host_health only reads
    hass.states and the entity registry, and dispatches announcements
    through the existing helpers (which themselves own any service calls)."""
    src = _HOST_HEALTH_PY.read_text()
    assert "hass.services.async_call" not in src
    assert "async_update_entity" not in src  # never enables a disabled entity


async def test_prompt_fields_only_fixed_internal_labels(hh, monkeypatch):
    import sys, types
    reg = FakeEntityRegistry()
    reg.add(FakeRegistryEntry("sensor.cpu_with_secret_name_do_not_leak", "systemmonitor",
                              unique_id="processor_use_", translation_key="processor_use"))
    import homeassistant.helpers.entity_registry as er_mod
    monkeypatch.setattr(er_mod, "async_get", lambda hass: reg)
    hass = FakeHass()
    hass.states.set("sensor.cpu_with_secret_name_do_not_leak", 95.0,
                    unit_of_measurement="%", state_class="measurement",
                    friendly_name="A Friendly Name That Must Never Leak Into A Prompt")
    config = {"host_health_enabled": True}
    await hh.tick(hass, config)  # populate _STATE — prompt_fields reads the last tick, not a fresh sample
    fields = hh.prompt_fields(hass, config)
    blob = repr(fields)
    assert "secret_name_do_not_leak" not in blob
    assert "Friendly Name That Must Never Leak" not in blob
    # Only the fixed metric key and numeric fields are present.
    assert any(f["metric"] == "cpu_percent" for f in fields)
    for f in fields:
        assert set(f.keys()) == {"metric", "value", "unit", "over_threshold", "persistent"}
