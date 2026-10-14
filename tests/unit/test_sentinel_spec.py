"""Sentinel, checked against its spec (7d).

Sentinel watches doors, windows, garage doors and locks and, when one stays
open or unlocked past its rule's limit, says so (and keeps saying so, with
the true total time) through the one delivery path. Everything outward is
faked: the event helpers, the registries, the stores, delivery and the model.
"""
import datetime as _dt
import json
import sys
import types

import pytest

from fakes import FakeHass


@pytest.fixture
def sm(load, monkeypatch):
    ev = types.ModuleType("homeassistant.helpers.event")
    ev.tracked = []

    def _track_state(hass, ids, cb):
        unsub = types.SimpleNamespace(calls=0, ids=list(ids))

        def _off():
            unsub.calls += 1
        ev.tracked.append(("state", list(ids), cb, unsub))
        return _off

    def _track_time(hass, cb, every):
        ev.tracked.append(("time", every, cb, None))
        return lambda: ev.tracked.append(("stopped", every, cb, None))
    ev.async_track_state_change_event = _track_state
    ev.async_track_time_interval = _track_time
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)
    mod = load("sentinel")
    monkeypatch.setattr(mod, "async_track_state_change_event", _track_state)
    monkeypatch.setattr(mod, "async_track_time_interval", _track_time)
    mod._ev = ev
    return mod


class _Reg:
    def __init__(self, entries=None, devices=None):
        self.entries = entries or {}
        self.devices = devices or {}

    def async_get(self, entity_id):
        return self.entries.get(entity_id)


def _entry(entity_id, device_id=None, category=None):
    return types.SimpleNamespace(entity_id=entity_id, device_id=device_id,
                                 entity_category=category)


@pytest.fixture
def reg(sm, monkeypatch):
    registry = _Reg()
    monkeypatch.setattr(sm.er, "async_get", lambda hass: registry)
    monkeypatch.setattr(sm.er, "async_entries_for_device",
                        lambda r, device_id, include_disabled_entities=False:
                        registry.devices.get(device_id, []), raising=False)
    return registry


@pytest.fixture
def world(sm, load, monkeypatch, reg):
    """Settings, exclusions, sleep, honorific, stores and delivery, all faked."""
    w = types.SimpleNamespace(settings={}, excluded=set(), sleeping=False,
                              honorific="sir", saved=[], messages=[], delivered=[],
                              outdoor=None, sleep_calls=0)
    monkeypatch.setattr(sm.nova_config, "runtime_get",
                        lambda hass, entry, key, default=None: w.settings.get(key, default))
    ef = load("entity_filter")
    monkeypatch.setattr(ef, "is_excluded", lambda hass, eid: eid in w.excluded)
    sd = load("sleep_detection")

    def _sleeping(hass, **kw):
        w.sleep_calls += 1
        w.sleep_kw = kw
        return w.sleeping, "test"
    monkeypatch.setattr(sd, "is_sleeping", _sleeping)
    hon = load("honorific")
    monkeypatch.setattr(hon, "effective_honorific", lambda hass: w.honorific)
    core = load("cognitive_core")
    monkeypatch.setattr(core, "discover_outdoor_temp", lambda hass: w.outdoor)
    monkeypatch.setattr(sm, "save_sentinel_event", lambda *a: w.saved.append(a))
    monkeypatch.setattr(sm, "save_message", lambda *a, **k: w.messages.append((a, k)))
    delivery = load("delivery")

    async def _deliver(hass, request):
        w.delivered.append(request)
    monkeypatch.setattr(delivery, "async_deliver", _deliver)
    monkeypatch.setattr(sm, "resolve_tts_entity", lambda hass, engine: "tts.cloud")
    ar = load("audio_routing")
    monkeypatch.setattr(ar, "broadcast_target",
                        lambda hass, broadcast_group=None, announcement_speakers=None:
                        list(announcement_speakers or []))
    return w


def _make(sm, hass=None, rules=None, entry=True):
    return sm.NovaSentinel(hass or FakeHass(), object(), "boot", rules=rules,
                           entry=types.SimpleNamespace() if entry else None)


def _event(eid, new, old="off", device_class=None):
    attrs = {"device_class": device_class} if device_class else {}
    return types.SimpleNamespace(data={
        "entity_id": eid,
        "new_state": None if new is None else types.SimpleNamespace(state=new, attributes=attrs),
        "old_state": None if old is None else types.SimpleNamespace(state=old)})


