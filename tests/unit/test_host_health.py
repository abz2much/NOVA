"""host_health.py — discovery, mapping, reading, and the deterministic
persistence/cooldown evaluator (Phase 10).

Uses FakeHass + FakeEntityRegistry (tests/fakes.py) exclusively — no test
here depends on the development machine's own sensors, matching the task's
explicit requirement.
"""
import math

import pytest

from fakes import FakeHass, FakeEntityRegistry, FakeRegistryEntry


@pytest.fixture
def hh(load):
    return load("host_health")


@pytest.fixture(autouse=True)
def _reset_state(hh):
    hh.reset_state()
    yield
    hh.reset_state()


@pytest.fixture
def registry(monkeypatch):
    reg = FakeEntityRegistry()
    import homeassistant.helpers.entity_registry as er_mod
    monkeypatch.setattr(er_mod, "async_get", lambda hass: reg)
    return reg


def _sysmon_entry(entity_id, sysmon_key, **kw):
    return FakeRegistryEntry(entity_id, "systemmonitor", unique_id=f"{sysmon_key}_",
                             translation_key=sysmon_key, **kw)


# ── discovery: integration ownership + metadata, not text matching ─────────

def test_discover_by_translation_key_ignores_friendly_name(hh, registry):
    """A renamed, non-English friendly name must not affect discovery — only
    platform ownership + translation_key matter."""
    registry.add(_sysmon_entry("sensor.n1", "processor_use"))
    hass = FakeHass()
    hass.states.set("sensor.n1", 42.0, unit_of_measurement="%",
                    state_class="measurement", friendly_name="Utilisation du processeur (Français)")
    cands = hh.discover_candidates(hass)
    assert len(cands["cpu_percent"]) == 1
    assert cands["cpu_percent"][0].entity_id == "sensor.n1"


def test_discover_ignores_non_systemmonitor_entities(hh, registry):
    registry.add(FakeRegistryEntry("sensor.other", "other_integration",
                                   translation_key="processor_use"))
    hass = FakeHass()
    hass.states.set("sensor.other", 42.0, unit_of_measurement="%", state_class="measurement")
    cands = hh.discover_candidates(hass)
    assert cands["cpu_percent"] == []


def test_discover_falls_back_to_unique_id_prefix_without_translation_key(hh, registry):
    """An older/migrated entity might lack translation_key entirely — the
    unique_id prefix (the integration's own internal key, not user text) is
    the documented fallback."""
    registry.add(FakeRegistryEntry("sensor.legacy_cpu", "systemmonitor",
                                   unique_id="processor_use_", translation_key=None))
    hass = FakeHass()
    hass.states.set("sensor.legacy_cpu", 10.0, unit_of_measurement="%", state_class="measurement")
    cands = hh.discover_candidates(hass)
    assert len(cands["cpu_percent"]) == 1


def test_discover_excludes_accumulated_psi_total_entities(hh, registry):
    """The _total PSI variants must never be treated as candidates for
    memory_pressure_some, even though they share the metric's key prefix —
    excluded by state_class (total_increasing), not by name matching."""
    registry.add(_sysmon_entry("sensor.mem_pressure_avg", "memory_pressure_some_avg60"))
    registry.add(FakeRegistryEntry("sensor.mem_pressure_total", "systemmonitor",
                                   unique_id="memory_pressure_some_total_",
                                   translation_key="memory_pressure_some_total"))
    hass = FakeHass()
    hass.states.set("sensor.mem_pressure_avg", 3.0, unit_of_measurement="%", state_class="measurement")
    hass.states.set("sensor.mem_pressure_total", 123456, unit_of_measurement="us", state_class="total_increasing")
    cands = hh.discover_candidates(hass)
    assert [c.entity_id for c in cands["memory_pressure_some"]] == ["sensor.mem_pressure_avg"]


def test_discover_shows_disabled_entity_as_a_candidate_flagged_disabled(hh, registry):
    registry.add(_sysmon_entry("sensor.cpu_temp", "processor_temperature", disabled_by="integration"))
    hass = FakeHass()  # disabled entity has no live state
    cands = hh.discover_candidates(hass)
    assert len(cands["cpu_temperature"]) == 1
    assert cands["cpu_temperature"][0].disabled is True


