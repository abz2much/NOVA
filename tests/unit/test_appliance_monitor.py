"""Fresh behaviour tests for appliance_monitor.py, written from the spec
(reports/independence/specs/appliance_monitor.md), not from the old tests.

Time is driven the way the existing appliance tests do it: by back-dating
the cycle's own timestamps (run_start, settle_start, a delta's start_time)
instead of sleeping. Registries, delivery and the honorific are faked at
their public seams; nothing reaches a real speaker or phone.
"""
from __future__ import annotations

import sys
import time
import types
from datetime import datetime, timedelta

import pytest

from conftest import _install_nova_runtime


# ── fixtures and helpers ────────────────────────────────────────────────────

@pytest.fixture
def am(load, fake_hass, monkeypatch):
    mod = load("appliance_monitor")
    mon = mod._MON
    for attr, value in (("hass", fake_hass), ("config", {}), ("entry", None),
                        ("sensors", {}), ("natives", {}), ("delta", None),
                        ("profile", []), ("disagg", []), ("claimed", set()),
                        ("unsub", None), ("unsub_settle", None), ("running", False)):
        monkeypatch.setattr(mon, attr, value)
    yield mod
    fake_hass.close_pending()


@pytest.fixture
def delivered(load, monkeypatch):
    """Capture every delivery request instead of speaking or pushing."""
    dl = load("delivery")
    out = []

    async def _deliver(hass, req, **kw):
        out.append(req)
        return None
    monkeypatch.setattr(dl, "async_deliver", _deliver)
    return out


@pytest.fixture
def honorific(load, monkeypatch):
    hm = load("honorific")
    value = {"h": "sir"}
    monkeypatch.setattr(hm, "effective_honorific", lambda hass: value["h"])
    return value


@pytest.fixture
def scheduled(am, monkeypatch):
    """Replace the announcer so scheduled announcements are recorded."""
    calls = []

    async def _nothing():
        return None

    def _announce(sensor, label):
        calls.append((sensor, label))
        return _nothing()
    monkeypatch.setattr(am, "_announce_done", _announce)
    return calls


@pytest.fixture
def timers(monkeypatch):
    """A stub homeassistant.helpers.event recording interval registrations."""
    ev = types.ModuleType("homeassistant.helpers.event")
    made = []

    def _interval(hass, action, interval):
        handle = {"action": action, "interval": interval, "cancelled": 0}

        def _cancel():
            handle["cancelled"] += 1
        handle["cancel"] = _cancel
        made.append(handle)
        return _cancel
    ev.async_track_time_interval = _interval
    ev.async_call_later = lambda *a, **k: (lambda: None)
    ev.async_track_state_change_event = lambda *a, **k: (lambda: None)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)
    return made


@pytest.fixture
def listens(fake_hass, monkeypatch):
    regs = []

    def _listen(event_type, handler):
        reg = {"type": event_type, "handler": handler, "cancelled": 0}

        def _cancel():
            reg["cancelled"] += 1
        regs.append(reg)
        return _cancel
    monkeypatch.setattr(fake_hass.bus, "async_listen", _listen)
    return regs


@pytest.fixture
def no_persisted(load, monkeypatch):
    jc = load("nova_config")
    store = {"cfg": {}}
    monkeypatch.setattr(jc, "get_all", lambda: dict(store["cfg"]))
    return store


class _Dev:
    def __init__(self, name, name_by_user=None, area_id=None):
        self.name, self.name_by_user, self.area_id = name, name_by_user, area_id


class _Ent:
    def __init__(self, entity_id, device_id=None, area_id=None):
        self.entity_id, self.device_id, self.area_id = entity_id, device_id, area_id


@pytest.fixture
def registry(monkeypatch):
    """Fake entity/device/area registries behind HA's helper modules."""
    ents, devs, areas = {}, {}, {}
    er = sys.modules["homeassistant.helpers.entity_registry"]
    dr = sys.modules["homeassistant.helpers.device_registry"]
    ar = sys.modules["homeassistant.helpers.area_registry"]
    monkeypatch.setattr(er, "async_get", lambda hass: types.SimpleNamespace(
        entities=ents, async_get=lambda eid: ents.get(eid)))
    monkeypatch.setattr(dr, "async_get", lambda hass: types.SimpleNamespace(
        devices=devs, async_get=lambda did: devs.get(did)))
    monkeypatch.setattr(ar, "async_get", lambda hass: types.SimpleNamespace(
        async_get_area=lambda aid: areas.get(aid), async_list_areas=lambda: list(areas.values())))

    class R:
        def entity(self, eid, device_id=None, area_id=None):
            ents[eid] = _Ent(eid, device_id, area_id)

        def device(self, did, name, name_by_user=None, area_id=None):
            devs[did] = _Dev(name, name_by_user, area_id)

        def area(self, aid, name):
            areas[aid] = types.SimpleNamespace(name=name)
    return R()


def _power(hass, eid, value, unit="W", **attrs):
    hass.states.set(eid, value, unit_of_measurement=unit, device_class="power", **attrs)


def _event(am, eid, new, unit=None, old=None):
    from homeassistant.core import State
    attrs = {"unit_of_measurement": unit} if unit else {}
    return am.Event("state_changed", {
        "entity_id": eid,
        "old_state": State(eid, old) if old is not None else None,
        "new_state": State(eid, new, attrs) if new is not None else None,
    })


def _sensor(am, eid="sensor.washer_power", atype=None, method="declared_entity",
            friendly="Washer", **kw):
    return am._SensorState(entity_id=eid, friendly_name=friendly,
                           appliance=atype or am.ApplianceType.WASHER,
                           discovery_method=method, **kw)


def _run(am, sensor, watts):
    """Take a sensor from idle into running, as a sustained draw would."""
    a = sensor.appliance
    sensor.run_start = time.time() - (a.sustained_seconds + 1)
    assert am._process_reading(sensor, watts) is None
    assert sensor.phase == "running"


def _settle(am, sensor, watts=0.0):
    """Feed a low reading whose settle wait has already passed."""
    if sensor.settle_start == 0.0:
        assert am._process_reading(sensor, watts) is None
    sensor.settle_start = time.time() - (sensor.appliance.settle_seconds + 1)
    return am._process_reading(sensor, watts)


# ── appliance profiles and constants ────────────────────────────────────────

@pytest.mark.parametrize("name,label,run,idle,sustain,settle", [
    ("WASHER", "washer", 50, 10, 120, 60),
    ("DRYER", "dryer", 100, 15, 120, 90),
    ("DISHWASHER", "dishwasher", 30, 5, 120, 60),
    ("OVEN", "oven", 500, 20, 180, 120),
    ("MICROWAVE", "microwave", 200, 10, 30, 15),
    ("GENERIC", "appliance", 50, 10, 120, 60),
])
def test_each_appliance_type_carries_its_thresholds(am, name, label, run, idle, sustain, settle):
    t = am.ApplianceType[name]
    assert (t.label, t.run_threshold, t.idle_threshold,
            t.sustained_seconds, t.settle_seconds) == (label, run, idle, sustain, settle)


def test_the_settle_recheck_runs_every_thirty_seconds(am):
    assert am.SETTLE_CHECK_SECONDS == 30


def test_monitor_state_starts_empty_and_has_no_guessing_switches(am):
    s = am._MonitorState()
    assert s.running is False and s.sensors == {} and s.natives == {}
    assert s.delta is None and s.profile == [] and s.disagg == [] and s.claimed == set()
    assert s.entry is None and s.hass is None
    assert not hasattr(s, "power_guessing") and not hasattr(s, "announce_unknown")