def _clock(sm, monkeypatch, start):
    now = [start]
    monkeypatch.setattr(sm.dt_util, "utcnow", lambda: now[0])
    return now


T0 = _dt.datetime(2026, 1, 1, 12, 0, tzinfo=_dt.timezone.utc)


# ── The rules ───────────────────────────────────────────────────────────────

def test_the_default_rules_are_unchanged(sm):
    msg = "{honorific}, {friendly_name} has been open for {minutes} minutes."
    assert sm.DEFAULT_RULES == [
        {"id": "door_left_open", "domain": "binary_sensor", "device_class": "door",
         "state": "on", "for_minutes": 10, "skip_if_outdoor_above_c": 10, "message": msg},
        {"id": "window_left_open", "domain": "binary_sensor", "device_class": "window",
         "state": "on", "for_minutes": 30, "skip_if_outdoor_above_c": 10, "message": msg},
        {"id": "garage_left_open", "domain": "binary_sensor", "device_class": "garage_door",
         "state": "on", "also": [{"domain": "cover", "device_class": "garage",
                                  "state": "open"}],
         "for_minutes": 15, "message": msg},
        {"id": "lock_unlocked", "domain": "lock", "state": "unlocked", "for_minutes": 20,
         "requires_security_lock": True,
         "message": "{honorific}, {friendly_name} has been unlocked for {minutes} minutes."},
    ]
    assert sm.SENTINEL_MODEL == "openai/gpt-oss-120b"


def test_variants_and_watched_states(sm):
    garage = sm.DEFAULT_RULES[2]
    assert sm._variants(garage) == [garage, garage["also"][0]]
    assert sm._variants({"also": ["junk", {"domain": "x"}]})[1:] == [{"domain": "x"}]
    assert sm._watched_states("cover.garage", garage) == {"open"}
    assert sm._watched_states("binary_sensor.g", garage) == {"on"}
    assert sm._watched_states("anything.x", {"state": "on"}) == {"on"}
    assert sm._watched_states("light.x", garage) == set()


# ── Which locks count ───────────────────────────────────────────────────────

def test_only_real_locks_are_watched(sm, reg):
    hass = FakeHass()
    reg.entries.update({
        "lock.config": _entry("lock.config", category="config"),
        "lock.thermostat": _entry("lock.thermostat", device_id="d1"),
        "lock.door": _entry("lock.door", device_id="d2"),
        "lock.plain": _entry("lock.plain"),
    })
    reg.devices.update({"d1": [_entry("climate.t"), _entry("lock.thermostat")],
                        "d2": [_entry("sensor.battery"), _entry("lock.door")]})
    assert sm._is_security_relevant_lock(hass, "lock.unknown") is True
    assert sm._is_security_relevant_lock(hass, "lock.config") is False
    assert sm._is_security_relevant_lock(hass, "lock.thermostat") is False
    assert sm._is_security_relevant_lock(hass, "lock.door") is True
    assert sm._is_security_relevant_lock(hass, "lock.plain") is True


def test_a_registry_error_counts_the_lock_as_real(sm, monkeypatch):
    def _boom(hass):
        raise RuntimeError("no registry")
    monkeypatch.setattr(sm.er, "async_get", _boom)
    assert sm._is_security_relevant_lock(FakeHass(), "lock.x") is True


# ── Which entities are watched ──────────────────────────────────────────────

def test_entities_are_collected_by_rule(sm, world, reg):
    hass = FakeHass()
    hass.states.async_set("binary_sensor.front", "off", {"device_class": "door"})
    hass.states.async_set("binary_sensor.back", "off", {"device_class": "door"})
    hass.states.async_set("binary_sensor.motion", "off", {"device_class": "motion"})
    hass.states.async_set("binary_sensor.bedroom", "off", {"device_class": "window"})
    hass.states.async_set("cover.garage", "closed", {"device_class": "garage"})
    hass.states.async_set("cover.blind", "closed", {"device_class": "blind"})
    hass.states.async_set("lock.front", "locked")
    hass.states.async_set("lock.keypad", "locked")
    reg.entries["lock.keypad"] = _entry("lock.keypad", category="config")
    world.excluded.add("binary_sensor.back")
    ids = _make(sm, hass)._collect_entity_ids()
    assert sorted(ids) == ["binary_sensor.bedroom", "binary_sensor.front", "cover.garage",
                           "lock.front"]