def test_discover_multiple_disk_mount_candidates(hh, registry):
    registry.add(_sysmon_entry("sensor.disk_root", "disk_use_percent"))
    registry.add(_sysmon_entry("sensor.disk_data", "disk_use_percent"))
    hass = FakeHass()
    hass.states.set("sensor.disk_root", 40.0, unit_of_measurement="%", state_class="measurement")
    hass.states.set("sensor.disk_data", 55.0, unit_of_measurement="%", state_class="measurement")
    cands = hh.discover_candidates(hass)
    assert len(cands["disk_percent"]) == 2


def test_discover_registry_failure_yields_empty_not_a_crash(hh, monkeypatch):
    import homeassistant.helpers.entity_registry as er_mod

    def _boom(hass):
        raise RuntimeError("registry unavailable")
    monkeypatch.setattr(er_mod, "async_get", _boom)
    cands = hh.discover_candidates(FakeHass())
    assert all(v == [] for v in cands.values())


# ── mapping resolution ──────────────────────────────────────────────────────

def test_single_candidate_auto_maps(hh, registry):
    registry.add(_sysmon_entry("sensor.cpu", "processor_use"))
    hass = FakeHass()
    hass.states.set("sensor.cpu", 20.0, unit_of_measurement="%", state_class="measurement")
    mappings = hh.resolve_mappings(hass, {})
    m = mappings["cpu_percent"]
    assert m.status == "mapped" and m.source == "auto" and m.entity_id == "sensor.cpu"


def test_multiple_candidates_require_manual_selection(hh, registry):
    registry.add(_sysmon_entry("sensor.disk_a", "disk_use_percent"))
    registry.add(_sysmon_entry("sensor.disk_b", "disk_use_percent"))
    hass = FakeHass()
    hass.states.set("sensor.disk_a", 10.0, unit_of_measurement="%", state_class="measurement")
    hass.states.set("sensor.disk_b", 20.0, unit_of_measurement="%", state_class="measurement")
    mappings = hh.resolve_mappings(hass, {})
    assert mappings["disk_percent"].status == "ambiguous"
    assert mappings["disk_percent"].entity_id is None


def test_manual_mapping_resolves_an_ambiguous_metric(hh, registry):
    registry.add(_sysmon_entry("sensor.disk_a", "disk_use_percent"))
    registry.add(_sysmon_entry("sensor.disk_b", "disk_use_percent"))
    hass = FakeHass()
    hass.states.set("sensor.disk_a", 10.0, unit_of_measurement="%", state_class="measurement")
    hass.states.set("sensor.disk_b", 20.0, unit_of_measurement="%", state_class="measurement")
    config = {"host_health_mappings": {"disk_percent": "sensor.disk_b"}}
    mappings = hh.resolve_mappings(hass, config)
    assert mappings["disk_percent"].status == "mapped"
    assert mappings["disk_percent"].entity_id == "sensor.disk_b"
    assert mappings["disk_percent"].source == "manual"


def test_disabled_sole_candidate_reported_as_disabled_not_auto_enabled(hh, registry):
    registry.add(_sysmon_entry("sensor.cpu_temp", "processor_temperature", disabled_by="integration"))
    hass = FakeHass()
    mappings = hh.resolve_mappings(hass, {})
    assert mappings["cpu_temperature"].status == "disabled"
    assert mappings["cpu_temperature"].entity_id is None  # never force-enabled


def test_missing_optional_metric_does_not_block_others(hh, registry):
    registry.add(_sysmon_entry("sensor.cpu", "processor_use"))
    hass = FakeHass()
    hass.states.set("sensor.cpu", 5.0, unit_of_measurement="%", state_class="measurement")
    mappings = hh.resolve_mappings(hass, {})
    assert mappings["cpu_percent"].status == "mapped"
    assert mappings["cpu_temperature"].status == "missing"


def test_manual_mapping_rejects_wrong_domain(hh, registry):
    hass = FakeHass()
    hass.states.set("light.not_a_sensor", "on")
    config = {"host_health_mappings": {"cpu_percent": "light.not_a_sensor"}}
    mappings = hh.resolve_mappings(hass, config)
    assert mappings["cpu_percent"].status != "mapped"


def test_manual_mapping_rejects_wrong_unit(hh, registry):
    hass = FakeHass()
    hass.states.set("sensor.not_percent", 5.0, unit_of_measurement="kWh", state_class="measurement")
    config = {"host_health_mappings": {"cpu_percent": "sensor.not_percent"}}
    mappings = hh.resolve_mappings(hass, config)
    assert mappings["cpu_percent"].status != "mapped"