# ── classification ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("eid,fname,expected", [
    ("sensor.dishwasher_power", "", "DISHWASHER"),
    ("sensor.dish_washer_w", "", "DISHWASHER"),
    ("sensor.plug_7", "Tumble Dryer", "DRYER"),
    ("sensor.plug_8", "KITCHEN MICROWAVE", "MICROWAVE"),
    ("sensor.plug_9", "Range Hood", "OVEN"),
    ("sensor.plug_10", "Stove top", "OVEN"),
    ("sensor.washing_machine_power", "", "WASHER"),
    ("sensor.laundry_dryer_power", "", "WASHER"),     # longer "laundry" wins
    ("sensor.desk_lamp_power", "Desk Lamp", None),
])
def test_names_are_classified_most_specific_keyword_first(am, eid, fname, expected):
    got = am._classify_appliance(eid, fname)
    assert got == (am.ApplianceType[expected] if expected else None)


@pytest.mark.parametrize("text,expected", [
    ("dishwasher", "DISHWASHER"), (" Dryer ", "DRYER"), ("tumble_dryer", "DRYER"),
    ("microwave oven", "MICROWAVE"), ("oven", "OVEN"), ("washer", "WASHER"),
    ("appliance", "GENERIC"), ("", "GENERIC"), (None, "GENERIC"), ("kettle", "GENERIC"),
])
def test_declared_type_strings_map_to_a_type(am, text, expected):
    assert am._type_to_appliance(text) == am.ApplianceType[expected]


@pytest.mark.parametrize("watts,expected", [
    (100, None), (199.9, None), (200, "WASHER"), (599, "WASHER"),
    (600, "MICROWAVE"), (800, "MICROWAVE"), (1100, "MICROWAVE"),
    (1200, "DISHWASHER"), (1500, "DISHWASHER"), (1799, "DISHWASHER"),
    (1800, "DRYER"), (2500, "DRYER"), (6000, "DRYER"), (6001, "OVEN"),
])
def test_peak_power_fingerprints(am, watts, expected):
    got = am._fingerprint_from_power(watts)
    if expected is None:
        assert got is None
    else:
        atype, reason = got
        assert atype == am.ApplianceType[expected]
        assert isinstance(reason, str) and reason


# ── the declared appliance profile ──────────────────────────────────────────

def test_profile_entries_are_normalised(am):
    out = am._normalize_profile({"appliance_profile": [
        {"name": "  Washer ", "type": " Washer ", "entity": " sensor.washer_power ", "watts": "500"},
        {"name": "Kettle"},
        {"name": "Bad watts", "watts": "lots"},
        {"name": "", "watts": 100},
        "not a dict",
        {"type": "dryer"},
    ]})
    assert out == [
        {"name": "Washer", "type": "washer", "entity": "sensor.washer_power",
         "watts": 500.0, "learned_w": 500.0, "observed_n": 0},
        {"name": "Kettle", "type": "appliance", "entity": "", "watts": 0.0,
         "learned_w": 0.0, "observed_n": 0},
        {"name": "Bad watts", "type": "appliance", "entity": "", "watts": 0.0,
         "learned_w": 0.0, "observed_n": 0},
    ]


@pytest.mark.parametrize("raw,names", [
    ('[{"name": "Dryer", "watts": 4200}]', ["Dryer"]),
    ("", []), ("   ", []), ("{not json", []), ('{"name": "x"}', []), (None, []),
])
def test_profile_accepts_a_json_string(am, raw, names):
    cfg = {} if raw is None else {"appliance_profile": raw}
    assert [a["name"] for a in am._normalize_profile(cfg)] == names


def _decl(name, watts, type_="appliance"):
    return {"name": name, "type": type_, "entity": "", "watts": float(watts),
            "learned_w": float(watts), "observed_n": 0}


def test_a_load_matches_the_closest_declared_appliance(am):
    am._MON.disagg = [_decl("Dryer", 4000), _decl("Kettle", 2000)]
    best, err, ambiguous = am._match_declared(3800)
    assert best["name"] == "Dryer" and err == pytest.approx(0.05) and ambiguous is False


def test_small_appliances_get_a_250_watt_window(am):
    am._MON.disagg = [_decl("Fan heater", 300)]
    assert am._match_declared(540)[0]["name"] == "Fan heater"   # 80% off, inside 250 W
    assert am._match_declared(560)[0] is None


def test_a_load_matching_nothing_returns_none(am):
    am._MON.disagg = [_decl("Dryer", 4000), _decl("No watts", 0)]
    assert am._match_declared(900) == (None, None, False)


def test_two_close_candidates_are_ambiguous(am):
    am._MON.disagg = [_decl("A", 1000), _decl("B", 1050)]
    best, _err, ambiguous = am._match_declared(1020)
    assert best["name"] == "A" and ambiguous is True


def test_a_load_already_explained_by_a_running_claimed_sensor_matches_nothing(am):
    am._MON.disagg = [_decl("Dryer", 4000)]
    s = _sensor(am, "sensor.dryer_power", am.ApplianceType.DRYER)
    s.phase, s.peak_power, s.last_power = "running", 4100.0, 3900.0
    am._MON.sensors[s.entity_id] = s
    am._MON.claimed = {s.entity_id}
    assert am._match_declared(4000) == (None, None, False)
    am._MON.claimed = set()                     # not claimed: no longer explains it
    assert am._match_declared(4000)[0]["name"] == "Dryer"


def test_learning_moves_the_wattage_toward_a_confident_observation(am):
    ap = _decl("Dryer", 4000)
    am._learn_watts(ap, 4500, ambiguous=False)
    assert ap["learned_w"] == 4100.0 and ap["observed_n"] == 1


@pytest.mark.parametrize("observed,ambiguous", [(4500, True), (5100, False), (0, False), (-5, False)])
def test_learning_ignores_ambiguous_or_far_off_observations(am, observed, ambiguous):
    ap = _decl("Dryer", 4000)
    am._learn_watts(ap, observed, ambiguous=ambiguous)
    assert ap["learned_w"] == 4000.0 and ap["observed_n"] == 0


# ── the per-sensor state machine ────────────────────────────────────────────

def test_one_high_reading_only_arms_the_run_timer(am):
    s = _sensor(am)
    assert am._process_reading(s, 400) is None
    assert s.phase == "idle" and s.run_start > 0 and s.last_power == 400


def test_a_low_reading_while_idle_disarms_the_run_timer(am):
    s = _sensor(am)
    am._process_reading(s, 400)
    assert am._process_reading(s, 49.9) is None
    assert s.run_start == 0.0 and s.phase == "idle"


def test_a_draw_shorter_than_the_sustain_time_stays_idle(am):
    s = _sensor(am)
    am._process_reading(s, 400)
    am._process_reading(s, 400)
    assert s.phase == "idle"


def test_a_sustained_draw_starts_a_cycle(am):
    s = _sensor(am)
    s.announced = True
    s.run_start = time.time() - 121
    assert am._process_reading(s, 50) is None        # exactly the threshold counts
    assert s.phase == "running" and s.peak_power == 50 and s.announced is False


def test_peak_tracks_the_highest_running_reading(am):
    s = _sensor(am)
    _run(am, s, 300)
    am._process_reading(s, 650)
    am._process_reading(s, 200)
    assert s.peak_power == 650 and s.last_power == 200


def test_a_low_reading_arms_the_settle_timer_and_a_middling_one_disarms_it(am):
    s = _sensor(am)
    _run(am, s, 300)
    am._process_reading(s, 2)
    assert s.settle_start > 0 and s.phase == "running"
    am._process_reading(s, 10)                       # not below the idle threshold
    assert s.settle_start == 0.0 and s.phase == "running"


def test_a_settled_cycle_completes_once_and_returns_the_label(am):
    s = _sensor(am)
    _run(am, s, 500)
    assert _settle(am, s) == "washer"
    assert s.phase == "idle" and s.run_start == 0.0 and s.settle_start == 0.0
    assert s.announced is True and s.peak_power == 500
    assert am._process_reading(s, 0) is None


def test_a_low_reading_before_the_settle_time_does_not_complete(am):
    s = _sensor(am)
    _run(am, s, 500)
    assert am._process_reading(s, 0) is None
    assert am._process_reading(s, 0) is None
    assert s.phase == "running"


