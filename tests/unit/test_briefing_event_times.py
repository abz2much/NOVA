"""Sentinel events are stored in UTC; briefings read them out in local time."""
import datetime
import sqlite3


def test_overnight_events_use_local_time(load, fake_hass, tmp_path, monkeypatch):
    briefing = load("briefing")
    db = tmp_path / "conversations.db"
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE sentinel_events "
                     "(timestamp TEXT, entity_id TEXT, event_type TEXT, detail TEXT)")
        conn.execute("INSERT INTO sentinel_events VALUES (?,?,?,?)",
                     (now_utc.replace(tzinfo=None).isoformat(), "binary_sensor.door",
                      "open", "Door opened"))
    monkeypatch.setattr(briefing, "CONVERSATIONS_DB", str(db))
    local = now_utc.astimezone().strftime("%H:%M")
    assert briefing._gather_overnight_events(fake_hass, 0.5) == [f"{local}: Door opened"]
