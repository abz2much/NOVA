"""The on demand briefing (nova.briefing): what it gathers from the house, the
plain fallback, the grounding check, and how the finished briefing is saved
and delivered."""
import datetime as _dt
import sqlite3
import types

import pytest


@pytest.fixture
def br(load):
    return load("briefing")


class _Clock(_dt.datetime):
    hour_now = 9

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 6, cls.hour_now, 5, tzinfo=tz)


# ── greeting ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hour,expected", [
    (0, "Good morning"), (11, "Good morning"), (12, "Good afternoon"),
    (16, "Good afternoon"), (17, "Good evening"), (21, "Good evening"),
    (22, "Still awake"), (23, "Still awake"),
])
def test_greeting_follows_the_hour(br, monkeypatch, hour, expected):
    clock = type("C", (_Clock,), {"hour_now": hour})
    monkeypatch.setattr(br, "datetime", clock)
    assert br._time_greeting() == expected


# ── weather ─────────────────────────────────────────────────────────────────

def test_weather_reads_condition_temperature_and_today(br, fake_hass):
    fake_hass.states.set("weather.home", "cloudy", temperature=12, temperature_unit="°C",
                         forecast=[{"temperature": 15, "templow": 7}, {"temperature": 99}])
    assert br._gather_weather(fake_hass) == "cloudy, currently 12°C, high 15°C, low 7°C"


def test_weather_without_temperature_or_forecast(br, fake_hass):
    fake_hass.states.set("weather.home", "rainy")
    assert br._gather_weather(fake_hass) == "rainy"


def test_weather_default_unit_and_half_forecast(br, fake_hass):
    fake_hass.states.set("weather.home", "sunny", temperature=20,
                         forecast=[{"temperature": 24}])
    assert br._gather_weather(fake_hass) == "sunny, currently 20°"


def test_no_weather_entity_gives_nothing(br, fake_hass):
    assert br._gather_weather(fake_hass) == ""


# ── open things ─────────────────────────────────────────────────────────────

def test_open_doors_windows_and_garages(br, fake_hass):
    fake_hass.states.set("binary_sensor.a", "on", device_class="door", friendly_name="Back Door")
    fake_hass.states.set("binary_sensor.b", "on", device_class="window")
    fake_hass.states.set("binary_sensor.c", "on", device_class="garage_door",
                         friendly_name="Shed")
    fake_hass.states.set("binary_sensor.d", "off", device_class="door", friendly_name="Shut")
    fake_hass.states.set("binary_sensor.e", "on", device_class="motion", friendly_name="Hall")
    assert br._gather_open_things(fake_hass) == [
        "Back Door is open", "binary_sensor.b is open", "Shed is open"]


def test_unlocked_locks_skip_the_exempt_ones(br, load, fake_hass, monkeypatch, tmp_path):
    cc = load("cognitive_core")
    monkeypatch.setattr(cc, "LOCKDOWN_STATE_PATH", str(tmp_path / "lockdown.json"))
    monkeypatch.setattr(cc._CORE, "lockdown_mgr", cc.LockdownManager(
        fake_hass, {"lockdown_exempt_locks": ["lock.panel"]}))
    fake_hass.states.set("lock.panel", "unlocked", friendly_name="Panel Lock")
    fake_hass.states.set("lock.side", "unlocked", friendly_name="Side")
    fake_hass.states.set("lock.shut", "locked", friendly_name="Shut")
    assert br._gather_open_things(fake_hass) == ["Side is unlocked"]


def test_unlocked_lock_without_a_name_uses_its_id(br, fake_hass):
    fake_hass.states.set("lock.side", "unlocked")
    assert br._gather_open_things(fake_hass) == ["lock.side is unlocked"]


# ── calendar ────────────────────────────────────────────────────────────────

def test_calendar_lists_only_active_events_with_a_start(br, fake_hass):
    fake_hass.states.set("calendar.a", "on", message="Dentist",
                         start_time="2026-10-06 09:30:00")
    fake_hass.states.set("calendar.b", "off", message="Later",
                         start_time="2026-10-06 18:00:00")
    fake_hass.states.set("calendar.c", "on", message="No time")
    assert br._gather_calendar(fake_hass) == ["Dentist at 09:30"]