def test_manual_mapping_to_nonexistent_entity_falls_back(hh, registry):
    hass = FakeHass()
    config = {"host_health_mappings": {"cpu_percent": "sensor.does_not_exist"}}
    mappings = hh.resolve_mappings(hass, config)
    assert mappings["cpu_percent"].status == "missing"  # no candidates either


def test_mapping_removed_or_entity_disappears_does_not_crash(hh, registry):
    registry.add(_sysmon_entry("sensor.cpu", "processor_use"))
    hass = FakeHass()
    hass.states.set("sensor.cpu", 5.0, unit_of_measurement="%", state_class="measurement")
    mappings = hh.resolve_mappings(hass, {})
    assert mappings["cpu_percent"].status == "mapped"
    # Entity vanishes (removed from registry AND states)
    registry.entities.clear()
    hass.states.remove("sensor.cpu")
    mappings2 = hh.resolve_mappings(hass, {})
    assert mappings2["cpu_percent"].status == "missing"


# ── reading + normalization ─────────────────────────────────────────────────

def test_read_metric_unavailable_state(hh, registry):
    registry.add(_sysmon_entry("sensor.cpu", "processor_use"))
    hass = FakeHass()
    hass.states.set("sensor.cpu", "unavailable")
    mapping = hh.resolve_mappings(hass, {})["cpu_percent"]
    sample = hh.read_metric(hass, mapping, 600.0)
    assert sample.available is False and sample.reason == "unavailable"


def test_read_metric_unknown_state(hh, registry):
    registry.add(_sysmon_entry("sensor.cpu", "processor_use"))
    hass = FakeHass()
    hass.states.set("sensor.cpu", "unknown")
    mapping = hh.resolve_mappings(hass, {})["cpu_percent"]
    sample = hh.read_metric(hass, mapping, 600.0)
    assert sample.available is False


def test_read_metric_non_numeric_state(hh, registry):
    registry.add(_sysmon_entry("sensor.cpu", "processor_use"))
    hass = FakeHass()
    hass.states.set("sensor.cpu", "not-a-number")
    mapping = hh.resolve_mappings(hass, {})["cpu_percent"]
    sample = hh.read_metric(hass, mapping, 600.0)
    assert sample.available is False and sample.reason == "non_numeric"


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan"])
def test_read_metric_non_finite_state(hh, registry, bad):
    registry.add(_sysmon_entry("sensor.cpu", "processor_use"))
    hass = FakeHass()
    hass.states.set("sensor.cpu", bad)
    mapping = hh.resolve_mappings(hass, {})["cpu_percent"]
    sample = hh.read_metric(hass, mapping, 600.0)
    assert sample.available is False


def test_read_metric_missing_mapping(hh):
    mapping = hh.MappingResult(hh._METRICS_BY_KEY["cpu_percent"], None, None, "missing", [])
    sample = hh.read_metric(FakeHass(), mapping, 600.0)
    assert sample.available is False and sample.reason == "missing"


def test_read_metric_celsius_passthrough(hh, registry):
    registry.add(_sysmon_entry("sensor.temp", "processor_temperature"))
    hass = FakeHass()
    hass.states.set("sensor.temp", 55.0, unit_of_measurement="°C", state_class="measurement")
    mapping = hh.resolve_mappings(hass, {})["cpu_temperature"]
    sample = hh.read_metric(hass, mapping, 600.0)
    assert sample.available and sample.value == 55.0 and sample.unit == "°C"


def test_read_metric_fahrenheit_converted_to_celsius(hh, registry):
    registry.add(_sysmon_entry("sensor.temp", "processor_temperature"))
    hass = FakeHass()
    hass.states.set("sensor.temp", 212.0, unit_of_measurement="°F", state_class="measurement")
    mapping = hh.resolve_mappings(hass, {})["cpu_temperature"]
    sample = hh.read_metric(hass, mapping, 600.0)
    assert sample.available and sample.unit == "°C"
    assert math.isclose(sample.value, 100.0, abs_tol=0.01)


def test_read_metric_stale_reading_rejected(hh, registry):
    import datetime
    registry.add(_sysmon_entry("sensor.cpu", "processor_use"))
    hass = FakeHass()
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    hass.states.set("sensor.cpu", 50.0, unit_of_measurement="%", state_class="measurement",
                    last_updated=old)
    mapping = hh.resolve_mappings(hass, {})["cpu_percent"]
    sample = hh.read_metric(hass, mapping, max_age_seconds=600.0)
    assert sample.available is False and sample.reason == "stale"