def test_an_explicit_entity_rule_is_watched_unless_excluded(sm, world):
    rules = [{"id": "a", "entity_id": "switch.pump", "state": "on"},
             {"id": "b", "entity_id": "switch.hidden", "state": "on"}]
    world.excluded.add("switch.hidden")
    assert _make(sm, rules=rules)._collect_entity_ids() == ["switch.pump"]


def test_matching_an_entity_to_a_rule(sm, reg):
    hass = FakeHass()
    hass.states.async_set("binary_sensor.front", "off", {"device_class": "door"})
    hass.states.async_set("binary_sensor.motion", "off", {"device_class": "motion"})
    hass.states.async_set("cover.garage", "closed", {"device_class": "garage"})
    s = _make(sm, hass)
    door, _, garage, lock = sm.DEFAULT_RULES
    assert s._entity_matches_rule("binary_sensor.front", door)
    assert not s._entity_matches_rule("binary_sensor.motion", door)
    assert s._entity_matches_rule("binary_sensor.gone", door)      # no state: matches
    assert s._entity_matches_rule("cover.garage", garage)
    assert not s._entity_matches_rule("light.x", door)
    assert not s._entity_matches_rule("switch.b", {"id": "x", "entity_id": "switch.a"})
    assert s._entity_matches_rule("switch.a", {"id": "x", "entity_id": "switch.a"})
    reg.entries["lock.keypad"] = _entry("lock.keypad", category="config")
    assert not s._entity_matches_rule("lock.keypad", lock)
    assert s._entity_matches_rule("lock.front", lock)


# ── Following state ─────────────────────────────────────────────────────────

def test_opening_starts_the_clock_once_and_closing_clears_it(sm, world, monkeypatch):
    hass = FakeHass()
    hass.states.async_set("binary_sensor.front", "on", {"device_class": "door"})
    s = _make(sm, hass)
    now = _clock(sm, monkeypatch, T0)
    s._handle_state_change(_event("binary_sensor.front", "on", device_class="door"))
    key = "binary_sensor.front:door_left_open"
    assert s._state_start[key] == T0.replace(tzinfo=None)
    now[0] = T0 + _dt.timedelta(minutes=3)
    s._handle_state_change(_event("binary_sensor.front", "on", "on", device_class="door"))
    assert s._state_start[key] == T0.replace(tzinfo=None)
    s._last_announced[key] = T0
    s._handle_state_change(_event("binary_sensor.front", "off", "on", device_class="door"))
    assert key not in s._state_start and key not in s._last_announced


def test_a_missing_new_state_is_ignored(sm, world):
    s = _make(sm)
    s._handle_state_change(_event("binary_sensor.front", None))
    assert s._state_start == {}


@pytest.mark.parametrize("old, announced", [("off", 1), ("unavailable", 0)])
async def test_an_instant_time_window_rule_fires_except_on_recovery(sm, world, monkeypatch,
                                                                  old, announced):
    hass = FakeHass()
    rule = {"id": "night", "domain": "lock", "state": "unlocked",
            "time_window": {"start": "00:00", "end": "23:59"}, "message": "x"}
    s = _make(sm, hass, rules=[rule])
    calls = []

    async def _announce(eid, r, minutes):
        calls.append((eid, r["id"], minutes))
    monkeypatch.setattr(s, "_announce_rule", _announce)
    _clock(sm, monkeypatch, T0)
    s._handle_state_change(_event("lock.front", "unlocked", old))
    await hass.drain()
    assert len(calls) == announced
    assert "lock.front:night" in s._state_start


@pytest.mark.parametrize("now, window, inside", [
    ("12:00", {"start": "09:00", "end": "17:00"}, True),
    ("08:59", {"start": "09:00", "end": "17:00"}, False),
    ("23:30", {"start": "22:00", "end": "06:00"}, True),
    ("05:00", {"start": "22:00", "end": "06:00"}, True),
    ("12:00", {"start": "22:00", "end": "06:00"}, False),
])
def test_time_windows_include_ones_across_midnight(sm, monkeypatch, now, window, inside):
    hour, minute = map(int, now.split(":"))

    class _Fixed(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, hour, minute)
    monkeypatch.setattr(sm, "datetime", _Fixed)
    assert _make(sm)._in_time_window(window) is inside


# ── The duration check ──────────────────────────────────────────────────────

def _armed(sm, monkeypatch, rules=None):
    hass = FakeHass()
    s = _make(sm, hass, rules=rules)
    s._entity_cache = ["binary_sensor.front"]
    fired = []

    async def _announce(eid, rule, minutes):
        fired.append((eid, rule["id"], minutes))
    monkeypatch.setattr(s, "_announce_rule", _announce)
    return s, hass, fired