# ── energy ──────────────────────────────────────────────────────────────────

def test_energy_lists_power_draw_over_500(br, fake_hass):
    fake_hass.states.set("sensor.oven", "2400.4", device_class="power", friendly_name="Oven",
                         unit_of_measurement="W")
    fake_hass.states.set("sensor.kettle", "1.2", device_class="power",
                         unit_of_measurement="kW")
    fake_hass.states.set("sensor.edge", "500", device_class="power")
    fake_hass.states.set("sensor.heater", "800", device_class="power")
    fake_hass.states.set("sensor.bad", "unavailable", device_class="power")
    fake_hass.states.set("sensor.temp", "900", device_class="temperature")
    assert br._gather_energy_anomalies(fake_hass) == [
        "Oven is drawing 2400W", "sensor.heater is drawing 800W"]


# ── overnight events ────────────────────────────────────────────────────────

def _events_db(path, rows):
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE sentinel_events "
                     "(timestamp TEXT, entity_id TEXT, event_type TEXT, detail TEXT)")
        conn.executemany("INSERT INTO sentinel_events VALUES (?,?,?,?)", rows)


def test_overnight_events_newest_first_in_local_time(br, fake_hass, tmp_path, monkeypatch):
    db = tmp_path / "c.db"
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0, tzinfo=None)
    older = now - _dt.timedelta(hours=2)
    stale = now - _dt.timedelta(hours=30)
    _events_db(db, [(older.isoformat(), "x", "open", "Older"),
                    (now.isoformat(), "x", "open", "Newer"),
                    (stale.isoformat(), "x", "open", "Stale")])
    monkeypatch.setattr(br, "CONVERSATIONS_DB", str(db))

    def local(t):
        return t.replace(tzinfo=_dt.timezone.utc).astimezone().strftime("%H:%M")
    assert br._gather_overnight_events(fake_hass, 12) == [
        f"{local(now)}: Newer", f"{local(older)}: Older"]


def test_overnight_events_cap_at_ten(br, fake_hass, tmp_path, monkeypatch):
    db = tmp_path / "c.db"
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    _events_db(db, [((now - _dt.timedelta(minutes=i)).isoformat(), "x", "e", f"E{i}")
                    for i in range(15)])
    monkeypatch.setattr(br, "CONVERSATIONS_DB", str(db))
    assert len(br._gather_overnight_events(fake_hass)) == 10


def test_unparseable_timestamp_keeps_its_clock_text(br, fake_hass, tmp_path, monkeypatch):
    db = tmp_path / "c.db"
    _events_db(db, [("9999-99-99T23:59:00 bad", "x", "e", "Odd")])
    monkeypatch.setattr(br, "CONVERSATIONS_DB", str(db))
    assert br._gather_overnight_events(fake_hass) == ["23:59: Odd"]


def test_missing_database_or_table_gives_nothing(br, fake_hass, tmp_path, monkeypatch):
    monkeypatch.setattr(br, "CONVERSATIONS_DB", str(tmp_path / "absent.db"))
    assert br._gather_overnight_events(fake_hass) == []
    assert not (tmp_path / "absent.db").exists()
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    monkeypatch.setattr(br, "CONVERSATIONS_DB", str(empty))
    assert br._gather_overnight_events(fake_hass) == []


def test_database_path_defaults_to_paths(br, load, monkeypatch):
    monkeypatch.setattr(br, "CONVERSATIONS_DB", None)
    monkeypatch.setattr(load("paths"), "conversations_db", lambda: "/x/c.db")
    assert br._conversations_db() == "/x/c.db"
    monkeypatch.setattr(br, "CONVERSATIONS_DB", "/y/o.db")
    assert br._conversations_db() == "/y/o.db"


# ── plain fallback ──────────────────────────────────────────────────────────

def test_plain_briefing_reads_the_facts(br):
    assert br._plain_briefing("Good morning", "sir", ["It is Monday.", " ", "", "Rain."]) == (
        "Good morning, sir. It is Monday. Rain.")


