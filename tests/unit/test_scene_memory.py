"""Scene memory (opt in): what the cameras described, kept over time.

Covers the text cleaning (denials never count as sightings), storage bounds
(age and per camera cap), search, comparison, the off switch, the agent tools
and the setting validators. Uses a real temporary database.
"""
import json
import time

import pytest


class _Hass:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


@pytest.fixture
def sm(load):
    return load("scene_memory")


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "scene_memory.db")


def _on(monkeypatch, load, on=True, days=None):
    nc = load("nova_config")
    values = {"scene_memory_enabled": on}
    if days is not None:
        values["scene_memory_retention_days"] = days
    monkeypatch.setattr(nc, "get", lambda k, d=None: values.get(k, d))


# ── cleaning ────────────────────────────────────────────────────────────────

def test_extract_keeps_plain_things_in_singular(sm):
    words = sm.extract_objects("A set of keys and two red bikes beside the boxes.")
    assert {"key", "red", "bike", "box", "set"} <= set(words)
    assert "the" not in words and "two" not in words


def test_denials_never_count(sm):
    assert "package" not in sm.extract_objects("No package is visible on the porch.")
    assert "package" not in sm.extract_objects("The porch is empty, without a package.")
    assert "keys" not in sm.extract_objects("Cannot see any keys on the table")
    assert "key" not in sm.extract_objects("The keys are missing from the hook.")


def test_denial_only_blocks_its_own_clause(sm):
    words = sm.extract_objects("A car is parked outside, but no person is visible.")
    assert "car" in words and "person" not in words


def test_term_is_cleaned_like_stored_text(sm):
    assert sm.normalise_term("My Keys") == ["my", "key"] or sm.normalise_term("My Keys") == ["key"]
    assert sm.normalise_term("red bikes") == ["red", "bike"]
    assert sm.normalise_term("the") == []


def test_singular_rules(sm):
    for plural, single in (("keys", "key"), ("boxes", "box"), ("berries", "berry"),
                           ("glasses", "glass"), ("bus", "bus"), ("grass", "grass")):
        assert sm._singular(plural) == single


# ── store and search ────────────────────────────────────────────────────────

def test_where_last_seen_returns_latest_match(sm, db):
    assert sm.record_scene("camera.hall", "Keys on the hall table.", "hall",
                           ts=time.time() - 7200, db_path=db)
    assert sm.record_scene("camera.kitchen", "Keys beside the kettle.", "kitchen",
                           ts=time.time() - 600, db_path=db)
    assert sm.record_scene("camera.hall", "An empty hall table.", "hall",
                           db_path=db)
    hit = sm.where_last_seen("keys", db_path=db)
    assert hit["camera"] == "camera.kitchen" and hit["area"] == "kitchen"


def test_a_denial_is_never_a_sighting(sm, db):
    sm.record_scene("camera.porch", "No package is visible on the porch.", db_path=db)
    assert sm.where_last_seen("package", db_path=db) is None
    sm.record_scene("camera.porch", "A package sits by the door.", db_path=db)
    assert sm.where_last_seen("package", db_path=db)["camera"] == "camera.porch"


def test_multi_word_search_needs_every_word(sm, db):
    sm.record_scene("camera.garage", "A red bike leans on the wall.", db_path=db)
    sm.record_scene("camera.yard", "A blue bike on the grass.", db_path=db)
    assert sm.where_last_seen("red bike", db_path=db)["camera"] == "camera.garage"
    assert sm.where_last_seen("green bike", db_path=db) is None


def test_search_is_whole_word_not_substring(sm, db):
    sm.record_scene("camera.yard", "A monkey statue by the pond.", db_path=db)
    assert sm.where_last_seen("key", db_path=db) is None


def test_search_term_cannot_inject_wildcards(sm, db):
    sm.record_scene("camera.yard", "A bike by the wall.", db_path=db)
    assert sm.where_last_seen("%", db_path=db) is None
    assert sm.where_last_seen("_ike", db_path=db) is None


def test_empty_and_blank_input(sm, db):
    assert sm.record_scene("", "text", db_path=db) is False
    assert sm.record_scene("camera.a", "   ", db_path=db) is False
    assert sm.where_last_seen("", db_path=db) is None