async def _tick(s, hass):
    s._check_durations(None)
    await hass.drain()


async def test_a_door_is_announced_at_its_limit_then_with_the_true_total(sm, world,
                                                                       monkeypatch):
    s, hass, fired = _armed(sm, monkeypatch)
    now = _clock(sm, monkeypatch, T0)
    s._state_start["binary_sensor.front:door_left_open"] = T0.replace(tzinfo=None)
    now[0] = T0 + _dt.timedelta(minutes=9, seconds=59)
    await _tick(s, hass)
    assert fired == []
    now[0] = T0 + _dt.timedelta(minutes=10)
    await _tick(s, hass)
    now[0] = T0 + _dt.timedelta(minutes=15)
    await _tick(s, hass)
    now[0] = T0 + _dt.timedelta(minutes=20, seconds=30)
    await _tick(s, hass)
    assert fired == [("binary_sensor.front", "door_left_open", 10),
                     ("binary_sensor.front", "door_left_open", 20)]


async def test_mild_weather_skips_the_heat_loss_rules(sm, world, monkeypatch):
    s, hass, fired = _armed(sm, monkeypatch)
    _clock(sm, monkeypatch, T0 + _dt.timedelta(minutes=60))
    s._state_start["binary_sensor.front:door_left_open"] = T0.replace(tzinfo=None)
    world.outdoor = (52.0, "°F")          # 11.1 °C: above the 10 °C gate
    await _tick(s, hass)
    assert fired == []
    world.outdoor = (10.0, "°C")          # not above: checked
    await _tick(s, hass)
    assert len(fired) == 1


async def test_excluded_entities_and_rules_without_a_limit_are_skipped(sm, world,
                                                                      monkeypatch):
    rules = [{"id": "instant", "domain": "binary_sensor", "state": "on"},
             {"id": "long", "domain": "binary_sensor", "state": "on", "for_minutes": 1}]
    s, hass, fired = _armed(sm, monkeypatch, rules=rules)
    _clock(sm, monkeypatch, T0 + _dt.timedelta(minutes=5))
    for rid in ("instant", "long"):
        s._state_start[f"binary_sensor.front:{rid}"] = T0.replace(tzinfo=None)
    world.excluded.add("binary_sensor.front")
    await _tick(s, hass)
    assert fired == []
    world.excluded.clear()
    await _tick(s, hass)
    assert fired == [("binary_sensor.front", "long", 5)]


@pytest.mark.parametrize("found, celsius", [(None, None), ((50.0, "°F"), 10.0),
                                            ((7.5, "°C"), 7.5), ((7.5, None), 7.5)])
def test_outdoor_temperature_in_celsius(sm, world, found, celsius):
    world.outdoor = found
    assert _make(sm)._outdoor_temp_c() == celsius


# ── Saying it ───────────────────────────────────────────────────────────────

async def _say(sm, world, rule=None, entity="binary_sensor.front", minutes=12, hass=None):
    hass = hass or FakeHass()
    hass.states.async_set("binary_sensor.front", "on",
                          {"friendly_name": "Front door", "device_class": "door"})
    s = _make(sm, hass)
    await s._announce_rule(entity, rule or sm.DEFAULT_RULES[0], minutes)
    return s


async def test_an_alert_is_saved_said_and_delivered(sm, world):
    world.settings["announcement_speakers"] = ["media_player.kitchen"]
    await _say(sm, world)
    text = "sir, Front door has been open for 12 minutes."
    assert world.saved == [("binary_sensor.front", "door_left_open", text)]
    assert world.messages == [(("assistant", f"[Sentinel] {text}"), {"device_id": "sentinel"})]
    req = world.delivered[0]
    assert (req.source, req.message, req.entity_id, req.category) == (
        "sentinel", text, "binary_sensor.front", "door_left_open")
    assert req.speakers == ("media_player.kitchen",) and req.phone_only is False
    assert (req.tts_entity, req.tts_context, req.push, req.title) == (
        "tts.cloud", "sentinel", "always", "Nova")
    assert req.quiet_ok is False and req.sleep_ok is False
    assert (req.record_kind, req.record_facts, req.record_assessment) == (
        "left_open", {"rule": "door_left_open", "minutes": 12}, "left open past its limit")


async def test_with_no_speaker_it_goes_to_the_phones_only(sm, world):
    await _say(sm, world)
    assert world.delivered[0].speakers == () and world.delivered[0].phone_only is True


