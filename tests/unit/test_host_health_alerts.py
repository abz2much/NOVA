"""host_health.py's alert dispatch — reuses output_gate / audio_routing /
tts_helper / notify_targets, the exact pieces appliance_monitor.py's own
proactive announcements use. No standalone alert dispatcher.
"""
import pytest

from fakes import FakeHass


@pytest.fixture
def hh(load):
    return load("host_health")


@pytest.fixture(autouse=True)
def _reset_state(hh):
    hh.reset_state()
    yield
    hh.reset_state()


async def test_dispatch_routes_through_output_gate_can_announce(hh, monkeypatch):
    calls = {"can_announce": None, "record": []}

    class FakeOutputGate:
        @staticmethod
        def can_announce(**kw):
            calls["can_announce"] = kw
            return True, "ok"

        @staticmethod
        def record_announcement(**kw):
            calls["record"].append(kw)

    import sys, types
    fake_mod = types.ModuleType("jc.output_gate")
    fake_mod.can_announce = FakeOutputGate.can_announce
    fake_mod.record_announcement = FakeOutputGate.record_announcement
    monkeypatch.setitem(sys.modules, "jc.output_gate", fake_mod)

    audio_mod = types.ModuleType("jc.audio_routing")
    audio_mod.observer_speak_target = lambda hass, **kw: ([], "suppressed")
    monkeypatch.setitem(sys.modules, "jc.audio_routing", audio_mod)

    sleep_mod = types.ModuleType("jc.sleep_detection")
    sleep_mod.is_sleeping = lambda hass, **kw: (False, "")
    monkeypatch.setitem(sys.modules, "jc.sleep_detection", sleep_mod)

    notify_mod = types.ModuleType("jc.notify_targets")
    sent = []
    async def _fake_send(hass, config, payload, **kw):
        sent.append(payload)
        return []
    notify_mod.async_send_configured_notifications = _fake_send
    monkeypatch.setitem(sys.modules, "jc.notify_targets", notify_mod)

    await hh._dispatch_alert(FakeHass(), {}, "test message", "host_health_problem")

    assert calls["can_announce"]["category"] == "host_health_problem"
    assert calls["can_announce"]["urgency"] == "medium"  # never critical — not a safety alert
    assert calls["can_announce"]["entity_id"] == "host_health"
    assert sent and sent[0]["message"] == "test message"
    assert calls["record"]  # was_spoken recorded either way


async def test_dispatch_suppressed_by_output_gate_never_reaches_notify(hh, monkeypatch):
    import sys, types

    gate_mod = types.ModuleType("jc.output_gate")
    gate_mod.can_announce = lambda **kw: (False, "rate limit")
    recorded = []
    gate_mod.record_announcement = lambda **kw: recorded.append(kw)
    monkeypatch.setitem(sys.modules, "jc.output_gate", gate_mod)

    notify_mod = types.ModuleType("jc.notify_targets")
    sent = []
    async def _fake_send(*a, **k):
        sent.append(1)
        return []
    notify_mod.async_send_configured_notifications = _fake_send
    monkeypatch.setitem(sys.modules, "jc.notify_targets", notify_mod)

    await hh._dispatch_alert(FakeHass(), {}, "msg", "host_health_problem")
    assert sent == []
    assert recorded and recorded[0]["was_spoken"] is False


async def test_delivery_failure_does_not_raise_or_loop(hh, monkeypatch):
    """A hard failure anywhere in the dispatch chain must be swallowed —
    the tick() caller already stamped last_alert_ts before calling this,
    so even a total dispatch failure can't cause an immediate retry."""
    import sys, types

    gate_mod = types.ModuleType("jc.output_gate")
    def _boom(**kw):
        raise RuntimeError("gate exploded")
    gate_mod.can_announce = _boom
    gate_mod.record_announcement = lambda **kw: None
    monkeypatch.setitem(sys.modules, "jc.output_gate", gate_mod)

    # Must not raise.
    await hh._dispatch_alert(FakeHass(), {}, "msg", "host_health_problem")


async def test_tick_stamps_last_alert_ts_even_if_dispatch_raises(hh, monkeypatch):
    from fakes import FakeEntityRegistry, FakeRegistryEntry
    reg = FakeEntityRegistry()
    reg.add(FakeRegistryEntry("sensor.cpu", "systemmonitor", unique_id="processor_use_",
                              translation_key="processor_use"))
    import homeassistant.helpers.entity_registry as er_mod
    monkeypatch.setattr(er_mod, "async_get", lambda hass: reg)

    hass = FakeHass()
    hass.states.set("sensor.cpu", 95.0, unit_of_measurement="%", state_class="measurement")

    box = {"t": 0.0}
    monkeypatch.setattr(hh, "_now", lambda: box["t"])

    async def _boom(*a, **k):
        raise RuntimeError("dispatch exploded")
    monkeypatch.setattr(hh, "_dispatch_alert", _boom)

    config = {"host_health_enabled": True, "host_health_alerts_enabled": True,
             "host_health_persistence_minutes": 10}
    for _ in range(6):
        box["t"] += 120
        await hh.tick(hass, config)  # must never raise despite _dispatch_alert raising

    st = hh._STATE["cpu_percent"]
    assert st["last_alert_ts"] is not None