def test_long_descriptions_are_cut(sm, db):
    sm.record_scene("camera.a", "bike " * 400, db_path=db)
    row = sm.where_last_seen("bike", db_path=db)
    assert len(row["description"]) <= sm.MAX_DESCRIPTION_CHARS


# ── bounds ──────────────────────────────────────────────────────────────────

def test_old_rows_are_dropped(sm, db):
    sm.record_scene("camera.a", "A ladder.", ts=time.time() - 20 * 86400,
                    retention=14, db_path=db)
    assert sm.stats(db_path=db)["count"] == 0
    sm.record_scene("camera.a", "A ladder.", ts=time.time() - 3 * 86400,
                    retention=14, db_path=db)
    assert sm.stats(db_path=db)["count"] == 1
    sm.record_scene("camera.a", "A bucket.", retention=1, db_path=db)
    assert sm.where_last_seen("ladder", db_path=db) is None
    assert sm.where_last_seen("bucket", db_path=db) is not None


def test_each_camera_is_capped(sm, db, monkeypatch):
    monkeypatch.setattr(sm, "MAX_ROWS_PER_CAMERA", 5)
    for i in range(9):
        sm.record_scene("camera.a", f"Item{i} number", ts=time.time() - (9 - i),
                        db_path=db)
    sm.record_scene("camera.b", "Other camera", db_path=db)
    st = sm.stats(db_path=db)
    assert st["count"] == 6 and st["cameras"] == 2
    assert sm.where_last_seen("item0", db_path=db) is None
    assert sm.where_last_seen("item8", db_path=db) is not None


def test_forget_all(sm, db):
    sm.record_scene("camera.a", "A ladder.", db_path=db)
    sm.record_scene("camera.b", "A bucket.", db_path=db)
    assert sm.forget_all(db_path=db) == 2
    assert sm.stats(db_path=db)["count"] == 0
    assert sm.where_last_seen("ladder", db_path=db) is None


# ── comparison ──────────────────────────────────────────────────────────────

def test_what_changed_compares_with_an_older_description(sm, db):
    now = time.time()
    sm.record_scene("camera.garage", "A car and a ladder.", "garage",
                    ts=now - 10 * 3600, db_path=db)
    sm.record_scene("camera.garage", "A car and a bike.", "garage",
                    ts=now - 60, db_path=db)
    res = sm.what_changed("garage", now - 5 * 3600, db_path=db)
    assert res["found"] and res["added"] == ["bike"] and res["removed"] == ["ladder"]
    by_entity = sm.what_changed("CAMERA.GARAGE", now - 5 * 3600, db_path=db)
    assert by_entity["added"] == ["bike"]


def test_what_changed_without_an_older_row(sm, db):
    sm.record_scene("camera.garage", "A car.", "garage", db_path=db)
    res = sm.what_changed("garage", time.time() - 3600, db_path=db)
    assert res["found"] is True and res["baseline_ts"] is None
    assert res["added"] == [] and res["removed"] == []


def test_what_changed_unknown_camera(sm, db):
    assert sm.what_changed("nowhere", time.time(), db_path=db)["found"] is False
    assert sm.what_changed("", time.time(), db_path=db)["found"] is False


# ── off switch and settings ─────────────────────────────────────────────────

async def test_remember_does_nothing_when_off(sm, load, monkeypatch, db):
    _on(monkeypatch, load, on=False)
    monkeypatch.setattr(sm, "_DEFAULT_DB", db)
    assert await sm.async_remember(_Hass(), "camera.a", "A ladder.") is False
    assert sm.stats(db_path=db)["count"] == 0


async def test_remember_records_when_on(sm, load, monkeypatch, db):
    _on(monkeypatch, load, on=True)
    monkeypatch.setattr(sm, "_DEFAULT_DB", db)
    assert await sm.async_remember(_Hass(), "camera.a", "A ladder.", "garage") is True
    assert sm.where_last_seen("ladder", db_path=db)["area"] == "garage"
    assert await sm.async_remember(_Hass(), "camera.a", "") is False


def test_only_literal_true_turns_it_on(sm, load, monkeypatch):
    nc = load("nova_config")
    for value in ("true", 1, None, False):
        monkeypatch.setattr(nc, "get", lambda k, d=None, v=value: v)
        assert sm.enabled() is False
    monkeypatch.setattr(nc, "get", lambda k, d=None: True)
    assert sm.enabled() is True