async def test_nobody_to_address_starts_the_sentence_with_a_capital(sm, world):
    world.honorific = ""
    hass = FakeHass()
    hass.states.async_set("binary_sensor.front", "on", {"friendly_name": "front door"})
    s = _make(sm, hass)
    await s._announce_rule("binary_sensor.front", sm.DEFAULT_RULES[0], 12)
    assert world.saved[0][2] == "Front door has been open for 12 minutes."


async def test_a_custom_message_is_formatted_as_written(sm, world):
    rule = {"id": "pump", "message": "{friendly_name} on {minutes}m, {honorific}"}
    await _say(sm, world, rule=rule)
    assert world.saved[0][2] == "Front door on 12m, sir"


async def test_an_unknown_entity_is_named_by_its_id(sm, world):
    await _say(sm, world, entity="binary_sensor.other")
    assert world.saved[0][2] == "sir, binary_sensor.other has been open for 12 minutes."


@pytest.mark.parametrize("settings", [
    {"announcements_enabled": False}, {"sentinel_enabled": False},
    {"disabled_sentinel_rules": json.dumps(["door_left_open"])},
    {"disabled_sentinel_rules": ["door_left_open"]},
])
async def test_switched_off_alerts_say_nothing(sm, world, settings):
    world.settings.update(settings)
    await _say(sm, world)
    assert world.saved == [] and world.delivered == []


async def test_a_broken_disabled_list_does_not_stop_the_alert(sm, world):
    world.settings["disabled_sentinel_rules"] = "{not json"
    await _say(sm, world)
    world.settings["disabled_sentinel_rules"] = 5
    await _say(sm, world)
    assert len(world.delivered) == 2


async def test_an_excluded_entity_says_nothing(sm, world):
    world.excluded.add("binary_sensor.front")
    await _say(sm, world)
    assert world.delivered == []


async def test_while_the_house_sleeps_it_is_saved_but_not_remembered_as_said(sm, world):
    world.sleeping = True
    world.settings.update(bedroom_areas=["bedroom"], observer_quiet_start="23:00",
                          observer_quiet_end="06:30")
    await _say(sm, world)
    assert world.saved and world.messages == []
    assert world.delivered[0].quiet_ok is False
    assert world.sleep_kw == {"bedroom_area_ids": ["bedroom"], "quiet_start": "23:00",
                              "quiet_end": "06:30"}


async def test_an_urgent_security_rule_skips_the_sleep_check(sm, world):
    world.sleeping = True
    rule = dict(sm.DEFAULT_RULES[0], urgent_security=True)
    await _say(sm, world, rule=rule)
    assert world.sleep_calls == 0 and world.messages
    assert world.delivered[0].quiet_ok is True and world.delivered[0].sleep_ok is True


async def test_a_failed_sleep_check_means_awake(sm, world, load, monkeypatch):
    def _boom(hass, **kw):
        raise RuntimeError("no sensors")
    monkeypatch.setattr(load("sleep_detection"), "is_sleeping", _boom)
    await _say(sm, world)
    assert world.messages


async def test_without_an_entry_the_switches_are_not_read(sm, world):
    world.settings["announcements_enabled"] = False
    hass = FakeHass()
    s = _make(sm, hass, entry=False)
    await s._announce_rule("binary_sensor.front", sm.DEFAULT_RULES[0], 12)
    assert world.delivered and world.delivered[0].tts_entity == ""
    assert s._speakers() == [] and s._tts_entity() is None


# ── A rule with no message asks the model ───────────────────────────────────

@pytest.fixture
def chat(load, monkeypatch):
    activity = load("providers.activity")
    seen = []
    reply = types.SimpleNamespace(value="  The door is open.  ", error=None)

    async def _chat(hass, client, messages, **kw):
        seen.append((messages, kw))
        if reply.error:
            raise reply.error
        return types.SimpleNamespace(text=reply.value)
    monkeypatch.setattr(activity, "execute_chat", _chat)
    ol = load("output_language")
    monkeypatch.setattr(ol, "with_language", lambda hass, system: system + " [lang]")
    reply.seen = seen
    return reply


async def test_a_model_line_is_used_trimmed(sm, world, chat):
    await _say(sm, world, rule={"id": "free", "state": "on"})
    assert world.saved[0][2] == "The door is open."
    messages, kw = chat.seen[0]
    assert messages[0]["role"] == "system" and messages[0]["content"].endswith(" [lang]")
    assert messages[1]["content"] == (
        "Generate one concise Nova alert (under 25 words) telling sir that 'Front door' "
        "has been 'on' for 12 minutes. Speak as Nova — calm, precise, one dry remark if "
        "warranted.")
    assert kw == {"role": "sentinel", "data_category": "text", "max_tokens": 60,
                  "temperature": 0.7}