def test_a_second_cycle_completes_again(am):
    s = _sensor(am)
    _run(am, s, 500)
    assert _settle(am, s) == "washer"
    _run(am, s, 450)
    assert s.peak_power == 450
    assert _settle(am, s) == "washer"


def test_an_unidentified_sensor_is_fingerprinted_when_its_cycle_ends(am):
    s = _sensor(am, "sensor.plug_power", am.ApplianceType.GENERIC, "unidentified",
                friendly="Plug", identified=False)
    _run(am, s, 2500)
    assert _settle(am, s) == "dryer"
    assert s.appliance == am.ApplianceType.DRYER and s.identified is True
    assert s.discovery_method == "fingerprint:2500W"


def test_an_unrecognised_peak_leaves_the_sensor_generic(am):
    s = _sensor(am, "sensor.plug_power", am.ApplianceType.GENERIC, "unidentified",
                friendly="Plug", identified=False)
    _run(am, s, 120)
    assert _settle(am, s) == "appliance"
    assert s.identified is False and s.discovery_method == "unidentified"


def test_an_identified_sensor_is_never_refingerprinted(am):
    s = _sensor(am, method="declared_entity")
    _run(am, s, 2500)
    assert _settle(am, s) == "washer"
    assert s.appliance == am.ApplianceType.WASHER and s.discovery_method == "declared_entity"


# ── the settle re-check timer ───────────────────────────────────────────────

def test_the_recheck_ends_a_cycle_whose_sensor_went_quiet(am, scheduled):
    am._MON.running = True
    s = _sensor(am)
    _run(am, s, 500)
    am._process_reading(s, 0)
    am._MON.sensors[s.entity_id] = s
    am._settle_tick()
    assert s.phase == "running" and scheduled == []
    s.settle_start = time.time() - 61
    am._settle_tick(datetime.now())
    assert s.phase == "idle"
    assert [(x.entity_id, label) for x, label in scheduled] == [("sensor.washer_power", "washer")]
    assert len(am._MON.hass._tasks) == 1


def test_the_recheck_ignores_running_sensors_not_waiting_to_settle_and_idle_ones(am, scheduled):
    am._MON.running = True
    busy = _sensor(am, "sensor.a_washer_power")
    _run(am, busy, 500)
    idle = _sensor(am, "sensor.b_washer_power")
    idle.settle_start = time.time() - 1000          # stale but idle
    am._MON.sensors = {busy.entity_id: busy, idle.entity_id: idle}
    am._settle_tick()
    assert busy.phase == "running" and busy.settle_start == 0.0
    assert idle.phase == "idle" and scheduled == []


def test_the_recheck_does_nothing_when_the_monitor_is_stopped(am, scheduled):
    s = _sensor(am)
    _run(am, s, 500)
    am._process_reading(s, 0)
    s.settle_start = time.time() - 61
    am._MON.sensors[s.entity_id] = s
    am._settle_tick()
    assert s.phase == "running" and scheduled == []


def test_the_recheck_without_hass_completes_but_schedules_nothing(am, scheduled):
    am._MON.running = True
    am._MON.hass = None
    s = _sensor(am)
    _run(am, s, 500)
    am._process_reading(s, 0)
    s.settle_start = time.time() - 61
    am._MON.sensors[s.entity_id] = s
    am._settle_tick()
    assert s.phase == "idle" and scheduled == []


# ── state_changed handling ──────────────────────────────────────────────────

def test_events_are_ignored_while_stopped(am, fake_hass):
    s = _sensor(am)
    am._MON.sensors[s.entity_id] = s
    am._on_state_changed(_event(am, s.entity_id, "500"))
    assert s.last_power == 0.0


def test_a_removed_entity_event_is_ignored(am):
    am._MON.running = True
    s = _sensor(am)
    am._MON.sensors[s.entity_id] = s
    am._on_state_changed(_event(am, s.entity_id, None))
    assert s.last_power == 0.0


def test_power_events_feed_the_state_machine_in_watts(am):
    am._MON.running = True
    s = _sensor(am)
    am._MON.sensors[s.entity_id] = s
    am._on_state_changed(_event(am, s.entity_id, "0.5", unit="kW"))
    assert s.last_power == 500.0 and s.run_start > 0
    am._on_state_changed(_event(am, s.entity_id, "75", unit="W"))
    assert s.last_power == 75.0


@pytest.mark.parametrize("raw", ["unavailable", "unknown", "", "n/a"])
def test_unreadable_power_events_are_skipped(am, raw):
    am._MON.running = True
    s = _sensor(am)
    s.last_power = 42.0
    am._MON.sensors[s.entity_id] = s
    am._on_state_changed(_event(am, s.entity_id, raw))
    assert s.last_power == 42.0


def test_a_completed_cycle_from_events_schedules_one_announcement(am, scheduled):
    am._MON.running = True
    s = _sensor(am)
    am._MON.sensors[s.entity_id] = s
    s.run_start = time.time() - 200
    am._on_state_changed(_event(am, s.entity_id, "500"))
    am._on_state_changed(_event(am, s.entity_id, "1"))
    s.settle_start = time.time() - 100
    am._on_state_changed(_event(am, s.entity_id, "1"))
    am._on_state_changed(_event(am, s.entity_id, "1"))
    assert [label for _s, label in scheduled] == ["washer"]
    assert scheduled[0][0] is s


def test_unrelated_entities_are_ignored(am, scheduled):
    am._MON.running = True
    am._on_state_changed(_event(am, "light.kitchen", "on"))
    assert scheduled == []


def _native(am, eid="sensor.dishwasher_job_state", done=("finish", "finished"),
            last="", atype=None, name="Dishwasher"):
    n = am._NativeAppliance(entity_id=eid, device_name=name,
                            appliance=atype or am.ApplianceType.DISHWASHER,
                            trigger_states=frozenset(done), last_state=last)
    am._MON.natives[eid] = n
    am._MON.running = True
    return n


def test_a_native_first_event_is_absorbed_even_when_it_reads_done(am, scheduled):
    n = _native(am, last="wash")
    am._on_state_changed(_event(am, n.entity_id, "Finish"))
    assert scheduled == [] and n.seeded is True and n.announced is True
    assert n.last_state == "finish"


def test_a_native_first_event_that_is_not_done_seeds_quietly(am, scheduled):
    n = _native(am, last="unavailable")
    am._on_state_changed(_event(am, n.entity_id, "wash"))
    assert scheduled == [] and n.seeded is True and n.announced is False


def test_a_native_completion_after_seeding_is_announced(am, scheduled):
    n = _native(am, last="")
    am._on_state_changed(_event(am, n.entity_id, "wash"))
    am._on_state_changed(_event(am, n.entity_id, "finish"))
    assert len(scheduled) == 1
    synth, label = scheduled[0]
    assert label == "dishwasher"
    assert synth.entity_id == n.entity_id and synth.friendly_name == "Dishwasher"
    assert synth.appliance == am.ApplianceType.DISHWASHER
    assert synth.discovery_method == "native_status" and synth.peak_power == 0.0
    assert n.announced is True


def test_a_native_staying_done_or_moving_between_done_states_is_not_reannounced(am, scheduled):
    n = _native(am, last="")
    n.seeded = True
    n.last_state = "wash"
    am._on_state_changed(_event(am, n.entity_id, "finish"))
    am._on_state_changed(_event(am, n.entity_id, "finish"))
    am._on_state_changed(_event(am, n.entity_id, "finished"))
    assert len(scheduled) == 1


def test_a_native_leaving_done_rearms_it(am, scheduled):
    n = _native(am)
    n.seeded = True
    n.last_state = "wash"
    for state in ("finish", "rinse", "finish"):
        am._on_state_changed(_event(am, n.entity_id, state))
    assert len(scheduled) == 2 and n.announced is True


