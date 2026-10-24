"""Nova's state entities (8.70.0): the rules behind what they show.

Pins the Status order, timed shush (end, restart with a past end, the
watcher), the admin check on the suggestion services, the stored briefing
and the energy note when data is missing."""
import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ns(load, monkeypatch, tmp_path):
    mod = load("nova_state")
    monkeypatch.setattr(mod, "_briefing_path", lambda: str(tmp_path / "latest_briefing.json"))
    mod._BRIEFING.update(text=None, time=None)
    mod._HASS["hass"] = None
    yield mod
    mod._BRIEFING.update(text=None, time=None)
    mod._HASS["hass"] = None


@pytest.fixture
def og(load, monkeypatch, tmp_path):
    mod = load("output_gate")
    monkeypatch.setattr(mod, "_mutes_path", lambda: str(tmp_path / "output_mutes.json"))
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(mod, "_now", lambda: clock["now"])
    mod.clock = clock

    def reset():
        mod._STATE.muted_entities.clear()
        mod._STATE.muted_categories.clear()
        mod._STATE.until.clear()
        mod._STATE.mute_all = False
        mod._WATCH["reschedule"] = None
    reset()
    yield mod
    reset()


class _Hass:
    """Enough of Home Assistant for the watcher and the briefing store."""

    def __init__(self):
        self.fired, self.tasks = [], []
        self.bus = types.SimpleNamespace(
            async_fire=lambda event, data=None: self.fired.append((event, data)))

    def async_create_task(self, coro):
        self.tasks.append(coro)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


# ── Status order ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("flags, expected", [
    (dict(loaded=False, lockdown=True, napping=True, quiet=True, observer_on=False),
     "unavailable"),
    (dict(loaded=True, lockdown=True, napping=True, quiet=True, observer_on=False),
     "lockdown"),
    (dict(loaded=True, lockdown=False, napping=True, quiet=True, observer_on=False),
     "napping"),
    (dict(loaded=True, lockdown=False, napping=False, quiet=True, observer_on=False),
     "quiet"),
    (dict(loaded=True, lockdown=False, napping=False, quiet=False, observer_on=False),
     "observer_off"),
    (dict(loaded=True, lockdown=False, napping=False, quiet=False, observer_on=True),
     "ready"),
])
def test_status_picks_the_first_that_applies(ns, flags, expected):
    assert ns.pick_status(**flags) == expected


def test_quiet_attributes_follow_the_gate(ns, og):
    assert ns.quiet() == (False, {"mute_all": False, "muted_categories": [],
                                  "muted_entities": [], "until": None})
    og.shush(category="energy", minutes=10)
    on, attrs = ns.quiet()
    assert on and attrs["muted_categories"] == ["energy"]
    assert attrs["until"] == datetime.fromtimestamp(
        1_000_600.0, timezone.utc).isoformat()
    # A mute with no end means quiet has no end either.
    og.shush(entity_id="sensor.washer")
    assert ns.quiet()[1]["until"] is None


# ── Timed shush ─────────────────────────────────────────────────────────────

def test_shush_with_minutes_ends_on_time(og):
    og.shush(all=True, minutes=30)
    og.shush(entity_id="sensor.a", minutes=5)
    assert og.next_expiry() == 1_000_300.0
    assert og.expire_due()["cleared_entities"] == []
    og.clock["now"] += 300
    cleared = og.expire_due()
    assert cleared["cleared_entities"] == ["sensor.a"] and not cleared["blanket_cleared"]
    assert og._STATE.mute_all
    og.clock["now"] += 1500
    assert og.expire_due()["blanket_cleared"] is True
    assert not og._STATE.mute_all and og._STATE.until == {}


def test_shush_without_minutes_is_unchanged_and_drops_an_old_end(og):
    og.shush(category="lights", minutes=5)
    og.shush(category="lights")
    og.clock["now"] += 3600
    og.expire_due()
    assert og._STATE.muted_categories == {"lights"}


def test_unshush_forgets_the_end(og):
    og.shush(entity_id="sensor.a", minutes=5)
    og.unshush(entity_id="sensor.a")
    assert og._STATE.until == {}
    og.shush(all=True, minutes=5)
    og.unshush()
    assert og._STATE.until == {} and og.next_expiry() is None