async def test_with_nobody_to_address_the_model_is_told_the_household(sm, world, chat):
    world.honorific = ""
    await _say(sm, world, rule={"id": "free"})
    assert "telling the household that 'Front door' has been 'unknown'" in \
        chat.seen[0][0][1]["content"]


@pytest.mark.parametrize("honorific, text", [
    ("sir", "sir, Front door requires your attention."),
    ("", "Front door requires your attention.")])
async def test_a_model_failure_gives_a_plain_line(sm, world, chat, honorific, text):
    world.honorific = honorific
    chat.error = RuntimeError("down")
    await _say(sm, world, rule={"id": "free"})
    assert world.saved[0][2] == text


def test_the_honorific_falls_back_to_the_one_given_at_start(sm, load, monkeypatch):
    def _boom(hass):
        raise RuntimeError("no presence")
    monkeypatch.setattr(load("honorific"), "effective_honorific", _boom)
    assert _make(sm)._live_honorific() == "boot"


# ── Starting and stopping ───────────────────────────────────────────────────

async def test_start_watches_the_entities_and_two_timers_once(sm, world):
    hass = FakeHass()
    hass.states.async_set("binary_sensor.front", "off", {"device_class": "door"})
    s = _make(sm, hass)
    await s.async_start()
    await s.async_start()
    kinds = [(k, v) for k, v, _, _ in sm._ev.tracked]
    assert kinds == [("state", ["binary_sensor.front"]),
                     ("time", _dt.timedelta(seconds=60)), ("time", _dt.timedelta(minutes=5))]
    assert s._active is True
    await s.async_stop()
    assert s._active is False and s._unsubs == []
    assert sm._ev.tracked[0][3].calls == 1


async def test_with_nothing_to_watch_there_is_no_state_listener(sm, world):
    s = _make(sm)
    await s.async_start()
    assert [k for k, *_ in sm._ev.tracked] == ["time", "time"]


async def test_a_rescan_replaces_the_old_listener(sm, world):
    """A rescan that changes the list removes the old state listener before
    adding the new one, so a removed sensor stops being followed and no event
    is handled twice."""
    hass = FakeHass()
    hass.states.async_set("binary_sensor.front", "off", {"device_class": "door"})
    s = _make(sm, hass)
    await s.async_start()
    s._refresh_entities(None)
    assert len([t for t in sm._ev.tracked if t[0] == "state"]) == 1
    hass.states.async_set("binary_sensor.back", "off", {"device_class": "door"})
    s._refresh_entities(None)
    states = [t for t in sm._ev.tracked if t[0] == "state"]
    assert len(states) == 2 and sorted(states[1][1]) == ["binary_sensor.back",
                                                         "binary_sensor.front"]
    assert states[0][3].calls == 1                   # the old listener is gone
    assert len(s._unsubs) == 3
    await s.async_stop()
    assert states[0][3].calls == 1 and states[1][3].calls == 1


async def test_a_rescan_never_doubles_an_instant_alert(sm, world, monkeypatch):
    """With the old listener left behind, every later state change ran twice
    and an instant time window rule spoke twice."""
    rule = {"id": "night_door", "domain": "binary_sensor", "device_class": "door",
            "state": "on", "time_window": {"start": "00:00", "end": "23:59"},
            "message": "x"}
    hass = FakeHass()
    hass.states.async_set("binary_sensor.front", "off", {"device_class": "door"})
    s = sm.NovaSentinel(hass, None, "sir", rules=[rule])
    await s.async_start()
    hass.states.async_set("binary_sensor.back", "off", {"device_class": "door"})
    s._refresh_entities(None)
    announced = []

    async def _announce(entity_id, r, minutes):
        announced.append(entity_id)
    monkeypatch.setattr(s, "_announce_rule", _announce)
    new = types.SimpleNamespace(state="on", attributes={"device_class": "door"})
    event = types.SimpleNamespace(data={"entity_id": "binary_sensor.front", "new_state": new,
                                        "old_state": types.SimpleNamespace(state="off")})
    for t in sm._ev.tracked:
        if t[0] == "state" and t[3].calls == 0:
            t[2](event)
    await hass.drain()
    assert announced == ["binary_sensor.front"]