def test_plain_briefing_without_facts_or_honorific(br):
    assert br._plain_briefing("Good evening", "", []) == "Good evening. Nothing notable to report."
    assert br._plain_briefing("Good evening", "sir", None) == (
        "Good evening, sir. Nothing notable to report.")


# ── grounding ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("said,context,expected", [
    ("The garage is open.", "Weather: dry.", "garage"),
    ("The gates are shut.", "Weather: dry.", "gate"),
    ("A door is open.", "Weather: dry.", "door"),
    ("Windows are open.", "Weather: dry.", "window"),
    ("Side is unlocked.", "Side Door is open.", "lock"),
    ("Heating is on.", "Weather: dry.", "thermostat"),
    ("Heating is on.", "climate.hall is heat.", ""),
    ("Garage door open.", "Garage Door is open.", ""),
    ("The Gateway is fine.", "Weather: dry.", ""),
    ("", "", ""),
    (None, None, ""),
])
def test_device_words_need_context(br, said, context, expected):
    assert br.ungrounded_briefing_fact(said, context) == expected


def test_clock_times_compare_on_a_twelve_hour_clock(br):
    ctx = "It is 4:37 PM. Events: 13:29: Door opened."
    assert br.ungrounded_briefing_fact("At 1:29 and 16:37.", ctx) == ""
    assert br.ungrounded_briefing_fact("At 2:14 and 0:05.", ctx) == "time 12:05"
    assert br.ungrounded_briefing_fact("At 12:30.", ctx) == "time 12:30"
    # not a clock time, so not checked
    assert br.ungrounded_briefing_fact("Score 25:61.", ctx) == ""


def test_clock_times_helper(br):
    assert br._clock_times("13:29 and 1:29, 99:00, 7:75") == {(1, 29)}


# ── hazards ─────────────────────────────────────────────────────────────────

def test_hazard_lines_for_every_feed(br):
    hz = {"ok": True,
          "earthquakes": [{"mag": 4, "dist_km": 12, "place": "Sea"},
                          {"mag": None, "dist_km": 30, "place": "Hills"}],
          "weather": [{"severity": "Severe", "event": "Gale", "area": "Coast"},
                      {"severity": "Minor", "event": "Fog"}],
          "disasters": [{"category": "Wildfire", "title": "Ridge", "dist_km": 50}]}
    assert br._hazard_context(hz) == [
        "Active hazards nearby: magnitude 4.0 quake 12 km away (Sea); "
        "an earthquake quake 30 km away (Hills); Severe weather alert: Gale for Coast; "
        "Minor weather alert: Fog; Wildfire: Ridge 50 km away."]


def test_hazard_warnings_without_area_or_window(br):
    hz = {"ok": False, "warnings": [
        {"level": "orange", "type": "Snow", "source_label": "Office", "headline_text": "H",
         "from": "Mon"}]}
    assert br._hazard_context(hz) == [
        "Weather warnings in force:\n- Office Orange Snow warning for your area. "
        "Headline (quote exactly): H"]


def test_quiet_hazard_scan_adds_nothing(br):
    assert br._hazard_context({"ok": True, "earthquakes": None, "warnings": None}) == []


# ── the service ─────────────────────────────────────────────────────────────