def test_a_native_announced_flag_blocks_a_completion(am, scheduled):
    n = _native(am)
    n.seeded, n.last_state, n.announced = True, "wash", True
    am._on_state_changed(_event(am, n.entity_id, "finish"))
    assert scheduled == []


def test_a_native_entity_is_handled_before_a_power_sensor_of_the_same_id(am, scheduled):
    n = _native(am, eid="sensor.dual")
    s = _sensor(am, "sensor.dual")
    am._MON.sensors[s.entity_id] = s
    am._on_state_changed(_event(am, "sensor.dual", "123"))
    assert n.last_state == "123" and s.last_power == 0.0


def test_the_whole_home_meter_reading_goes_to_delta_tracking(am):
    am._MON.running = True
    am._MON.delta = am._DeltaTracker(entity_id="sensor.home_power")
    am._on_state_changed(_event(am, "sensor.home_power", "1.2", unit="kW"))
    assert am._MON.delta.last_reading == 1200.0 and am._MON.delta.samples == [1200.0]
    am._on_state_changed(_event(am, "sensor.home_power", "unknown"))
    assert am._MON.delta.samples == [1200.0]


def test_a_power_sensor_takes_precedence_over_the_meter(am):
    am._MON.running = True
    am._MON.delta = am._DeltaTracker(entity_id="sensor.home_power")
    s = _sensor(am, "sensor.home_power")
    am._MON.sensors[s.entity_id] = s
    am._on_state_changed(_event(am, "sensor.home_power", "300"))
    assert s.last_power == 300 and am._MON.delta.samples == []


# ── whole-home delta tracking ───────────────────────────────────────────────

@pytest.fixture
def meter(am):
    am._MON.delta = am._DeltaTracker(entity_id="sensor.home_power", baseline_w=500.0)
    return am._MON.delta


def _feed(am, *readings):
    for w in readings:
        am._process_delta(w)


def test_the_delta_ids_count_up(am):
    t = am._DeltaTracker(entity_id="sensor.x")
    assert [t.next_id(), t.next_id()] == ["delta_1", "delta_2"]


def test_no_meter_means_nothing_happens(am):
    am._process_delta(5000)       # must not raise


def test_the_baseline_is_a_rolling_mean_of_the_last_twenty_quiet_readings(am, meter):
    _feed(am, *([100.0] * 5 + [300.0] * 20))
    assert meter.samples == [300.0] * 20 and meter.baseline_w == 300.0
    assert meter.last_reading == 300.0 and meter.active_deltas == {}


def test_the_first_reading_replaces_the_discovered_baseline(am, meter):
    _feed(am, 900)
    assert meter.baseline_w == 900 and meter.active_deltas == {}


def test_a_jump_without_a_profile_is_fingerprinted(am, meter):
    _feed(am, *([500.0] * 19))
    _feed(am, 2500)                         # baseline becomes 600, jump 1900
    (did, d), = meter.active_deltas.items()
    assert did == "delta_1"
    assert d["delta_w"] == pytest.approx(1900) and d["peak_w"] == 2500
    assert d["appliance_guess"] == "dryer" and d["matched"] is None
    assert d["announced"] is False and d["ambiguous"] is False and d["reason"]


def test_a_mid_range_jump_without_a_profile_is_guessed_a_washer(am, meter):
    _feed(am, *([500.0] * 19), 800)        # the jump joins the mean: ~285 W
    (d,) = meter.active_deltas.values()
    assert d["delta_w"] == pytest.approx(285) and d["appliance_guess"] == "washer"


def test_a_jump_matching_a_declared_appliance_is_named(am, meter):
    am._MON.profile = [_decl("Kettle", 1900, "appliance")]
    am._MON.disagg = list(am._MON.profile)
    _feed(am, *([500.0] * 19), 2500)
    (d,) = meter.active_deltas.values()
    assert d["appliance_guess"] == "Kettle" and d["matched"] is am._MON.disagg[0]
    assert "Kettle" in d["reason"]


def test_a_jump_matching_nothing_declared_is_unknown(am, meter):
    am._MON.profile = [_decl("Dryer", 4500)]
    am._MON.disagg = list(am._MON.profile)
    _feed(am, *([500.0] * 19), 2500)
    (d,) = meter.active_deltas.values()
    assert d["appliance_guess"] == "unknown" and d["matched"] is None


def test_a_similar_jump_is_not_tracked_twice_and_raises_the_peak(am, meter):
    _feed(am, *([500.0] * 19), 2500)
    _feed(am, 2700)
    (d,) = meter.active_deltas.values()
    assert d["peak_w"] == 2700


def test_the_baseline_is_frozen_while_a_load_is_active(am, meter):
    _feed(am, *([500.0] * 19), 2500)
    base = meter.baseline_w
    _feed(am, 2600, 2550)
    assert meter.baseline_w == base and len(meter.samples) == 20


def _age(meter, seconds):
    for d in meter.active_deltas.values():
        d["start_time"] = time.time() - seconds


def test_a_matched_load_that_ends_is_announced_by_name_and_learned(am, meter, scheduled):
    ap = _decl("Kettle", 1900, "kettle")
    am._MON.profile = [ap]
    am._MON.disagg = [ap]
    _feed(am, *([500.0] * 19), 2500)
    _age(meter, 31)
    _feed(am, 550)
    assert meter.active_deltas == {}
    (synth, label), = scheduled
    assert label == "Kettle" and synth.friendly_name == "Kettle"
    assert synth.entity_id == "sensor.home_power"
    assert synth.discovery_method == "whole_home_match:1900W"
    assert synth.appliance == am.ApplianceType.GENERIC and synth.peak_power == 2500
    assert ap["observed_n"] == 1 and ap["learned_w"] == pytest.approx(1900.0)


def test_a_load_that_ends_within_thirty_seconds_is_held(am, meter, scheduled):
    _feed(am, *([500.0] * 19), 2500)
    _age(meter, 10)
    _feed(am, 550)
    assert len(meter.active_deltas) == 1 and scheduled == []
    _age(meter, 31)
    _feed(am, 520)                            # a later quiet reading releases it
    assert meter.active_deltas == {} and len(scheduled) == 1


def test_a_load_still_drawing_is_not_ended(am, meter, scheduled):
    _feed(am, *([500.0] * 19), 2500)
    _age(meter, 600)
    _feed(am, 2400)
    assert len(meter.active_deltas) == 1 and scheduled == []


def test_an_unmatched_load_with_a_profile_is_dropped_silently(am, meter, scheduled):
    am._MON.profile = [_decl("Dryer", 4500)]
    am._MON.disagg = list(am._MON.profile)
    _feed(am, *([500.0] * 19), 2500)
    _age(meter, 31)
    _feed(am, 500)
    assert meter.active_deltas == {} and scheduled == []


def test_a_fingerprinted_load_without_a_profile_is_passed_on_as_a_guess(am, meter, scheduled):
    _feed(am, *([500.0] * 19), 2500)
    _age(meter, 31)
    _feed(am, 500)
    (synth, label), = scheduled
    assert label == "dryer" and synth.friendly_name == "Detected Dryer"
    assert synth.discovery_method == "whole_home_delta:1900W"
    assert synth.appliance == am.ApplianceType.DRYER


def test_an_unknown_load_without_a_profile_is_not_passed_on(am, meter, scheduled):
    _feed(am, *([500.0] * 19))
    _feed(am, 500 + 20 * 10.5)             # ~199.5 W jump: below 200, not a jump
    assert meter.active_deltas == {}
    meter.active_deltas["delta_9"] = {"start_time": time.time() - 40, "delta_w": 150.0,
                                      "peak_w": 650.0, "appliance_guess": "unknown",
                                      "reason": "", "matched": None, "ambiguous": False,
                                      "announced": False}
    _feed(am, 500)
    assert meter.active_deltas == {} and scheduled == []


