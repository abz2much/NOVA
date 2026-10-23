"""intrusion.py: calling an alert off, acknowledging it, and the snapshots.

Safety relevant: a call-off must really hold escalation for its cooldown and
then let go; an acknowledgement holds only the no answer escalation and is
not a call-off; snapshots stay in the private folder and are never served
from an unauthenticated path. Learning and the event log are covered in
test_intrusion_spec.py and test_intrusion_log.py.
"""
from __future__ import annotations

import base64
import pathlib
import sys
import time
import types
from datetime import timedelta

import pytest

T0 = 1_700_000_000.0
JPEG = b"\xff\xd8\xff\xe0 fake frame"


@pytest.fixture
def clock():
    return {"now": T0}


@pytest.fixture
def judged(monkeypatch):
    """Decision Record verdicts asked for by a call-off, kept in a list
    instead of being written anywhere."""
    calls: list = []
    fake = types.ModuleType("jc.decision_record")
    fake.set_outcome = lambda rid, verdict, source="": calls.append((rid, verdict, source)) or True
    fake.set_outcome_recent = lambda *a, **k: calls.append(("recent",) + a) or True
    monkeypatch.setitem(sys.modules, "jc.decision_record", fake)
    monkeypatch.setattr(sys.modules["jc"], "decision_record", fake, raising=False)
    return calls


@pytest.fixture
def intr(load, tmp_path, monkeypatch, clock, judged):
    """intrusion.py on a clock the test moves, with every piece of module
    state fresh and every folder inside tmp_path."""
    mod = load("intrusion")
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(
        time=lambda: clock["now"], localtime=time.localtime))
    monkeypatch.setattr(mod, "SNAPSHOT_DIR", str(tmp_path / "private"))
    monkeypatch.setattr(mod, "_LEGACY_SNAPSHOT_DIR", str(tmp_path / "public"))
    monkeypatch.setattr(mod, "LOG_PATH", tmp_path / "events.json")
    fresh = {"_called_off_until": 0.0, "_acknowledged_ts": 0.0, "_last_snapshot": {},
             "_false_alarms": [], "_log": [], "_log_loaded": False,
             "_last_decision_id": None}
    for name, value in fresh.items():
        monkeypatch.setattr(mod, name, value)
    return mod


def private_dir(intr) -> pathlib.Path:
    return pathlib.Path(intr.SNAPSHOT_DIR)


def install(monkeypatch, name: str, **attrs):
    """Stand in for a Home Assistant module for one test only."""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


# ── a fresh start ───────────────────────────────────────────────────────────

def test_nothing_is_held_at_start(intr):
    assert intr.is_called_off() is False
    assert intr.is_acknowledged() is False
    assert intr.false_alarm_count() == 0
    assert intr.last_snapshot() is None
    st = intr.status()
    assert {"last_snapshot", "called_off", "acknowledged", "suppressed_for",
            "false_alarms_24h"} <= set(st)
    assert (st["called_off"], st["acknowledged"], st["suppressed_for"]) == (False, False, 0)


# ── calling an alert off ────────────────────────────────────────────────────

def test_a_call_off_holds_escalation_and_says_for_how_long(intr):
    reply = intr.dismiss_intrusion("the cat")
    assert reply["ok"] is True
    assert reply["suppressed_seconds"] == int(intr._CALLOFF_COOLDOWN) > 0
    assert reply["recorded"]["reason"] == "the cat"
    assert intr.is_called_off() is True
    st = intr.status()
    assert st["called_off"] is True
    assert st["suppressed_for"] == int(intr._CALLOFF_COOLDOWN)


@pytest.mark.parametrize("seconds_from_end, held", [
    (-599.0, True),   # just after the call-off
    (-1.0, True),     # one second before the cooldown ends
    (0.0, False),     # exactly at the end: released
    (5.0, False),
])
def test_a_call_off_ends_with_its_cooldown(intr, clock, seconds_from_end, held):
    intr.dismiss_intrusion()
    clock["now"] = T0 + intr._CALLOFF_COOLDOWN + seconds_from_end
    assert intr.is_called_off() is held


def test_clearing_lifts_a_call_off(intr):
    intr.dismiss_intrusion()
    assert intr.is_called_off() is True
    intr.clear_calloff()
    assert intr.is_called_off() is False


def test_a_call_off_judges_the_alert_it_belongs_to(intr, judged):
    intr.set_last_decision_id(42)
    intr.dismiss_intrusion()
    assert judged[0] == (42, "wrong", "dismiss_intrusion")


# ── false alarm memory ──────────────────────────────────────────────────────