@pytest.fixture
def run(br, load, monkeypatch, fake_hass):
    """Run async_briefing with the house gatherers stubbed and the model
    replying with `reply` (or raising)."""
    _Clock.hour_now = 9
    monkeypatch.setattr(br, "datetime", _Clock)
    saved, chats, delivered = [], [], []
    monkeypatch.setattr(br, "save_message", lambda *a: saved.append(a))
    monkeypatch.setattr(br, "build_system_prompt", lambda hass, hon, task: f"SYS[{task}]")
    monkeypatch.setattr(br, "get_presence_summary", lambda hass: {"people": [
        {"name": "Ann", "state": "home"}, {"name": "Bo", "state": "work"}]})
    monkeypatch.setattr(br, "_gather_weather", lambda hass: "dry, currently 9°C")
    monkeypatch.setattr(br, "_gather_overnight_events", lambda hass, h: ["01:00: Door opened"])
    monkeypatch.setattr(br, "_gather_open_things", lambda hass: ["Side is unlocked"])
    monkeypatch.setattr(br, "_gather_calendar", lambda hass: ["Dentist at 09:30"])
    monkeypatch.setattr(br, "_gather_energy_anomalies", lambda hass: ["Oven is drawing 900W"])

    async def scan(hass):
        return {"ok": True, "earthquakes": [{"mag": 3.0, "dist_km": 5, "place": "P"}]}
    monkeypatch.setattr(load("hazard_monitor"), "scan_now", scan)
    monkeypatch.setattr(load("proactive_briefing"), "get_snapshot_summary",
                        lambda hours: f"Camera: 2 people in {hours}h.")
    monkeypatch.setattr(load("output_language"), "with_language",
                        lambda hass, system: system + "|lang")

    async def deliver(hass, req):
        delivered.append(req)
        return types.SimpleNamespace(spoken=True)
    monkeypatch.setattr(load("delivery"), "async_deliver", deliver)

    async def _run(data=None, reply="Good morning, sir. Side is unlocked.", honorific="sir",
                   tts="tts.x", speakers=("media_player.a",)):
        async def chat(hass, client, messages, **kw):
            chats.append((messages, kw))
            if isinstance(reply, BaseException):
                raise reply
            return types.SimpleNamespace(text=reply)
        monkeypatch.setattr(load("providers.activity"), "execute_chat", chat)
        call = types.SimpleNamespace(data=dict(data or {}))
        out = await br.async_briefing(fake_hass, call, "client", honorific, tts, list(speakers))
        return out, saved, chats, delivered
    return _run


async def test_full_context_in_order(run):
    out, saved, chats, delivered = await run()
    assert out["context"].split("\n") == [
        "It is " + out["context"].split("\n")[0][6:],
        "Weather: dry, currently 9°C.",
        "At home: Ann.",
        "Away: Bo.",
        "Events in the last 12 hours:",
        "- 01:00: Door opened",
        "Currently open/unlocked: Side is unlocked.",
        "Calendar: Dentist at 09:30.",
        "High power draw: Oven is drawing 900W.",
        "Active hazards nearby: magnitude 3.0 quake 5 km away (P).",
        "Camera: 2 people in 12h.",
    ]
    assert out["context"].startswith("It is ") and ", 9:05 AM." in out["context"].split("\n")[0]
    assert out == {"success": True, "briefing": "Good morning, sir. Side is unlocked.",
                   "context": out["context"], "announced": True}


async def test_model_call_shape(run, load):
    out, _, chats, _ = await run()
    (messages, kw), = chats
    assert messages[1] == {"role": "user", "content": "Context:\n" + out["context"]}
    system = messages[0]["content"]
    assert messages[0]["role"] == "system"
    assert system.endswith("|lang") and system.startswith("SYS[")
    assert "spoken briefing to sir. Begin with 'Good morning, sir.'" in system
    assert "Under 120 words." in system
    assert load("const").BRIEFING_GROUNDING_RULE in system
    assert kw == {"role": "briefing", "data_category": "text",
                  "max_tokens": 1500, "temperature": 0.2}


async def test_no_honorific_addresses_the_household(run):
    _, _, chats, _ = await run(honorific="")
    system = chats[0][0][0]["content"]
    assert "spoken briefing to the household. Begin with 'Good morning.'" in system


async def test_every_section_can_be_turned_off(run):
    out, *_ = await run(data={
        "include_weather": False, "include_calendar": False, "include_presence": False,
        "include_events": False, "include_energy": False, "include_hazards": False})
    # open things are always included; the camera summary has no switch
    assert out["context"].split("\n")[1:] == [
        "Currently open/unlocked: Side is unlocked.", "Camera: 2 people in 12h."]


async def test_hours_reach_events_and_camera(run, br, monkeypatch):
    seen = []
    monkeypatch.setattr(br, "_gather_overnight_events", lambda hass, h: seen.append(h) or [])
    out, *_ = await run(data={"hours": "6"})
    assert seen == [6]
    assert "Camera: 2 people in 6h." in out["context"]