def test_read_metric_fresh_reading_accepted(hh, registry):
    registry.add(_sysmon_entry("sensor.cpu", "processor_use"))
    hass = FakeHass()
    hass.states.set("sensor.cpu", 50.0, unit_of_measurement="%", state_class="measurement")
    mapping = hh.resolve_mappings(hass, {})["cpu_percent"]
    sample = hh.read_metric(hass, mapping, max_age_seconds=600.0)
    assert sample.available is True


def test_read_pressure_metric(hh, registry):
    registry.add(_sysmon_entry("sensor.mem_p_some", "memory_pressure_some_avg60"))
    registry.add(_sysmon_entry("sensor.mem_p_full", "memory_pressure_full_avg60"))
    registry.add(_sysmon_entry("sensor.io_p_some", "io_pressure_some_avg60"))
    registry.add(_sysmon_entry("sensor.io_p_full", "io_pressure_full_avg60"))
    hass = FakeHass()
    hass.states.set("sensor.mem_p_some", 12.0, unit_of_measurement="%", state_class="measurement")
    hass.states.set("sensor.mem_p_full", 4.0, unit_of_measurement="%", state_class="measurement")
    hass.states.set("sensor.io_p_some", 8.0, unit_of_measurement="%", state_class="measurement")
    hass.states.set("sensor.io_p_full", 1.0, unit_of_measurement="%", state_class="measurement")
    mappings = hh.resolve_mappings(hass, {})
    for key, expect in (("memory_pressure_some", 12.0), ("memory_pressure_full", 4.0),
                        ("io_pressure_some", 8.0), ("io_pressure_full", 1.0)):
        sample = hh.read_metric(hass, mappings[key], 600.0)
        assert sample.available and sample.value == expect


# ── configuration clamps ────────────────────────────────────────────────────

def test_clamp_threshold_bounds(hh):
    assert hh.clamp_threshold("cpu_percent", 500) == 100.0
    assert hh.clamp_threshold("cpu_percent", -5) == 1.0
    assert hh.clamp_threshold("cpu_percent", 75) == 75.0
    assert hh.clamp_threshold("cpu_percent", None) == hh.DEFAULT_THRESHOLDS["cpu_percent"]


def test_clamp_persistence_and_cooldown_bounds(hh):
    assert hh.clamp_persistence_minutes(999) == hh._PERSISTENCE_RANGE_MINUTES[1]
    assert hh.clamp_persistence_minutes(0) == hh._PERSISTENCE_RANGE_MINUTES[0]
    assert hh.clamp_cooldown_minutes(9999) == hh._COOLDOWN_RANGE_MINUTES[1]
    assert hh.clamp_cooldown_minutes(-1) == hh._COOLDOWN_RANGE_MINUTES[0]


# ── evaluator: persistence, brief spikes, cooldown, recovery ───────────────

@pytest.fixture
def cpu_setup(hh, registry):
    registry.add(_sysmon_entry("sensor.cpu", "processor_use"))
    hass = FakeHass()
    hass.states.set("sensor.cpu", 10.0, unit_of_measurement="%", state_class="measurement")
    return hass


def _fake_clock(hh, monkeypatch, start=0.0):
    box = {"t": start}
    monkeypatch.setattr(hh, "_now", lambda: box["t"])
    return box


async def _tick_silent(hh, monkeypatch, hass, config):
    """tick() with alert dispatch stubbed to a no-op recorder."""
    calls = []
    async def _fake_dispatch(hass_, config_, message, category):
        calls.append((category, message))
    monkeypatch.setattr(hh, "_dispatch_alert", _fake_dispatch)
    res = await hh.tick(hass, config)
    return res, calls


async def test_brief_spike_does_not_alert(hh, monkeypatch, cpu_setup):
    hass = cpu_setup
    clock = _fake_clock(hh, monkeypatch)
    config = {"host_health_enabled": True, "host_health_alerts_enabled": True,
             "host_health_persistence_minutes": 10}
    hass.states.set("sensor.cpu", 95.0, unit_of_measurement="%", state_class="measurement")
    calls_total = []
    for _ in range(2):
        clock["t"] += 120
        _, calls = await _tick_silent(hh, monkeypatch, hass, config)
        calls_total += calls
    hass.states.set("sensor.cpu", 10.0, unit_of_measurement="%", state_class="measurement")
    clock["t"] += 120
    _, calls = await _tick_silent(hh, monkeypatch, hass, config)
    calls_total += calls
    assert calls_total == []