def test_end_time_survives_a_restart_and_a_past_end_unshushes_on_start(og, monkeypatch):
    og.shush(category="presence", minutes=20)
    og._write_mutes(og._mutes_snapshot())
    og._STATE.muted_categories.clear()
    og._STATE.until.clear()
    og.load_mutes()
    assert og._STATE.until == {"category:presence": 1_001_200.0}

    # Home Assistant was down past the end: starting the watcher lifts it.
    og.clock["now"] += 7200
    later = []
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", types.SimpleNamespace(
        async_call_later=lambda hass, delay, action: later.append(delay) or (lambda: None)))
    hass = _Hass()
    stop = og.async_watch_expiry(hass)
    for task in hass.tasks:
        _run(task)
    assert og._STATE.muted_categories == set()
    assert hass.fired == [("nova_observer_unshushed", {
        "cleared_entities": [], "cleared_categories": ["presence"],
        "blanket_cleared": False})]
    assert later == []
    stop()
    assert og._WATCH["reschedule"] is None


def test_watcher_waits_for_the_next_end_and_picks_up_a_new_one(og, monkeypatch):
    calls, cancelled = [], []
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", types.SimpleNamespace(
        async_call_later=lambda hass, delay, action: calls.append(delay)
        or (lambda: cancelled.append(delay))))
    og.shush(entity_id="sensor.a", minutes=10)
    hass = _Hass()
    og.async_watch_expiry(hass)
    for task in hass.tasks:
        _run(task)
    assert calls == [600.0] and hass.fired == []
    og.shush(entity_id="sensor.b", minutes=1)
    og.reschedule_expiry()
    assert calls == [600.0, 60.0] and cancelled == [600.0]


def test_saved_mutes_ignore_a_bad_until(og):
    import json
    with open(og._mutes_path(), "w") as handle:
        json.dump({"entities": ["sensor.a"], "until": {"entity:sensor.a": "soon"}}, handle)
    og.load_mutes()
    assert og._STATE.muted_entities == {"sensor.a"} and og._STATE.until == {}


# ── Admin check on the suggestion services ──────────────────────────────────

class Unauthorized(Exception):
    def __init__(self, **kw):
        super().__init__("unauthorized")
        self.kw = kw


class UnknownUser(Unauthorized):
    pass


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", types.SimpleNamespace(
        Unauthorized=Unauthorized, UnknownUser=UnknownUser))
    users = {"admin": types.SimpleNamespace(is_admin=True),
             "plain": types.SimpleNamespace(is_admin=False)}

    async def get_user(user_id):
        return users.get(user_id)
    return types.SimpleNamespace(auth=types.SimpleNamespace(async_get_user=get_user))


def _call(user_id):
    return types.SimpleNamespace(context=types.SimpleNamespace(user_id=user_id),
                                 data={"suggestion_id": 3})


def test_admin_and_automations_may_call(ns, auth):
    _run(ns.async_check_admin(auth, _call("admin")))
    _run(ns.async_check_admin(auth, _call(None)))


def test_non_admin_is_refused(ns, auth):
    with pytest.raises(Unauthorized) as err:
        _run(ns.async_check_admin(auth, _call("plain")))
    assert type(err.value) is Unauthorized
    with pytest.raises(UnknownUser):
        _run(ns.async_check_admin(auth, _call("gone")))


# ── Latest briefing ─────────────────────────────────────────────────────────

def test_briefing_text_and_time_are_stored_and_reloaded(ns):
    assert ns.latest_briefing() == (None, None)
    hass = _Hass()
    _run(ns.async_record_briefing(hass, "Good morning. All quiet."))
    when, text = ns.latest_briefing()
    assert text == "Good morning. All quiet." and when.tzinfo is not None
    _run(ns.async_record_briefing(hass, "Second."))
    ns._BRIEFING.update(text=None, time=None)
    ns.load_briefing()
    assert ns.latest_briefing()[1] == "Second."
    # An empty briefing leaves the last one in place.
    _run(ns.async_record_briefing(hass, ""))
    assert ns.latest_briefing()[1] == "Second."


# ── Energy note ─────────────────────────────────────────────────────────────

def test_energy_sentence_needs_both_days(ns):
    assert ns.energy_sentence(None, 4.0) is None
    assert ns.energy_sentence(4.0, None) is None
    assert ns.energy_sentence(5.5, 5.0) == (
        "So far today the house has used 5.5 kWh, 10% more than by this time yesterday.")
    text = ns.energy_sentence(4.0, 4.1, 3.0, 2.0)
    assert text == ("So far today the house has used 4.0 kWh, about the same as by this "
                    "time yesterday. Solar has made 3.0 kWh, 50% more than yesterday.")
    assert len(text) <= 255
    assert "against 0.0 kWh" in ns.energy_sentence(1.0, 0.0)