def test_a_load_older_than_four_hours_is_forgotten(am, meter, scheduled):
    _feed(am, *([500.0] * 19), 2500)
    _age(meter, 14401)
    _feed(am, 2500)
    assert meter.active_deltas == {} and scheduled == []


# ── the announcement chokepoint ─────────────────────────────────────────────

async def test_a_trusted_completion_is_delivered_with_the_appliance_request(
        am, delivered, honorific):
    am._MON.config = {"notify_service": "notify.phone"}
    s = _sensor(am, "sensor.dryer_power", am.ApplianceType.DRYER, "declared_entity",
                friendly="Dryer", peak_power=3000.0)
    assert await am._announce_done(s, "dryer") is None
    (req,) = delivered
    assert req.source == "appliance" and req.category == "appliance"
    assert req.message == "Sir, the Dryer cycle is complete."
    assert req.entity_id == "sensor.dryer_power"
    assert req.push == "high" and req.title == "Nova" and req.tts_context == "sentinel"
    assert req.config is am._MON.config and req.motion_counts is True
    assert req.urgency == "medium" and req.requested_by_user is False
    assert req.speakers == () and req.quiet_ok is False and req.phone_only is False


@pytest.mark.parametrize("method", ["native_status", "declared_entity", "explicit_config",
                                    "whole_home_match:420W", "native_status_extra"])
async def test_trusted_provenances_speak(am, delivered, honorific, method):
    await am._announce_done(_sensor(am, method=method), "washer")
    assert len(delivered) == 1


@pytest.mark.parametrize("method", ["", "keyword", "device_sibling", "sibling_name",
                                    "area:laundry", "unidentified", "fingerprint:350W",
                                    "whole_home_delta:1900W", "whole_home_unmatched:100W"])
async def test_untrusted_provenances_stay_silent(am, delivered, honorific, method):
    await am._announce_done(_sensor(am, method=method), "washer")
    assert delivered == []


@pytest.mark.parametrize("atype,label,friendly", [
    ("OVEN", "oven", "Kitchen"),
    ("WASHER", "washer", "Garage Freezer"),
    ("GENERIC", "appliance", "Bedroom AC"),
    ("GENERIC", "appliance", "Loft A/C unit"),
    ("GENERIC", "appliance", "Heat Pump"),
    ("GENERIC", "Fridge", "Kitchen"),
    ("GENERIC", "appliance", "Induction Cooktop"),
])
async def test_never_announced_appliances_stay_silent_even_when_trusted(
        am, delivered, honorific, atype, label, friendly):
    s = _sensor(am, atype=am.ApplianceType[atype], method="native_status", friendly=friendly)
    await am._announce_done(s, label)
    assert delivered == []


def test_never_announce_rules(am):
    T = am.ApplianceType
    assert am._is_never_announce(T.OVEN, "x", "y") is True
    assert am._is_never_announce(T.WASHER, "washer", "Utility Room Washer") is False
    assert am._is_never_announce(T.GENERIC, "appliance", "Lilac lamp") is False
    assert am._is_never_announce(T.GENERIC, "appliance", "HVAC blower") is True
    assert am._is_never_announce(T.GENERIC, "appliance", "Ice Maker") is True


async def test_an_excluded_entity_stays_silent(am, fake_hass, delivered, honorific):
    _install_nova_runtime(fake_hass, {"excluded_entities": ["sensor.washer_power"]})
    await am._announce_done(_sensor(am), "washer")
    assert delivered == []
    await am._announce_done(_sensor(am, "sensor.other_washer_power"), "washer")
    assert len(delivered) == 1


@pytest.mark.parametrize("atype,label,friendly,sentence", [
    ("DRYER", "dryer", "Dryer", "Sir, the Dryer cycle is complete."),
    ("DRYER", "dryer", "Utility Plug", "Sir, Utility Plug has finished its cycle. The Dryer is done."),
    ("GENERIC", "appliance", "Workshop Saw", "Sir, Workshop Saw has finished its cycle."),
    ("WASHER", "Washer", "Washer", "Sir, the Washer cycle is complete."),
    ("WASHER", "big_washer", "Plug 3", "Sir, Plug 3 has finished its cycle. The Big Washer is done."),
    ("DRYER", "dryer", "", "Sir, the Dryer cycle is complete."),
])
async def test_message_wording(am, delivered, honorific, atype, label, friendly, sentence):
    s = _sensor(am, atype=am.ApplianceType[atype], friendly=friendly)
    await am._announce_done(s, label)
    assert delivered[0].message == sentence


async def test_no_honorific_capitalises_the_sentence(am, delivered, honorific):
    honorific["h"] = ""
    await am._announce_done(_sensor(am, atype=am.ApplianceType.DRYER, friendly="Dryer"), "dryer")
    assert delivered[0].message == "The Dryer cycle is complete."


async def test_a_failing_honorific_lookup_falls_back_to_config_then_sir(
        am, load, monkeypatch, delivered):
    def _boom(hass):
        raise RuntimeError("no people")
    monkeypatch.setattr(load("honorific"), "effective_honorific", _boom)
    am._MON.config = {"honorific": "madam"}
    await am._announce_done(_sensor(am, atype=am.ApplianceType.DRYER, friendly="Dryer"), "dryer")
    am._MON.config = {}
    await am._announce_done(_sensor(am, atype=am.ApplianceType.DRYER, friendly="Dryer"), "dryer")
    assert [r.message for r in delivered] == ["Madam, the Dryer cycle is complete.",
                                              "Sir, the Dryer cycle is complete."]


# ── discovery ───────────────────────────────────────────────────────────────

def test_discovery_finds_power_sensors_by_keyword(am, fake_hass):
    _power(fake_hass, "sensor.dishwasher_power", "3", friendly_name="Dishwasher Power")
    fake_hass.states.set("sensor.dryer_energy", "3", unit_of_measurement="kWh",
                         device_class="energy", friendly_name="Dryer Energy")
    fake_hass.states.set("binary_sensor.washer_door", "off", friendly_name="Washer")
    found = am._discover_sensors(fake_hass, (set(), set(), set()))
    assert set(found) == {"sensor.dishwasher_power"}
    s = found["sensor.dishwasher_power"]
    assert s.appliance == am.ApplianceType.DISHWASHER and s.discovery_method == "keyword"
    assert s.friendly_name == "Dishwasher Power" and s.identified is True and s.phase == "idle"


@pytest.mark.parametrize("unit", ["W", "kW", "watt", "Watts"])
def test_discovery_accepts_power_units_without_a_device_class(am, fake_hass, unit):
    fake_hass.states.set("sensor.washer_load", "1", unit_of_measurement=unit)
    assert "sensor.washer_load" in am._discover_sensors(fake_hass, (set(), set(), set()))


def test_discovery_honours_the_exclusion_snapshot(am, fake_hass):
    _power(fake_hass, "sensor.washer_power", "3")
    _power(fake_hass, "sensor.dryer_power", "3")
    found = am._discover_sensors(fake_hass, ({"sensor.washer_power"}, set(), set()))
    assert set(found) == {"sensor.dryer_power"}
    assert am._discover_sensors(fake_hass, (set(), {"sensor"}, set())) == {}


def test_unrecognised_power_sensors_are_tracked_as_unidentified(am, fake_hass):
    _power(fake_hass, "sensor.plug_3_power", "40", friendly_name="Plug 3")
    _power(fake_hass, "sensor.plug_4_power", "not-a-number")
    _power(fake_hass, "sensor.solar_power", "40")
    _power(fake_hass, "sensor.grid_power", "40")
    _power(fake_hass, "sensor.cabinet_power", "40")     # "net" inside "cabinet"
    _power(fake_hass, "sensor.big_power", "5001")
    _power(fake_hass, "sensor.big_kw_power", "6", unit="kW")
    found = am._discover_sensors(fake_hass, (set(), set(), set()))
    assert set(found) == {"sensor.plug_3_power", "sensor.plug_4_power"}
    s = found["sensor.plug_3_power"]
    assert s.appliance == am.ApplianceType.GENERIC and s.identified is False
    assert s.discovery_method == "unidentified" and s.friendly_name == "Plug 3"
    assert found["sensor.plug_4_power"].friendly_name == "sensor.plug_4_power"