def test_each_call_off_is_remembered_as_a_false_alarm(intr):
    intr.dismiss_intrusion("cat again")
    assert intr.false_alarm_count() == 1
    assert intr._false_alarms[-1]["reason"] == "cat again"
    assert intr._false_alarms[-1]["ts"] == int(T0)


def test_only_false_alarms_inside_the_window_are_counted(intr, clock):
    intr.dismiss_intrusion("long ago")
    clock["now"] += 200_000
    intr.dismiss_intrusion("today")
    assert intr.false_alarm_count(within_seconds=86_400) == 1
    assert intr.false_alarm_count(within_seconds=300_000) == 2
    assert intr.status()["false_alarms_24h"] == 1


def test_the_false_alarm_list_is_capped_keeping_the_newest(intr, clock):
    for n in range(60):
        clock["now"] = T0 + n
        intr.dismiss_intrusion(f"n{n}")
    assert len(intr._false_alarms) == 50
    assert intr._false_alarms[0]["reason"] == "n10"
    assert intr._false_alarms[-1]["reason"] == "n59"


# ── acknowledging ───────────────────────────────────────────────────────────

def test_an_acknowledgement_holds_without_calling_off(intr):
    reply = intr.acknowledge("I'm looking")
    assert reply["ok"] is True
    assert reply["held_seconds"] == int(intr._ACK_WINDOW)
    assert intr.is_acknowledged() is True
    # Fresh evidence must still be able to escalate: not a false alarm.
    assert intr.is_called_off() is False
    assert intr.false_alarm_count() == 0
    assert intr.status()["acknowledged"] is True


@pytest.mark.parametrize("offset, held", [(-1.0, True), (0.0, False), (5.0, False)])
def test_an_acknowledgement_runs_out(intr, clock, offset, held):
    intr.acknowledge()
    clock["now"] = T0 + intr._ACK_WINDOW + offset
    assert intr.is_acknowledged() is held


def test_clearing_lifts_an_acknowledgement_too(intr):
    intr.acknowledge()
    intr.dismiss_intrusion()
    intr.clear_calloff()
    assert intr.is_acknowledged() is False
    assert intr.is_called_off() is False


# ── the agent can do both ───────────────────────────────────────────────────

@pytest.mark.parametrize("tool", ["dismiss_intrusion", "acknowledge_alert"])
def test_the_agent_offers_the_tool(load, tool):
    agent = load("agent")
    offered = {spec["function"]["name"] for spec in agent.NOVA_TOOLS}
    assert tool in offered
    assert tool in agent._TOOL_MAP


# ── capturing a snapshot ────────────────────────────────────────────────────

async def test_a_capture_is_saved_privately_and_has_no_public_url(
        intr, fake_hass, monkeypatch):
    asked = []

    async def grab(hass, entity, timeout=10):
        asked.append((entity, timeout))
        return types.SimpleNamespace(content=JPEG)
    install(monkeypatch, "homeassistant.components.camera", async_get_image=grab)

    info = await intr.capture_snapshot(fake_hass, "camera.dining_room")

    assert asked == [("camera.dining_room", 10)]
    assert info["camera"] == "camera.dining_room"
    assert info["ts"] == int(T0)
    # A /local/ address is served with no login at all; snapshots reach the
    # panel only through the admin websocket, so no URL is ever made here.
    assert "url" not in info
    saved = pathlib.Path(info["path"])
    assert saved.parent == private_dir(intr)
    assert saved.name == f"intrusion_dining_room_{int(T0)}.jpg"
    assert saved.read_bytes() == JPEG
    assert intr.last_snapshot()["camera"] == "camera.dining_room"
    assert intr.status()["last_snapshot"] == info


def test_the_real_snapshot_folder_is_never_under_www(load):
    """Checked against the module's own default, not a test's redirected
    folder, so it cannot pass by accident: Home Assistant serves the whole of
    /config/www at /local/ without authentication."""
    mod = load("intrusion")
    assert "/www/" not in mod._snapshot_dir()
    source = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    assert 'paths.nova_path("intrusion")' in source


async def test_no_camera_means_no_capture(intr, fake_hass):
    assert await intr.capture_snapshot(fake_hass, "") is None
    assert intr.last_snapshot() is None


async def test_a_camera_failure_is_swallowed(intr, fake_hass, monkeypatch):
    async def offline(hass, entity, timeout=10):
        raise RuntimeError("camera offline")
    install(monkeypatch, "homeassistant.components.camera", async_get_image=offline)
    assert await intr.capture_snapshot(fake_hass, "camera.den") is None
    assert intr.last_snapshot() is None
    assert not private_dir(intr).exists()


# ── reading a snapshot back for the panel ───────────────────────────────────