def test_a_missing_hour_means_no_total(ns):
    start = datetime(2026, 10, 24, 0, tzinfo=timezone.utc)
    hours = ns.hours_between(start, start + timedelta(hours=3))
    assert len(hours) == 3
    full = {h: 1.0 for h in hours}
    assert ns.total_for(full, hours) == 3.0
    del full[hours[1]]
    assert ns.total_for(full, hours) is None
    assert ns.total_for(full, []) is None


def _energy_stubs(load, monkeypatch, prefs, changes, now):
    solar = load("solar")

    async def read_prefs(hass):
        return prefs
    monkeypatch.setattr(solar, "_read_prefs", read_prefs)
    eo = load("energy_outlook")

    async def hourly(hass, ids, start, end):
        return changes(ids)
    monkeypatch.setattr(eo, "_hourly_changes", hourly)
    monkeypatch.setattr(eo, "_now", lambda hass: (now, timezone.utc))


def test_energy_note_is_unknown_without_sources_or_with_missing_data(ns, load, monkeypatch):
    now = datetime(2026, 10, 24, 3, 20, tzinfo=timezone.utc)
    _energy_stubs(load, monkeypatch, None, lambda ids: {}, now)
    assert _run(ns.async_energy_note(object())) is None

    prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.import"}]}
    _energy_stubs(load, monkeypatch, prefs, lambda ids: {}, now)
    assert _run(ns.async_energy_note(object())) is None


def test_energy_note_compares_with_the_same_time_yesterday(ns, load, monkeypatch):
    now = datetime(2026, 10, 24, 3, 20, tzinfo=timezone.utc)
    midnight = now.replace(hour=0, minute=0)
    series = {midnight - timedelta(days=1) + timedelta(hours=h): 1.0 for h in range(3)}
    series.update({midnight + timedelta(hours=h): 0.5 for h in range(3)})
    prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.import"}]}
    _energy_stubs(load, monkeypatch, prefs, lambda ids: {i: series for i in ids}, now)
    assert _run(ns.async_energy_note(object())) == (
        "So far today the house has used 1.5 kWh, 50% less than by this time yesterday.")


# ── Readers ─────────────────────────────────────────────────────────────────

def test_notify_does_nothing_until_attached(ns, monkeypatch):
    sent = []
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.dispatcher", types.SimpleNamespace(
        dispatcher_send=lambda hass, signal: sent.append((hass, signal))))
    ns.notify()
    hass = object()
    ns.attach(hass)
    ns.notify()
    ns.detach(object())
    ns.notify()
    ns.detach(hass)
    ns.notify()
    assert sent == [(hass, "nova_state_changed")] * 2


def test_last_announcement_and_camera_summary(ns, og, load):
    og._STATE.history.clear()
    assert ns.last_announcement() == (None, None)
    og._STATE.history.append(og.Announcement(1_000_000.0, "e", "c", "low", "Hi", True))
    og._STATE.history.append(og.Announcement(1_000_100.0, "e", "c", "low", "held", False))
    when, message = ns.last_announcement()
    assert message == "Hi" and when.timestamp() == 1_000_000.0
    og._STATE.history.clear()

    pb = load("proactive_briefing")
    pb._SNAPSHOTS.clear()
    assert ns.camera_summary() == (None, None, [])
    for i in range(7):
        pb.record_snapshot(f"Cam {i}", f"camera.c{i}", f"<b>seen {i}</b>")
    when, summary, recent = ns.camera_summary()
    assert summary == "<b>seen 6</b>"
    assert [r["camera"] for r in recent] == ["Cam 6", "Cam 5", "Cam 4", "Cam 3", "Cam 2"]
    pb._SNAPSHOTS.clear()


def test_next_reminders_counts_all_and_shows_five(ns, monkeypatch):
    items = [{"label": f"r{i}", "at": f"2026-10-24T1{i}:00", "due": False,
              "repeat": "daily" if i == 0 else None} for i in range(7)]
    monkeypatch.setitem(sys.modules, "jc.reminders", types.SimpleNamespace(
        pending_reminders=lambda **kw: items))
    count, shown = ns.next_reminders()
    assert count == 7 and len(shown) == 5
    assert shown[0] == {"label": "r0", "time": "2026-10-24T10:00", "repeat": "daily"}