def test_a_keyword_sensor_is_tracked_however_high_it_reads(am, fake_hass):
    _power(fake_hass, "sensor.oven_power", "7000")
    assert "sensor.oven_power" in am._discover_sensors(fake_hass, (set(), set(), set()))


def test_discovery_through_the_device_name(am, fake_hass, registry):
    registry.device("d1", "Plug", name_by_user="Dryer Plug")
    registry.entity("sensor.plug_power", device_id="d1")
    registry.entity("switch.plug", device_id="d1")
    _power(fake_hass, "sensor.plug_power", "3", friendly_name="Plug power")
    s = am._discover_sensors(fake_hass, (set(), set(), set()))["sensor.plug_power"]
    assert s.appliance == am.ApplianceType.DRYER and s.discovery_method == "device_sibling"
    assert s.friendly_name == "Dryer Plug" and s.sibling_entities == ["switch.plug"]


def test_discovery_through_a_sibling_name(am, fake_hass, registry):
    registry.device("d1", "Smart Plug 2")
    registry.entity("sensor.plug_2_power", device_id="d1")
    registry.entity("switch.plug_2", device_id="d1")
    fake_hass.states.set("switch.plug_2", "on", friendly_name="Dishwasher")
    _power(fake_hass, "sensor.plug_2_power", "3")
    s = am._discover_sensors(fake_hass, (set(), set(), set()))["sensor.plug_2_power"]
    assert s.appliance == am.ApplianceType.DISHWASHER and s.discovery_method == "sibling_name"
    assert s.friendly_name == "Dishwasher"


def test_discovery_through_the_area(am, fake_hass, registry):
    registry.area("a1", "Utility Room")
    registry.device("d1", "Plug 5", area_id="a1")
    registry.entity("sensor.plug_5_power", device_id="d1")
    _power(fake_hass, "sensor.plug_5_power", "3", friendly_name="Plug 5 power")
    s = am._discover_sensors(fake_hass, (set(), set(), set()))["sensor.plug_5_power"]
    assert s.appliance == am.ApplianceType.WASHER
    assert s.discovery_method == "area:utility room" and s.friendly_name == "Plug 5 power"


def test_native_discovery_patterns_and_done_states(am, fake_hass):
    fake_hass.states.set("binary_sensor.washer_run_completed", "off")
    fake_hass.states.set("sensor.lg_run_state", "Rinsing", friendly_name="LG Run State")
    fake_hass.states.set("sensor.dryer_job_state", "drying")
    fake_hass.states.set("sensor.boiler_temperature", "60")
    found = am._discover_native_appliances(fake_hass)
    assert set(found) == {"binary_sensor.washer_run_completed", "sensor.lg_run_state",
                          "sensor.dryer_job_state"}
    w = found["binary_sensor.washer_run_completed"]
    assert w.trigger_states == frozenset({"on"}) and w.appliance == am.ApplianceType.WASHER
    lg = found["sensor.lg_run_state"]
    assert lg.trigger_states == frozenset({"end", "finished", "finish", "complete", "completed"})
    assert lg.appliance == am.ApplianceType.GENERIC and lg.last_state == "rinsing"
    assert lg.device_name == "lg run state"            # lowercased friendly name
    assert lg.seeded is True and lg.announced is False   # a real state seeds it
    d = found["sensor.dryer_job_state"]
    assert {"finish", "finished"} <= d.trigger_states and d.device_name == "sensor.dryer_job_state"


def test_native_discovery_keeps_one_entity_per_device_binary_first(am, fake_hass, registry):
    registry.device("d1", "Washer")
    registry.entity("sensor.machine_run_state", device_id="d1")
    registry.entity("binary_sensor.machine_run_completed", device_id="d1")
    fake_hass.states.set("sensor.machine_run_state", "idle")
    fake_hass.states.set("binary_sensor.machine_run_completed", "off")
    found = am._discover_native_appliances(fake_hass)
    assert set(found) == {"binary_sensor.machine_run_completed"}
    n = found["binary_sensor.machine_run_completed"]
    assert n.device_name == "Washer" and n.appliance == am.ApplianceType.WASHER


def test_whole_home_meter_discovery(am, fake_hass):
    _power(fake_hass, "sensor.main_power", "2.5", unit="kW")
    _power(fake_hass, "sensor.circuit", "9000", friendly_name="Electric Consumption (2)")
    _power(fake_hass, "sensor.other", "800", friendly_name="Mains Power")
    fake_hass.states.set("sensor.home_energy", "5", unit_of_measurement="kWh",
                         friendly_name="Home Energy")
    t = am._discover_whole_home_meter(fake_hass)
    assert t.entity_id == "sensor.main_power"
    assert t.baseline_w == 2500.0 and t.last_reading == 2500.0
    assert t.samples == [] and t.active_deltas == {}


def test_whole_home_meter_with_an_unreadable_value_reads_zero(am, fake_hass):
    _power(fake_hass, "sensor.whole_house_power", "unavailable")
    t = am._discover_whole_home_meter(fake_hass)
    assert t.entity_id == "sensor.whole_house_power" and t.baseline_w == 0.0


def test_no_whole_home_meter(am, fake_hass):
    _power(fake_hass, "sensor.washer_power", "1")
    assert am._discover_whole_home_meter(fake_hass) is None


# ── live settings ───────────────────────────────────────────────────────────

def test_live_settings_without_an_owner_are_empty(am):
    assert am._live_runtime_config() == {}


def test_live_settings_come_from_the_owner_runtime(am, fake_hass):
    live = {"appliance_profile": [{"name": "Dryer"}]}
    am._MON.entry = _install_nova_runtime(fake_hass, live)
    assert am._live_runtime_config() is live


# ── start / stop / status ───────────────────────────────────────────────────

async def test_start_with_nothing_to_watch_does_not_run(am, fake_hass, timers, listens, no_persisted):
    await am.start(fake_hass, {"appliance_profile": [{"name": "Kettle", "watts": 2000}]})
    assert am.is_running() is False and listens == [] and timers == []
    assert [a["name"] for a in am._MON.profile] == ["Kettle"]


async def test_start_watches_power_sensors_with_a_settle_timer(
        am, fake_hass, timers, listens, no_persisted, scheduled):
    _power(fake_hass, "sensor.washer_power", "0")
    await am.start(fake_hass, {})
    assert am.is_running() is True
    assert [r["type"] for r in listens] == ["state_changed"]
    (t,) = timers
    assert t["interval"] == timedelta(seconds=30)
    # the registered handler is the event entry point
    s = am._MON.sensors["sensor.washer_power"]
    listens[0]["handler"](_event(am, "sensor.washer_power", "77"))
    assert s.last_power == 77
    # the timer callback ends a quiet cycle
    _run(am, s, 500)
    am._process_reading(s, 0)
    s.settle_start = time.time() - 100
    t["action"](datetime.now())
    assert s.phase == "idle" and len(scheduled) == 1


async def test_start_with_only_natives_has_no_settle_timer(
        am, fake_hass, timers, listens, no_persisted):
    fake_hass.states.set("binary_sensor.dryer_run_completed", "off")
    await am.start(fake_hass, {})
    assert am.is_running() and len(listens) == 1 and timers == []
    assert set(am._MON.natives) == {"binary_sensor.dryer_run_completed"}


async def test_stop_releases_everything(am, fake_hass, timers, listens, no_persisted):
    _power(fake_hass, "sensor.washer_power", "0")
    _power(fake_hass, "sensor.main_power", "500")
    entry = _install_nova_runtime(fake_hass, {})
    await am.start(fake_hass, {}, entry=entry)
    assert am._MON.entry is entry and am._MON.delta is not None
    await am.stop()
    assert listens[0]["cancelled"] == 1 and timers[0]["cancelled"] == 1
    assert am.is_running() is False and am._MON.sensors == {} and am._MON.natives == {}
    assert am._MON.delta is None and am._MON.entry is None and am._MON.unsub_settle is None