async def test_empty_sections_add_no_lines(run, br, load, monkeypatch):
    for name in ("_gather_overnight_events",):
        monkeypatch.setattr(br, name, lambda hass, h: [])
    for name in ("_gather_open_things", "_gather_calendar", "_gather_energy_anomalies"):
        monkeypatch.setattr(br, name, lambda hass: [])
    monkeypatch.setattr(br, "_gather_weather", lambda hass: "")
    monkeypatch.setattr(br, "get_presence_summary", lambda hass: {"people": []})
    monkeypatch.setattr(load("proactive_briefing"), "get_snapshot_summary", lambda hours: "")

    async def quiet(hass):
        return {"ok": True}
    monkeypatch.setattr(load("hazard_monitor"), "scan_now", quiet)
    out, *_ = await run()
    assert len(out["context"].split("\n")) == 1


async def test_only_away_people(run, br, monkeypatch):
    monkeypatch.setattr(br, "get_presence_summary", lambda hass: {"people": [
        {"name": "Bo", "state": "not_home"}]})
    out, *_ = await run()
    assert "Away: Bo." in out["context"] and "At home" not in out["context"]


async def test_failing_hazard_scan_and_camera_summary_are_skipped(run, load, monkeypatch):
    async def boom(hass):
        raise RuntimeError("feed down")
    monkeypatch.setattr(load("hazard_monitor"), "scan_now", boom)

    def cam(hours):
        raise RuntimeError("no snapshots")
    monkeypatch.setattr(load("proactive_briefing"), "get_snapshot_summary", cam)
    out, *_ = await run()
    assert "hazards" not in out["context"] and "Camera" not in out["context"]
    assert out["success"] is True


@pytest.mark.parametrize("reply", ["", "   ", None, RuntimeError("model down")])
async def test_empty_or_failed_model_reads_the_facts(run, reply):
    out, saved, _, delivered = await run(reply=reply)
    assert out["briefing"].startswith("Good morning, sir. It is ")
    assert "Weather: dry, currently 9°C." in out["briefing"]
    assert saved == [("assistant", f"[Briefing] {out['briefing']}", "briefing")]
    assert delivered[0].message == out["briefing"]


async def test_ungrounded_reply_is_replaced_whole(run):
    out, *_ = await run(reply="Good morning, sir. The garage is open.")
    assert "garage" not in out["briefing"]
    assert out["briefing"].startswith("Good morning, sir. It is ")


async def test_reply_is_trimmed_and_saved(run):
    out, saved, *_ = await run(reply="  Good morning, sir. Side is unlocked.\n")
    assert out["briefing"] == "Good morning, sir. Side is unlocked."
    assert saved == [("assistant", "[Briefing] Good morning, sir. Side is unlocked.",
                      "briefing")]


async def test_delivery_request(run):
    out, _, _, delivered = await run()
    (req,) = delivered
    assert (req.source, req.category, req.requested_by_user, req.quiet_ok) == (
        "briefing", "briefing_service", True, True)
    assert req.message == out["briefing"]
    assert req.speakers == ("media_player.a",)
    assert (req.tts_entity, req.tts_context, req.title) == ("tts.x", "briefing",
                                                            "Nova — Briefing")


async def test_no_speakers_still_delivers(run):
    out, _, _, delivered = await run(speakers=())
    assert delivered[0].speakers == ()
    assert out["announced"] is True


async def test_announce_false_or_no_tts_skips_delivery(run):
    out, saved, _, delivered = await run(data={"announce": False})
    assert delivered == [] and out["announced"] is False and saved
    out, _, _, delivered = await run(tts=None)
    assert delivered == [] and out["announced"] is False


async def test_announced_follows_delivery(run, load, monkeypatch):
    async def unspoken(hass, req):
        return types.SimpleNamespace(spoken=False)
    monkeypatch.setattr(load("delivery"), "async_deliver", unspoken)
    out, *_ = await run()
    assert out["announced"] is False


def test_model_constant(br):
    assert br.BRIEFING_MODEL == "openai/gpt-oss-120b"