async def test_a_saved_snapshot_reads_back_as_base64(intr, fake_hass):
    private_dir(intr).mkdir(parents=True)
    still = private_dir(intr) / "x.jpg"
    still.write_bytes(JPEG)
    encoded = await intr.get_snapshot_b64(fake_hass, str(still))
    assert base64.b64decode(encoded) == JPEG


async def test_a_missing_snapshot_reads_as_none(intr, fake_hass):
    private_dir(intr).mkdir(parents=True)
    assert await intr.get_snapshot_b64(fake_hass, str(private_dir(intr) / "gone.jpg")) is None


@pytest.mark.parametrize("where", ["beside", "climb_out"])
async def test_nothing_outside_the_private_folder_is_ever_read(intr, fake_hass, tmp_path, where):
    """Defence in depth: today every path comes from Nova's own records, but a
    future caller passing something untrusted must still be refused, whether
    it names another file outright or climbs out with '..'."""
    private_dir(intr).mkdir(parents=True)
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"not yours")
    path = str(secret) if where == "beside" else str(private_dir(intr) / ".." / "secret.jpg")
    assert await intr.get_snapshot_b64(fake_hass, path) is None


# ── moving old public snapshots ─────────────────────────────────────────────

def _old_still(intr, name: str, data: bytes) -> pathlib.Path:
    public = pathlib.Path(intr._LEGACY_SNAPSHOT_DIR)
    public.mkdir(parents=True, exist_ok=True)
    still = public / name
    still.write_bytes(data)
    return still


def test_old_snapshots_move_and_the_log_follows_them(intr):
    old = _old_still(intr, "a.jpg", b"one")
    intr._log = [{"id": "evt_1", "snapshot_path": str(old)}]
    intr._log_loaded = True

    assert intr.migrate_legacy_snapshots() == {"moved": 1, "skipped": 0}

    new = private_dir(intr) / "a.jpg"
    assert new.read_bytes() == b"one"
    assert not old.exists()                          # moved, not duplicated
    assert intr._log[0]["snapshot_path"] == str(new)


def test_with_no_old_folder_nothing_happens(intr):
    assert intr.migrate_legacy_snapshots() == {"moved": 0, "skipped": 0}
    assert not private_dir(intr).exists()


def test_a_name_clash_leaves_both_files_untouched(intr):
    old = _old_still(intr, "a.jpg", b"legacy-bytes")
    private_dir(intr).mkdir(parents=True)
    (private_dir(intr) / "a.jpg").write_bytes(b"already-here")

    assert intr.migrate_legacy_snapshots() == {"moved": 0, "skipped": 1}

    assert old.read_bytes() == b"legacy-bytes"       # not deleted
    assert (private_dir(intr) / "a.jpg").read_bytes() == b"already-here"   # not overwritten


# ── the picture on a phone notification ─────────────────────────────────────

async def test_without_a_media_folder_there_is_no_picture(intr, fake_hass):
    fake_hass.config.media_dirs = {}
    assert await intr.get_notification_image_url(fake_hass, "/some/path.jpg") is None


async def test_without_a_snapshot_there_is_no_picture(intr, fake_hass):
    assert await intr.get_notification_image_url(fake_hass, "") is None


async def test_the_picture_is_a_signed_short_lived_copy(intr, fake_hass, tmp_path, monkeypatch):
    """The private still is duplicated into the media folder under a name nobody
    can guess, the /media/ path is signed (never handed out unsigned), and a
    timer is set to delete the copy rather than leave it there."""
    media = tmp_path / "media"
    media.mkdir()
    source = tmp_path / "source.jpg"
    source.write_bytes(JPEG)
    fake_hass.config.media_dirs = {"local": str(media)}
    signed: list = []
    timers: list = []

    def sign(hass, path, expiry):
        signed.append((path, expiry))
        return f"{path}?authSig=test-token"

    def later(hass, delay, callback):
        timers.append((delay, callback))
        return lambda: None
    install(monkeypatch, "homeassistant.components.http.auth", async_sign_path=sign)
    install(monkeypatch, "homeassistant.helpers.event", async_call_later=later)

    url = await intr.get_notification_image_url(fake_hass, str(source))

    prefix = "/media/local/nova_intrusion/"
    assert url.startswith(prefix)
    assert url.endswith("?authSig=test-token")
    assert len(signed) == 1
    unsigned, expiry = signed[0]
    assert "?" not in unsigned and unsigned.startswith(prefix)
    assert expiry == timedelta(minutes=60)
    copies = list((media / "nova_intrusion").iterdir())
    assert len(copies) == 1
    assert copies[0].name != "source.jpg"
    assert copies[0].read_bytes() == JPEG
    assert source.exists()                           # the private still is left alone
    assert len(timers) == 1
    delay, expire = timers[0]
    assert delay == 3600
    await expire(None)
    assert not copies[0].exists()                    # the timer removes the copy