async def test_stop_before_start_is_safe(am):
    await am.stop()
    assert am.is_running() is False


async def test_stop_swallows_a_failing_unsubscribe(am):
    def _bad():
        raise RuntimeError("gone")
    am._MON.unsub = _bad
    am._MON.unsub_settle = _bad
    am._MON.running = True
    await am.stop()
    assert am.is_running() is False


async def test_starting_again_stops_the_previous_run(am, fake_hass, timers, listens, no_persisted):
    _power(fake_hass, "sensor.washer_power", "0")
    await am.start(fake_hass, {})
    await am.start(fake_hass, {})
    assert listens[0]["cancelled"] == 1 and timers[0]["cancelled"] == 1
    assert len(listens) == 2 and am.is_running()


async def test_the_live_profile_wins_over_the_saved_one(am, fake_hass, timers, listens, no_persisted):
    no_persisted["cfg"] = {"appliance_profile": [{"name": "Saved", "watts": 100}]}
    entry = _install_nova_runtime(fake_hass, {"appliance_profile": '[{"name": "Live", "watts": 900}]'})
    cfg = {"appliance_profile": [{"name": "Boot", "watts": 1}], "honorific": "sir"}
    await am.start(fake_hass, cfg, entry=entry)
    assert [a["name"] for a in am._MON.profile] == ["Live"]
    assert am._MON.config["appliance_profile"] == '[{"name": "Live", "watts": 900}]'
    assert am._MON.config["honorific"] == "sir"
    assert cfg["appliance_profile"] == [{"name": "Boot", "watts": 1}]     # caller's dict untouched


async def test_the_saved_profile_is_used_when_the_live_one_is_absent(
        am, fake_hass, timers, listens, no_persisted):
    no_persisted["cfg"] = {"appliance_profile": [{"name": "Saved", "watts": 100}]}
    await am.start(fake_hass, {"appliance_profile": [{"name": "Boot", "watts": 1}]})
    assert [a["name"] for a in am._MON.profile] == ["Saved"]


async def test_the_boot_profile_is_the_last_resort(am, fake_hass, timers, listens, load, monkeypatch):
    def _broken():
        raise OSError("disk")
    monkeypatch.setattr(load("nova_config"), "get_all", _broken)
    await am.start(fake_hass, {"appliance_profile": [{"name": "Boot", "watts": 1}]})
    assert [a["name"] for a in am._MON.profile] == ["Boot"]


async def test_the_saved_config_is_read_in_the_executor(am, fake_hass, timers, listens,
                                                         load, monkeypatch):
    jobs = []
    real = fake_hass.async_add_executor_job

    async def _job(func, *args):
        jobs.append(func)
        return await real(func, *args)
    monkeypatch.setattr(fake_hass, "async_add_executor_job", _job)
    monkeypatch.setattr(load("nova_config"), "get_all", lambda: {})
    await am.start(fake_hass, {})
    assert load("nova_config").get_all in jobs
    assert len(jobs) >= 4       # saved config + three discovery passes


async def test_declared_appliances_are_wired_by_kind(am, fake_hass, timers, listens, no_persisted):
    # "total" keeps discovery off it, so the declared mapping is what adds it
    _power(fake_hass, "sensor.plug_9_total_power", "0", friendly_name="Plug 9")
    fake_hass.states.set("binary_sensor.tumble_done", "off")
    fake_hass.states.set("select.lg_job_state", "idle")    # a domain discovery never scans
    await am.start(fake_hass, {"appliance_profile": [
        {"name": "Dryer", "type": "dryer", "entity": "sensor.plug_9_total_power", "watts": 0},
        {"name": "Tumble", "type": "dryer", "entity": "binary_sensor.tumble_done"},
        {"name": "Washer", "type": "washer", "entity": "select.lg_job_state"},
        {"name": "Missing", "type": "washer", "entity": "sensor.not_there_power"},
        {"name": "Kettle", "type": "appliance", "watts": 2000},
        {"name": "Toaster"},
    ]})
    s = am._MON.sensors["sensor.plug_9_total_power"]
    assert s.discovery_method == "declared_entity" and s.appliance == am.ApplianceType.DRYER
    assert s.friendly_name == "Plug 9" and s.identified is True
    tumble = am._MON.natives["binary_sensor.tumble_done"]
    assert tumble.trigger_states == frozenset({"on"}) and tumble.device_name == "Tumble"
    assert tumble.appliance == am.ApplianceType.DRYER and tumble.last_state == "off"
    washer = am._MON.natives["select.lg_job_state"]
    assert "finish" in washer.trigger_states and washer.device_name == "Washer"
    assert washer.last_state == "idle" and washer.seeded is True
    # a declared entity with no state yet is taken for a completion entity
    missing = am._MON.natives["sensor.not_there_power"]
    assert missing.trigger_states == frozenset({"on"}) and missing.last_state == ""
    assert "sensor.not_there_power" not in am._MON.sensors
    assert am._MON.claimed == {"sensor.plug_9_total_power", "binary_sensor.tumble_done",
                               "select.lg_job_state", "sensor.not_there_power"}
    assert [a["name"] for a in am._MON.disagg] == ["Kettle"]
    assert am.status()["profile"][-1]["mode"] == "declared"


async def test_explicit_sensors_from_config(am, fake_hass, timers, listens, no_persisted):
    fake_hass.states.set("sensor.socket_a", "0", friendly_name="Socket A")
    await am.start(fake_hass, {"appliance_sensors": {
        "sensor.socket_a": "dishwasher", "sensor.socket_b": "Garage thing"}})
    a = am._MON.sensors["sensor.socket_a"]
    assert a.discovery_method == "explicit_config" and a.appliance == am.ApplianceType.DISHWASHER
    assert a.friendly_name == "Socket A" and a.identified is True
    b = am._MON.sensors["sensor.socket_b"]
    assert b.appliance == am.ApplianceType.GENERIC and b.identified is False
    assert b.friendly_name == "Garage thing"


async def test_a_native_on_the_same_device_supersedes_its_power_sensor(
        am, fake_hass, registry, timers, listens, no_persisted):
    registry.device("d1", "Washer")
    registry.entity("sensor.washer_power", device_id="d1")
    registry.entity("binary_sensor.washer_run_completed", device_id="d1")
    _power(fake_hass, "sensor.washer_power", "0")
    _power(fake_hass, "sensor.dryer_power", "0")
    fake_hass.states.set("binary_sensor.washer_run_completed", "off")
    await am.start(fake_hass, {})
    assert set(am._MON.sensors) == {"sensor.dryer_power"}
    assert set(am._MON.natives) == {"binary_sensor.washer_run_completed"}


async def test_start_discovers_with_the_current_exclusions(am, fake_hass, timers, listens, no_persisted):
    entry = _install_nova_runtime(fake_hass, {"excluded_entities": ["sensor.washer_power"]})
    _power(fake_hass, "sensor.washer_power", "0")
    _power(fake_hass, "sensor.dryer_power", "0")
    await am.start(fake_hass, {}, entry=entry)
    assert set(am._MON.sensors) == {"sensor.dryer_power"}


def test_status_when_idle(am):
    assert am.status() == {"running": False, "profile": [], "sensors": {},
                           "native_appliances": {}, "whole_home_delta": None}