def test_retention_default_and_bounds(sm, load, monkeypatch):
    for value in (0, 91, "7", True, None):
        _on(monkeypatch, load, days=value)
        assert sm.retention_days() == 14
    _on(monkeypatch, load, days=30)
    assert sm.retention_days() == 30


def test_setting_validators(load):
    sc = load("safety_config")
    assert sc.valid_panel_value("scene_memory_enabled", True) is True
    for value in ("true", 1, None):
        assert sc.valid_panel_value("scene_memory_enabled", value) is False
    assert sc.valid_panel_value("scene_memory_retention_days", 30) is True
    for value in (0, 91, "30", 7.5, True):
        assert sc.valid_panel_value("scene_memory_retention_days", value) is False


# ── agent tools ─────────────────────────────────────────────────────────────

@pytest.fixture
def cams(load):
    return load("agent_runtime.capabilities.cameras")


async def test_tools_say_when_scene_memory_is_off(cams, sm, load, monkeypatch):
    _on(monkeypatch, load, on=False)
    out = json.loads(await cams._exec_where_last_seen(_Hass(), {"term": "keys"}))
    assert out["enabled"] is False and out["found"] is False and "off" in out["hint"]
    out = json.loads(await cams._exec_what_changed(_Hass(), {"camera": "garage"}))
    assert out["enabled"] is False


async def test_tools_validate_input(cams):
    assert "error" in json.loads(await cams._exec_where_last_seen(_Hass(), {}))
    assert "error" in json.loads(await cams._exec_what_changed(_Hass(), {}))


async def test_where_last_seen_tool_answers_from_memory(
        cams, sm, load, monkeypatch, db, fake_hass):
    _on(monkeypatch, load)
    monkeypatch.setattr(sm, "_DEFAULT_DB", db)
    fake_hass.states.set("camera.hall", "idle", friendly_name="Hall camera")
    sm.record_scene("camera.hall", "Keys on the hall table.", "hall",
                    ts=time.time() - 3 * 3600, db_path=db)

    class H(_Hass):
        states = fake_hass.states
        config = fake_hass.config

    out = json.loads(await cams._exec_where_last_seen(H(), {"term": "keys"}))
    assert out["found"] is True and "3 hours ago" == out["when"]
    assert "live view" in out["note"]
    miss = json.loads(await cams._exec_where_last_seen(H(), {"term": "wallet"}))
    assert miss["found"] is False and miss["enabled"] is True


async def test_what_changed_tool_clamps_hours_and_reports(
        cams, sm, load, monkeypatch, db):
    _on(monkeypatch, load)
    monkeypatch.setattr(sm, "_DEFAULT_DB", db)
    now = time.time()
    sm.record_scene("camera.garage", "A ladder.", "garage", ts=now - 7200, db_path=db)
    sm.record_scene("camera.garage", "A bike.", "garage", ts=now - 30, db_path=db)
    out = json.loads(await cams._exec_what_changed(
        _Hass(), {"camera": "garage", "hours": 1}))
    assert out["found"] is True and out["added"] == ["bike"]
    assert out["removed"] == ["ladder"]
    # A junk "hours" falls back to 24, and nothing is that old yet.
    junk = json.loads(await cams._exec_what_changed(
        _Hass(), {"camera": "garage", "hours": "junk"}))
    assert junk["found"] is True and "nothing older" in junk["comparison"]
    huge = json.loads(await cams._exec_what_changed(
        _Hass(), {"camera": "garage", "hours": 10**9}))
    assert huge["found"] is True


def test_question_looks_are_not_recorded(cams):
    import inspect
    src = inspect.getsource(cams._analyze_camera)
    assert '"record_scene": False' in src


def test_tools_are_read_only_and_fenced(load):
    reg = load("agent_runtime.registry")
    models = load("agent_runtime.models")
    for name in ("where_last_seen", "what_changed"):
        row = reg.TOOL_REGISTRY[name]
        assert (row.capability, row.mutates, row.persists, row.network) == (
            "cameras", False, False, False)
        assert row.trust == models.Trust.UNTRUSTED_EXTERNAL