async def test_sustained_breach_alerts_once_persistence_elapses(hh, monkeypatch, cpu_setup):
    hass = cpu_setup
    clock = _fake_clock(hh, monkeypatch)
    config = {"host_health_enabled": True, "host_health_alerts_enabled": True,
             "host_health_persistence_minutes": 10}
    hass.states.set("sensor.cpu", 95.0, unit_of_measurement="%", state_class="measurement")
    fired = []
    for _ in range(6):
        clock["t"] += 120
        _, calls = await _tick_silent(hh, monkeypatch, hass, config)
        fired += calls
    problem_calls = [c for c in fired if c[0] == "host_health_problem"]
    assert len(problem_calls) == 1


async def test_stale_samples_never_satisfy_persistence(hh, monkeypatch, registry):
    import datetime
    registry.add(_sysmon_entry("sensor.cpu", "processor_use"))
    hass = FakeHass()
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=5)
    hass.states.set("sensor.cpu", 95.0, unit_of_measurement="%", state_class="measurement",
                    last_updated=old)
    clock = _fake_clock(hh, monkeypatch)
    config = {"host_health_enabled": True, "host_health_alerts_enabled": True,
             "host_health_persistence_minutes": 10}
    fired = []
    for _ in range(10):
        clock["t"] += 120
        _, calls = await _tick_silent(hh, monkeypatch, hass, config)
        fired += calls
    assert fired == []


async def test_cooldown_suppresses_immediate_repeat_alert(hh, monkeypatch, cpu_setup):
    hass = cpu_setup
    clock = _fake_clock(hh, monkeypatch)
    config = {"host_health_enabled": True, "host_health_alerts_enabled": True,
             "host_health_persistence_minutes": 10, "host_health_cooldown_minutes": 60}
    hass.states.set("sensor.cpu", 95.0, unit_of_measurement="%", state_class="measurement")
    fired = []
    # confirm persistence (6 ticks) then keep polling well past it
    for _ in range(20):
        clock["t"] += 120
        _, calls = await _tick_silent(hh, monkeypatch, hass, config)
        fired += calls
    problem_calls = [c for c in fired if c[0] == "host_health_problem"]
    # 20 ticks * 120s = 2400s = 40 min < 60 min cooldown -> exactly one alert
    assert len(problem_calls) == 1


async def test_cooldown_allows_a_later_repeat_alert(hh, monkeypatch, cpu_setup):
    hass = cpu_setup
    clock = _fake_clock(hh, monkeypatch)
    config = {"host_health_enabled": True, "host_health_alerts_enabled": True,
             "host_health_persistence_minutes": 10, "host_health_cooldown_minutes": 20}
    hass.states.set("sensor.cpu", 95.0, unit_of_measurement="%", state_class="measurement")
    fired = []
    for _ in range(30):  # 30*120s = 3600s = 60 min, well past a 20-min cooldown
        clock["t"] += 120
        _, calls = await _tick_silent(hh, monkeypatch, hass, config)
        fired += calls
    problem_calls = [c for c in fired if c[0] == "host_health_problem"]
    assert len(problem_calls) >= 2


async def test_stable_recovery_announced_once(hh, monkeypatch, cpu_setup):
    hass = cpu_setup
    clock = _fake_clock(hh, monkeypatch)
    config = {"host_health_enabled": True, "host_health_alerts_enabled": True,
             "host_health_persistence_minutes": 10}
    hass.states.set("sensor.cpu", 95.0, unit_of_measurement="%", state_class="measurement")
    for _ in range(6):
        clock["t"] += 120
        await _tick_silent(hh, monkeypatch, hass, config)
    hass.states.set("sensor.cpu", 5.0, unit_of_measurement="%", state_class="measurement")
    fired = []
    for _ in range(6):
        clock["t"] += 120
        _, calls = await _tick_silent(hh, monkeypatch, hass, config)
        fired += calls
    recovery_calls = [c for c in fired if c[0] == "host_health_recovery"]
    assert len(recovery_calls) == 1


async def test_recovery_requires_persistence_too(hh, monkeypatch, cpu_setup):
    """A single good sample right after a confirmed problem must not
    immediately declare recovery."""
    hass = cpu_setup
    clock = _fake_clock(hh, monkeypatch)
    config = {"host_health_enabled": True, "host_health_alerts_enabled": True,
             "host_health_persistence_minutes": 10}
    hass.states.set("sensor.cpu", 95.0, unit_of_measurement="%", state_class="measurement")
    for _ in range(6):
        clock["t"] += 120
        await _tick_silent(hh, monkeypatch, hass, config)
    hass.states.set("sensor.cpu", 5.0, unit_of_measurement="%", state_class="measurement")
    clock["t"] += 120
    _, calls = await _tick_silent(hh, monkeypatch, hass, config)
    assert calls == []