async def test_status_shape(am, fake_hass, timers, listens, no_persisted, registry):
    registry.device("d1", "Plug")
    registry.entity("sensor.washer_power", device_id="d1")
    for i in range(7):
        registry.entity(f"switch.s{i}", device_id="d1")
    _power(fake_hass, "sensor.washer_power", "0", friendly_name="Washer Power")
    _power(fake_hass, "sensor.main_power", "450")
    fake_hass.states.set("sensor.dryer_job_state", "Drying")
    await am.start(fake_hass, {"appliance_profile": [
        {"name": "Washer", "type": "washer", "entity": "sensor.washer_power"},
        {"name": "Kettle", "watts": 1999.6},
    ]})
    am._MON.delta.active_deltas["delta_1"] = {
        "start_time": time.time() - 65, "delta_w": 2000.0, "peak_w": 2450.0,
        "appliance_guess": "Kettle"}
    st = am.status()
    assert st["running"] is True
    assert st["profile"] == [
        {"name": "Washer", "type": "washer", "entity": "sensor.washer_power", "watts": 0.0,
         "learned_w": 0, "observed_n": 0, "mode": "entity"},
        {"name": "Kettle", "type": "appliance", "entity": "", "watts": 1999.6,
         "learned_w": 2000, "observed_n": 0, "mode": "disaggregation"},
    ]
    w = st["sensors"]["sensor.washer_power"]
    assert w == {"appliance": "washer", "phase": "idle", "power_w": 0.0, "peak_w": 0.0,
                 "friendly_name": "Washer Power", "discovery": "declared_entity",
                 "identified": True, "siblings": [f"switch.s{i}" for i in range(5)]}
    assert st["native_appliances"]["sensor.dryer_job_state"] == {
        "appliance": "dryer", "device_name": "sensor.dryer_job_state",
        "trigger_state": sorted({"finished", "finish", "end", "complete", "completed"}),
        "current_state": "drying", "announced": False}
    wh = st["whole_home_delta"]
    assert wh["entity_id"] == "sensor.main_power" and wh["baseline_w"] == 450.0
    assert wh["last_reading_w"] == 450.0 and wh["active_events"] == 1
    d = wh["deltas"]["delta_1"]
    assert d["delta_w"] == 2000.0 and d["peak_w"] == 2450.0 and d["guess"] == "Kettle"
    assert 64 <= d["duration_s"] <= 70


# ── 8.49.22 regressions ─────────────────────────────────────────────────────

async def test_a_declared_entity_already_found_by_keyword_is_announced(
        am, fake_hass, timers, listens, no_persisted, delivered, honorific):
    """Bug 1: the declaration must win over discovery's untrusted guess."""
    _power(fake_hass, "sensor.washer_power", "0", friendly_name="Washer Power")
    await am.start(fake_hass, {"appliance_profile": [
        {"name": "Washer", "type": "washer", "entity": "sensor.washer_power"}]})
    s = am._MON.sensors["sensor.washer_power"]
    assert s.discovery_method == "declared_entity" and s.identified is True
    await am._announce_done(s, "washer")
    assert len(delivered) == 1


async def test_a_cycle_under_way_when_the_monitor_starts_is_announced(
        am, fake_hass, timers, listens, no_persisted, scheduled):
    """Bug 2: a restart or a save mid-cycle must not swallow the finish."""
    fake_hass.states.set("sensor.dishwasher_job_state", "drying")
    _power(fake_hass, "sensor.washer_power", "450")
    await am.start(fake_hass, {})
    am._on_state_changed(_event(am, "sensor.dishwasher_job_state", "finished",
                                old="drying"))
    assert [label for _s, label in scheduled] == ["dishwasher"]
    washer = am._MON.sensors["sensor.washer_power"]
    assert washer.phase == "running"
    assert _settle(am, washer) == "washer"


def test_the_whole_home_meter_sees_overlapping_loads_and_heater_pauses(
        am, meter, scheduled):
    """Bug 3: a kettle during a dryer cycle is seen, and a washer heater
    switching off mid-cycle is not a finish."""
    dryer, kettle, washer = (_decl("Dryer", 2400, "dryer"), _decl("Kettle", 2000, "kettle"),
                             _decl("Washer", 2000, "washer"))
    am._MON.profile = [dryer, kettle]
    am._MON.disagg = [dryer, kettle]
    _feed(am, *([500.0] * 19), 3000)           # dryer: ~2375 W over the baseline
    _age(meter, 60)
    _feed(am, 5000)                            # kettle on top: a 2000 W step
    assert sorted(d["appliance_guess"] for d in meter.active_deltas.values()) == [
        "Dryer", "Kettle"]
    _age(meter, 60)
    _feed(am, 3000)                            # kettle off, dryer still on
    assert [label for _s, label in scheduled] == ["Kettle"]
    assert [d["appliance_guess"] for d in meter.active_deltas.values()] == ["Dryer"]

    meter.active_deltas.clear()
    scheduled.clear()
    am._MON.profile = [washer]
    am._MON.disagg = [washer]
    _feed(am, 2600)                            # washer heater on
    _age(meter, 60)
    _feed(am, 650)                             # heater off: a pause, not a finish
    assert scheduled == [] and len(meter.active_deltas) == 1
    _feed(am, 2600)                            # heater back on: the same cycle
    (d,) = meter.active_deltas.values()
    assert "ended_at" not in d
    _feed(am, 650)
    d["ended_at"] -= am._CYCLE_PAUSE_S + 1
    _feed(am, 640)                             # off for the whole pause: finished
    assert [label for _s, label in scheduled] == ["Washer"]
    assert meter.active_deltas == {}


async def test_the_whole_home_meter_is_not_also_an_appliance(
        am, fake_hass, timers, listens, no_persisted):
    """Bug 4: a meter named like "whole house power" is only the meter."""
    _power(fake_hass, "sensor.whole_house_power", "800", friendly_name="Whole House Power")
    _power(fake_hass, "sensor.washer_power", "0")
    await am.start(fake_hass, {})
    assert am._MON.delta.entity_id == "sensor.whole_house_power"
    assert set(am._MON.sensors) == {"sensor.washer_power"}
    am._on_state_changed(_event(am, "sensor.whole_house_power", "900"))
    assert am._MON.delta.last_reading == 900.0


# ── 8.66: stop forgets the state listener it has removed ────────────────────

async def test_stopping_twice_removes_the_listener_once(am, fake_hass, timers, listens, no_persisted):
    # Home Assistant logs an error with a traceback when a listener is removed
    # twice. Before 8.66 stop() kept the removed listener, so a second stop
    # (unload after a panel reload that found nothing) removed it again.
    _power(fake_hass, "sensor.washer_power", "0")
    await am.start(fake_hass, {})
    await am.stop()
    await am.stop()
    assert listens[0]["cancelled"] == 1 and timers[0]["cancelled"] == 1
    assert am._MON.unsub is None


async def test_a_reload_that_finds_nothing_leaves_no_listener_behind(
        am, fake_hass, timers, listens, no_persisted):
    _power(fake_hass, "sensor.washer_power", "0")
    await am.start(fake_hass, {})
    fake_hass.states.remove("sensor.washer_power")
    await am.start(fake_hass, {})
    assert am.is_running() is False
    await am.stop()
    assert listens[0]["cancelled"] == 1


# ── characterisation (8.66): pinned before the rewrite of the type table ────

def test_appliance_type_values_and_order(am):
    assert [(t.name, t.value) for t in am.ApplianceType] == [
        ("WASHER", ("washer", 50, 10, 120, 60)),
        ("DRYER", ("dryer", 100, 15, 120, 90)),
        ("DISHWASHER", ("dishwasher", 30, 5, 120, 60)),
        ("OVEN", ("oven", 500, 20, 180, 120)),
        ("MICROWAVE", ("microwave", 200, 10, 30, 15)),
        ("GENERIC", ("appliance", 50, 10, 120, 60)),
    ]
    t = am.ApplianceType.OVEN
    assert (t.label, t.run_threshold, t.idle_threshold, t.sustained_seconds,
            t.settle_seconds) == ("oven", 500, 20, 180, 120)
    assert all(type(v) is int for t in am.ApplianceType for v in t.value[1:])
