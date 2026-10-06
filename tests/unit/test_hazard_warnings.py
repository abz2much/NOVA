"""Hazard warnings (8.8.0): the lifecycle, the saved memory and the wording."""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "met_eireann"
NOW = datetime(2026, 9, 9, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def hw(load):
    return load("hazard_warnings")


def _w(wid="w1", level="orange", wtype="Wind", areas=("Clare",), keys=("EI03",), refs=(),
       msg_type="Alert", expiry="2026-09-10T12:00:00+01:00", headline="Status Orange - Wind",
       description="<p>Prepare.</p>"):
    return {"id": wid, "msg_type": msg_type, "refs": list(refs), "source": "met_eireann",
            "source_label": "Met Éireann", "type": wtype, "level": level,
            "onset": "2026-09-09T12:00:00+01:00", "expiry": expiry, "headline": headline,
            "description": description, "areas": list(areas), "area_keys": list(keys)}


def _run(hw, entries, current, complete=True, push="yellow", now=NOW):
    return hw.reconcile(entries, current, complete=complete, now=now, push_level=push)


def _kinds(events):
    return [e[0] for e in events]


# ── lifecycle ───────────────────────────────────────────────────────────────

def test_a_new_warning_is_announced_once(hw):
    entries, events = _run(hw, {}, [_w()])
    assert _kinds(events) == ["new"] and entries["w1"]["notified"] is True
    entries, events = _run(hw, entries, [_w()])
    assert events == []
    entries, events = _run(hw, entries, [_w()])
    assert events == []


def test_an_upgrade_and_a_downgrade_are_announced(hw):
    entries, _ = _run(hw, {}, [_w(level="yellow")])
    entries, events = _run(hw, entries, [_w(level="orange")])
    assert events == [("upgraded", _w(level="orange"), "yellow")]
    entries, events = _run(hw, entries, [_w(level="red")])
    assert _kinds(events) == ["upgraded"]
    entries, events = _run(hw, entries, [_w(level="yellow")])
    assert events == [("lowered", _w(level="yellow"), "red")]


def test_a_cap_update_carries_on_the_warning_it_references(hw):
    entries, _ = _run(hw, {}, [_w("old", level="yellow")])
    entries, events = _run(hw, entries, [_w("new", level="orange", refs=["old"], msg_type="Update")])
    assert events == [("upgraded", _w("new", level="orange", refs=["old"], msg_type="Update"),
                       "yellow")]
    assert set(entries) == {"new"}
    entries, events = _run(hw, entries, [_w("newer", level="orange", refs=["new"])])
    assert events == [] and set(entries) == {"newer"}        # same level: no repeat


def test_a_json_reissue_without_references_carries_on_by_type_and_county(hw):
    entries, _ = _run(hw, {}, [_w("a", level="yellow")])
    entries, events = _run(hw, entries, [_w("b", level="orange")])
    assert _kinds(events) == ["upgraded"] and set(entries) == {"b"}
    # A different type in the same county is a new warning.
    entries, events = _run(hw, entries, [_w("b", level="orange"), _w("c", wtype="Rain")])
    assert _kinds(events) == ["new"]


def test_cancelled_after_a_good_empty_list(hw):
    entries, _ = _run(hw, {}, [_w()])
    entries, events = _run(hw, entries, [], complete=True)
    assert _kinds(events) == ["cancelled"] and entries == {}
    assert events[0][1]["headline"] == "Status Orange - Wind"


def test_a_failed_fetch_never_cancels(hw):
    entries, _ = _run(hw, {}, [_w()])
    for _ in range(3):
        entries, events = _run(hw, entries, [], complete=False)
        assert events == [] and "w1" in entries


def test_a_cap_cancel_message_cancels_its_reference(hw):
    entries, _ = _run(hw, {}, [_w()])
    entries, events = _run(hw, entries, [{"id": "x", "msg_type": "Cancel", "refs": ["w1"]}],
                           complete=False)
    assert _kinds(events) == ["cancelled"] and entries == {}


def test_an_expired_warning_is_ignored_and_forgotten_quietly(hw):
    gone = "2026-09-09T05:00:00+00:00"
    entries, events = _run(hw, {}, [_w(expiry=gone)])
    assert events == [] and entries == {}
    entries, _ = _run(hw, {}, [_w()])
    later = NOW + timedelta(days=3)                            # after its expiry
    entries, events = _run(hw, entries, [], now=later)
    assert events == [] and entries == {}                      # no "cancelled" for an expiry


def test_below_the_push_level_is_remembered_not_announced(hw):
    entries, events = _run(hw, {}, [_w(level="yellow")], push="orange")
    assert events == [] and entries["w1"]["notified"] is False
    entries, events = _run(hw, entries, [_w(level="orange")], push="orange")
    assert _kinds(events) == ["upgraded"] and entries["w1"]["notified"] is True
    # Never announced, so never "cancelled" either.
    entries2, _ = _run(hw, {}, [_w(level="yellow")], push="orange")
    assert _run(hw, entries2, [], push="orange")[1] == []


def test_warnings_that_are_not_a_level_are_ignored(hw):
    assert _run(hw, {}, [_w(level="green"), _w("x", level="")]) == ({}, [])


# ── memory ──────────────────────────────────────────────────────────────────

def test_the_memory_survives_a_restart(hw, tmp_path):
    path = str(tmp_path / "hazard_warnings.json")
    entries, events = _run(hw, {}, [_w()])
    assert hw.save_store({"met_eireann": entries}, NOW, path) is True
    loaded = hw.load_store(NOW, path)
    assert loaded == {"met_eireann": entries}
    _entries, events = _run(hw, loaded["met_eireann"], [_w()])
    assert events == []                                       # no repeat after a restart


def test_a_missing_corrupt_or_oversized_file_starts_empty(hw, tmp_path, caplog):
    path = tmp_path / "hazard_warnings.json"
    assert hw.load_store(NOW, str(path)) == {}
    path.write_text("{not json")
    assert hw.load_store(NOW, str(path)) == {}
    assert "corrupt" in caplog.text
    path.write_text(json.dumps({"met_eireann": {"x": {"level": "red"}}}) + " " * hw.STORE_MAX_BYTES)
    assert hw.load_store(NOW, str(path)) == {}
    assert "too large" in caplog.text


def test_invalid_and_expired_entries_are_dropped_on_load(hw, tmp_path):
    path = tmp_path / "hazard_warnings.json"
    path.write_text(json.dumps({
        "met_eireann": {
            "ok": {"level": "orange", "expiry": "2026-09-10T12:00:00+00:00", "notified": True},
            "old": {"level": "orange", "expiry": "2026-09-01T12:00:00+00:00"},
            "bad": {"level": "purple"}, "junk": "x"},
        "other": [1, 2]}))
    assert set(hw.load_store(NOW, str(path))["met_eireann"]) == {"ok"}


def test_the_memory_is_capped(hw):
    entries = {f"w{i}": hw._entry_for(_w(f"w{i}", expiry=f"2026-10-{1 + i % 28:02d}T00:00:00+00:00"), True)
               for i in range(hw.STORE_MAX_ENTRIES + 50)}
    assert len(hw._trim(entries, NOW)) == hw.STORE_MAX_ENTRIES


def test_a_save_that_fails_says_so(hw, tmp_path):
    (tmp_path / "f").write_text("")
    assert hw.save_store({"met_eireann": {}}, NOW, str(tmp_path / "f" / "x.json")) is False


# ── wording ─────────────────────────────────────────────────────────────────

def test_the_description_text_keeps_every_word(hw):
    desc = json.loads((FIX / "json" / "warning_IRELAND.json").read_text())[1]["description"]
    text = hw.text_of(desc)
    assert text.splitlines() == [
        "Be aware of potential impacts:", "", "• Difficult travelling conditions",
        "• Localised flooding possible", "• Outdoor events impacted",
        "• Animal welfare issues", "• Hazardous travelling conditions",
        "• Outdoor events impacted", "• Poor visibility", "• Travel disruption"]
    assert hw.text_of("Plain &amp; simple") == "Plain & simple"


def test_spoken_is_novas_sentence_and_the_unaltered_headline(hw):
    w = _w(areas=("Clare", "Galway"), headline="Status Orange - Wind and Rain warning for Clare, Galway, Mayo")
    said = hw.spoken("new", w, "sir", "Europe/Dublin")
    assert said == ("Met Éireann Orange wind warning for Clare and Galway, from Wed 9 Sep 12:00 "
                    "to Thu 10 Sep 12:00, sir. Status Orange - Wind and Rain warning for Clare, "
                    "Galway, Mayo")
    assert "Prepare" not in said                          # the description is never spoken


def test_no_honorific_leaves_no_dangling_comma(hw):
    said = hw.spoken("new", _w(), "", "Europe/Dublin")
    assert ", ." not in said and said.startswith("Met Éireann Orange wind warning for Clare, from ")
    assert hw.spoken("cancelled", _w(), "", "Europe/Dublin").startswith(
        "Met Éireann has cancelled the Orange wind warning for Clare. ")


def test_level_change_wording(hw):
    up = hw.spoken("upgraded", _w(level="red"), "sir", "Europe/Dublin", "orange")
    assert up.startswith("Met Éireann has upgraded the wind warning for Clare from Orange to Red, ")
    down = hw.spoken("lowered", _w(level="yellow"), "", "Europe/Dublin", "orange")
    assert down.startswith("Met Éireann has lowered the wind warning for Clare from Orange to Yellow, ")


def test_the_push_has_the_full_description_and_the_credit(hw):
    w = _w(description="<p>Line one.</p><ul><li>Two</li></ul>")
    pushed = hw.pushed("new", w, "sir", "Europe/Dublin")
    assert pushed == "\n\n".join([hw.spoken("new", w, "sir", "Europe/Dublin"),
                                   "Line one.\n\n• Two", "Source: Met Éireann"])
    assert hw.pushed("cancelled", w, "", None).endswith("Source: Met Éireann")
    assert "Line one" not in hw.pushed("cancelled", w, "", None)


def test_times_are_shown_in_home_assistants_time_zone(hw):
    assert hw.local_time("2026-09-09T12:00:00+01:00", "Europe/Dublin") == "Wed 9 Sep 12:00"
    assert hw.local_time("2026-09-09T12:00:00+01:00", "America/New_York") == "Wed 9 Sep 07:00"
    assert hw.local_time("not a time", "Europe/Dublin") == ""


def test_for_panel_keeps_old_keys_and_adds_the_text(hw):
    p = hw.for_panel(_w(), "Europe/Dublin")
    assert p["headline"] == "Status Orange - Wind" and p["description"] == "<p>Prepare.</p>"
    assert p["description_text"] == "Prepare." and p["counties"] == ["Clare"]
    assert p["from"] == "Wed 9 Sep 12:00" and p["to"] == "Thu 10 Sep 12:00"
    assert p["source_label"] == "Met Éireann" and p["level"] == "orange"


def test_snow_ice_and_hyphenated_types_read_naturally(hw):
    assert hw.type_words("snow-ice") == "snow and ice"
    assert hw.type_words("low-temperature") == "low temperature"
    assert hw.type_words("Wind") == "wind" and hw.type_words("") == "weather"