async def test_reload_does_not_inherit_false_persistence(hh, monkeypatch, cpu_setup):
    """reset_state() simulates a fresh process start — accumulated
    persistence must not survive it."""
    hass = cpu_setup
    clock = _fake_clock(hh, monkeypatch)
    config = {"host_health_enabled": True, "host_health_alerts_enabled": True,
             "host_health_persistence_minutes": 10}
    hass.states.set("sensor.cpu", 95.0, unit_of_measurement="%", state_class="measurement")
    for _ in range(3):  # not yet persistent
        clock["t"] += 120
        await _tick_silent(hh, monkeypatch, hass, config)
    hh.reset_state()
    fired = []
    for _ in range(3):  # would have been the 4th-6th tick of the OLD streak
        clock["t"] += 120
        _, calls = await _tick_silent(hh, monkeypatch, hass, config)
        fired += calls
    assert fired == []  # streak restarted from zero, not resumed


async def test_entity_remap_resets_breach_streak(hh, monkeypatch, registry):
    registry.add(_sysmon_entry("sensor.cpu_a", "processor_use"))
    hass = FakeHass()
    hass.states.set("sensor.cpu_a", 95.0, unit_of_measurement="%", state_class="measurement")
    clock = _fake_clock(hh, monkeypatch)
    config = {"host_health_enabled": True, "host_health_alerts_enabled": True,
             "host_health_persistence_minutes": 10}
    for _ in range(3):
        clock["t"] += 120
        await _tick_silent(hh, monkeypatch, hass, config)
    # Remap to a different (also over-threshold) entity mid-streak.
    hass.states.set("sensor.cpu_b", 95.0, unit_of_measurement="%", state_class="measurement")
    config["host_health_mappings"] = {"cpu_percent": "sensor.cpu_b"}
    fired = []
    for _ in range(3):  # would complete the OLD streak, must not for the new source
        clock["t"] += 120
        _, calls = await _tick_silent(hh, monkeypatch, hass, config)
        fired += calls
    assert fired == []


async def test_feature_disabled_by_default_tick_is_noop(hh, cpu_setup):
    hass = cpu_setup
    res = await hh.tick(hass, {})
    assert res == {"skipped": "disabled"}


async def test_alerts_disabled_persistence_still_tracked_but_no_dispatch(hh, monkeypatch, cpu_setup):
    hass = cpu_setup
    clock = _fake_clock(hh, monkeypatch)
    config = {"host_health_enabled": True, "host_health_alerts_enabled": False,
             "host_health_persistence_minutes": 10}
    hass.states.set("sensor.cpu", 95.0, unit_of_measurement="%", state_class="measurement")
    dispatch_calls = []
    async def _fake_dispatch(*a, **k):
        dispatch_calls.append(1)
    monkeypatch.setattr(hh, "_dispatch_alert", _fake_dispatch)
    for _ in range(6):
        clock["t"] += 120
        await hh.tick(hass, config)
    assert dispatch_calls == []
    snap = hh.snapshot(hass, config)
    assert snap["persistent_problems"]  # still tracked/visible in diagnostics


async def test_combines_simultaneous_problems_into_one_message(hh, monkeypatch, registry):
    registry.add(_sysmon_entry("sensor.cpu", "processor_use"))
    registry.add(_sysmon_entry("sensor.mem", "memory_use_percent"))
    hass = FakeHass()
    hass.states.set("sensor.cpu", 95.0, unit_of_measurement="%", state_class="measurement")
    hass.states.set("sensor.mem", 95.0, unit_of_measurement="%", state_class="measurement")
    clock = _fake_clock(hh, monkeypatch)
    config = {"host_health_enabled": True, "host_health_alerts_enabled": True,
             "host_health_persistence_minutes": 10}
    fired = []
    for _ in range(6):
        clock["t"] += 120
        _, calls = await _tick_silent(hh, monkeypatch, hass, config)
        fired += calls
    problem_calls = [c for c in fired if c[0] == "host_health_problem"]
    assert len(problem_calls) == 1
    assert "processor use" in problem_calls[0][1].lower()
    assert "memory usage" in problem_calls[0][1].lower()
