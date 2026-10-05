"""face_stand_down: a recognised resident can stop Nova opening a NEW intrusion
investigation. Opt in, off by default. It never closes an open investigation,
never touches critical alerts, lockdown, freeze or the mute rules, every stand
down is written to the Action Audit Log, and any error alerts.

All state is fake. Nothing touches a real alarm, lock or camera."""
from __future__ import annotations

import ast
import pathlib

import pytest
from core_sources import core_text, core_tree

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
ALARM = "alarm_control_panel.home"


@pytest.fixture
def env(load, cognitive_core, fake_hass, monkeypatch, tmp_path):
    rec = load("recognition")
    fr = load("face_roster")
    al = load("action_log")
    db = str(tmp_path / "actions.db")
    monkeypatch.setattr(al, "_DEFAULT_DB", db)
    for box in (rec._FACE_LOG, rec._PERSON_LOG, rec._RECOGNITION_CACHE):
        box.clear()
    fr._NAMES.clear()
    fr._NAMES["sam"] = "Sam"
    cc = cognitive_core

    class E:
        pass
    e = E()
    e.rec, e.fr, e.al, e.db, e.hass, e.cc = rec, fr, al, db, fake_hass, cc

    def make(**cfg):
        base = {"honorific": "sir", "security_alarm_entity": ALARM}
        base.update(cfg)
        return cc.SafetyManager(fake_hass, base)
    e.make = make

    def rows():
        page = al.page_requests(limit=50, db_path=db)
        return [r for r in page["requests"] if r["action"] == "intrusion_face_stand_down"]
    e.rows = rows

    def age(seconds):
        from datetime import timedelta
        for box in (rec._FACE_LOG, rec._PERSON_LOG):
            for entry in box:
                entry["ts"] = entry["ts"] - timedelta(seconds=seconds)
    e.age = age

    async def tick(safety, sleeping=False):
        actions = await safety.tick(sleeping=sleeping, anyone_home=not sleeping)
        fake_hass.close_pending()
        return [a for a in actions if str(a.get("type", "")).startswith("intrusion")]
    e.tick = tick
    yield e
    for box in (rec._FACE_LOG, rec._PERSON_LOG, rec._RECOGNITION_CACHE):
        box.clear()
    fr._NAMES.clear()


def _away_with_breach(hass):
    hass.states.set("person.username", "not_home")
    hass.states.set("binary_sensor.front_door", "on", device_class="door")
    hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")


# ── the setting ─────────────────────────────────────────────────────────────

def test_only_literal_true_enables_it_and_the_write_must_be_a_boolean(load):
    sc = load("safety_config")
    assert sc.face_stand_down_enabled({"face_stand_down": True}) is True
    for v in ("true", "1", 1, "yes", None, False, [], {}):
        assert sc.face_stand_down_enabled({"face_stand_down": v}) is False
    assert sc.face_stand_down_enabled(None) is False and sc.face_stand_down_enabled({}) is False
    assert sc.valid_panel_value("face_stand_down", True) is True
    assert sc.valid_panel_value("face_stand_down", False) is True
    for v in ("true", 1, 0, None, "on", [True]):
        assert sc.valid_panel_value("face_stand_down", v) is False


def test_setting_is_panel_writable_surfaced_and_applied_live():
    ws = (COMP / "websocket.py").read_text()
    assert '"face_stand_down",             # bool, off by default' in ws
    assert '"face_stand_down": _runtime_opt(\n                    hass, entry, "face_stand_down", False) is True,' in ws
    assert '"intrusion_requires_confinement", "face_stand_down"):\n            from . import cognitive_core' in ws
    cc = core_text()
    assert 'key in ("intrusion_requires_confinement", "face_stand_down")' in cc


async def test_apply_runtime_config_turns_it_on_and_off_live(env):
    cc = env.cc
    safety = env.make()
    cc._CORE.config = {}
    cc._CORE.safety_mgr = safety
    cc._CORE.lockdown_mgr = None
    try:
        await cc.apply_runtime_config("face_stand_down", True)
        assert safety.config["face_stand_down"] is True
        await cc.apply_runtime_config("face_stand_down", "true")     # not the literal true
        assert safety.config["face_stand_down"] is False
    finally:
        cc._CORE.safety_mgr = None


# ── off by default: nothing changes ─────────────────────────────────────────

async def test_off_by_default_a_resident_face_changes_nothing(env):
    env.rec.remember_recognition("front", "Sam", 95.0, source="frigate")
    safety = env.make()                                 # setting absent
    _away_with_breach(env.hass)
    intr = await env.tick(safety)
    assert len(intr) == 1 and "no one is home" in intr[0]["message"]
    assert safety._investigation is not None and env.rows() == []


async def test_off_the_armed_away_confinement_path_still_alerts(env):
    env.rec.remember_recognition("front", "Sam", 95.0)
    safety = env.make(intrusion_requires_confinement=True)
    env.hass.states.set("person.username", "not_home")
    env.hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    env.hass.states.set(ALARM, "armed_away")
    intr = await env.tick(safety)
    assert len(intr) == 1 and "no one is home" in intr[0]["message"]
    assert env.rows() == []


async def test_off_confined_with_residents_home_and_armed_away_still_alerts(env):
    env.rec.remember_recognition("front", "Sam", 95.0)
    safety = env.make(intrusion_requires_confinement=True)
    env.hass.states.set("person.username", "home")
    env.hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    env.hass.states.set(ALARM, "armed_away")
    assert len(await env.tick(safety)) == 1 and env.rows() == []


async def test_off_asleep_with_a_breach_still_alerts(env):
    env.rec.remember_recognition("front", "Sam", 95.0)
    safety = env.make()
    env.hass.states.set("binary_sensor.front_door", "on", device_class="door")
    env.hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    assert len(await env.tick(safety, sleeping=True)) == 1


# ── rule 1: roster, trusted backend, threshold, 180 seconds ─────────────────

async def test_rule1_a_trusted_resident_stands_it_down_and_is_audited(env):
    env.rec.remember_recognition("front", "sam", 91.0, source="frigate")
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    assert await env.tick(safety) == []
    assert safety._investigation is None
    rows = env.rows()
    assert len(rows) == 1


async def test_rule1_not_on_the_roster_alerts(env):
    env.rec.remember_recognition("front", "Stranger", 99.0)
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    assert len(await env.tick(safety)) == 1 and env.rows() == []


@pytest.mark.parametrize("conf,stands", [(59.9, False), (60.0, True), (60.1, True), (0.0, False)])
async def test_rule1_confidence_threshold_is_at_or_above(env, conf, stands):
    assert env.rec.CONFIDENCE_THRESHOLD == 60
    env.rec.remember_recognition("front", "Sam", conf)
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    intr = await env.tick(safety)
    assert (intr == []) is stands


@pytest.mark.parametrize("age,stands", [(0, True), (179, True), (181, False), (3600, False)])
async def test_rule1_window_is_180_seconds(env, age, stands):
    env.rec.remember_recognition("front", "Sam", 90.0)
    env.age(age)
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    assert (await env.tick(safety) == []) is stands


async def test_rule1_only_backend_recognitions_count(env):
    """A cache entry that did not come through remember_recognition (so not
    from Frigate or Double Take) is not trusted."""
    from datetime import datetime, timezone
    env.rec._RECOGNITION_CACHE["camera.front"] = {
        "name": "Sam", "confidence": 99.0, "unknown_count": 0,
        "ts": datetime.now(timezone.utc).replace(tzinfo=None)}
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    assert len(await env.tick(safety)) == 1
    # and neither does a Frigate sensor, which has no event time Nova can trust
    env.hass.states.set("sensor.front_last_recognized_face", "Sam", score=0.95)
    assert len(await env.tick(env.make(face_stand_down=True))) == 1


async def test_rule1_a_stand_down_does_not_block_a_later_alert(env):
    env.rec.remember_recognition("front", "Sam", 90.0)
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    assert await env.tick(safety) == []
    env.age(200)                                     # the resident has gone quiet
    assert len(await env.tick(safety)) == 1          # not held back by a 5 minute lockout


# ── rule 2: any other face blocks it ────────────────────────────────────────

@pytest.mark.parametrize("name,conf", [("unknown", 70.0), ("Unknown Person", 0.0),
                                       ("Sam", 40.0), ("Visitor", 95.0)])
async def test_rule2_another_face_on_any_camera_blocks_it(env, name, conf):
    env.rec.remember_recognition("front", "Sam", 95.0)
    env.rec.remember_recognition("garden", name, conf)        # a different camera
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    assert len(await env.tick(safety)) == 1 and env.rows() == []


async def test_rule2_an_unknown_face_just_before_the_resident_still_blocks(env):
    """The cache keeps only the latest face per camera, so an unknown face
    followed by a resident on the same camera would be forgotten there. The
    recognition log is what the stand down reads."""
    env.rec.remember_recognition("front", "unknown", 50.0)
    env.rec.remember_recognition("front", "Sam", 95.0)
    assert env.rec._RECOGNITION_CACHE["camera.front"]["name"] == "Sam"
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    assert len(await env.tick(safety)) == 1


async def test_rule2_an_unexplained_person_detection_blocks_it(env):
    env.rec.remember_recognition("front", "Sam", 95.0)
    env.rec.note_person_detected("garden")                    # a person, nobody named
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    assert len(await env.tick(safety)) == 1
    env.rec._PERSON_LOG.clear()
    env.rec.note_person_detected("front")                     # the resident's own camera
    assert await env.tick(env.make(face_stand_down=True)) == []


async def test_rule2_an_old_unknown_face_outside_the_window_does_not_block(env):
    env.rec.remember_recognition("garden", "unknown", 50.0)
    env.age(400)
    env.rec.remember_recognition("front", "Sam", 95.0)
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    assert await env.tick(safety) == []


# ── rule 3: never ends an investigation that is already open ───────────────

async def test_rule3_an_open_investigation_is_never_ended_by_a_face(env):
    safety = env.make()                                  # setting off: it opens
    _away_with_breach(env.hass)
    assert len(await env.tick(safety)) == 1
    assert safety._investigation is not None
    safety.config["face_stand_down"] = True              # now on, with a resident in view
    env.rec.remember_recognition("front", "Sam", 99.0)
    called = []
    orig = safety._face_stand_down

    async def spy(*a, **k):
        called.append(a)
        return await orig(*a, **k)
    safety._face_stand_down = spy
    for _ in range(3):
        await env.tick(safety)
    assert safety._investigation is not None and called == []
    assert env.rows() == []


def test_rule3_the_stand_down_is_only_called_where_an_investigation_would_start():
    tree = core_tree()
    calls = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n = sum(1 for c in ast.walk(fn) if isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute) and c.func.attr == "_face_stand_down")
            if n:
                calls[fn.name] = n
    assert calls == {"_check_intrusion": 3}              # away, confined, sleeping: never _investigate_step
    src = core_text()
    step = src.split("async def _investigate_step")[1].split("\n    async def ")[0]
    assert "face_roster" not in step and "_face_stand_down" not in step


# ── rule 4: nothing else reads it ───────────────────────────────────────────

def test_rule4_critical_lockdown_freeze_and_mutes_never_read_the_roster():
    tree = core_tree()
    users = [fn.name for fn in ast.walk(tree)
             if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
             and any(isinstance(n, ast.Name) and n.id == "face_roster"
                     or isinstance(n, ast.ImportFrom) and any(a.name == "face_roster" for a in n.names)
                     for n in ast.walk(fn))]
    assert users == ["_face_stand_down"]
    for name in ("output_gate.py", "safety_config.py", "alarm_source.py", "lockdown.py"):
        p = COMP / name
        if p.exists():
            assert "face_roster" not in p.read_text(), name


def test_rule4_critical_still_bypasses_every_mute(load):
    og = load("output_gate")
    og._STATE.mute_all = True
    try:
        ok, why = og.can_announce(entity_id="x", category="safety", urgency="critical",
                                  message="smoke detected")
        assert ok is True and why == "critical bypass"
    finally:
        og._STATE.mute_all = False


# ── rule 5: every stand down is in the Action Audit Log ─────────────────────

async def test_rule5_the_audit_row_says_who_where_how_sure_and_why(env):
    env.rec.remember_recognition("front", "Sam", 91.0, source="frigate")
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    await env.tick(safety)
    [row] = env.rows()
    assert row["source"] == "safety"
    t = row["targets"][0]
    assert t["entity_id"] == "camera.front"
    assert row["requested_by_name"] == "Sam"
    assert t["execution_result"] == "accepted" and t["reason_code"] == "face_stand_down"
    why = t["reason_text"]
    assert "Sam" in why and "front" in why and "91%" in why and "resident" in why
    assert "away" in why and "no other face" in why
    assert "face_stand_down" in why


async def test_rule5_each_stand_down_is_a_separate_row(env):
    env.rec.remember_recognition("front", "Sam", 91.0)
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    await env.tick(safety)
    await env.tick(safety)
    assert len(env.rows()) == 2


# ── rule 6: any error alerts ────────────────────────────────────────────────

async def test_rule6_an_error_in_the_decision_alerts(env, monkeypatch):
    env.rec.remember_recognition("front", "Sam", 91.0)

    def boom(*a, **k):
        raise RuntimeError("log unreadable")
    monkeypatch.setattr(env.rec, "recent_face_events", boom)
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    assert len(await env.tick(safety)) == 1 and env.rows() == []


async def test_rule6_an_error_in_the_stand_down_itself_alerts(env, monkeypatch):
    env.rec.remember_recognition("front", "Sam", 91.0)

    def boom(*a, **k):
        raise RuntimeError("roster broke")
    monkeypatch.setattr(env.fr, "evaluate_stand_down", boom)
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    assert len(await env.tick(safety)) == 1


async def test_rule6_if_the_audit_write_fails_the_alert_goes_ahead(env, monkeypatch):
    env.rec.remember_recognition("front", "Sam", 91.0)
    monkeypatch.setattr(env.al, "start", lambda *a, **k: None)     # what a failed write returns
    safety = env.make(face_stand_down=True)
    _away_with_breach(env.hass)
    assert len(await env.tick(safety)) == 1 and env.rows() == []


async def test_rule6_a_non_literal_setting_is_off(env):
    env.rec.remember_recognition("front", "Sam", 91.0)
    for bad in ("true", 1, "on"):
        safety = env.make(face_stand_down=bad)
        _away_with_breach(env.hass)
        assert len(await env.tick(safety)) == 1
    assert env.rows() == []


def test_the_pure_decision_never_raises(env, monkeypatch):
    monkeypatch.setattr(env.rec, "recent_face_events", lambda w: 1 / 0)
    d = env.fr.evaluate_stand_down()
    assert d["stand_down"] is False and "alert goes ahead" in d["reason"]


# ── all three ways an investigation can start ───────────────────────────────

async def test_stand_down_applies_to_a_new_sleeping_investigation(env):
    env.rec.remember_recognition("front", "Sam", 91.0)
    safety = env.make(face_stand_down=True)
    env.hass.states.set("binary_sensor.front_door", "on", device_class="door")
    env.hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    assert await env.tick(safety, sleeping=True) == []
    assert safety._investigation is None and len(env.rows()) == 1
    env.rec._FACE_LOG.clear()
    env.rec.remember_recognition("garden", "unknown", 30.0)          # an unknown face: it alerts
    assert len(await env.tick(safety, sleeping=True)) == 1


async def test_stand_down_applies_to_a_new_confined_investigation(env):
    env.rec.remember_recognition("front", "Sam", 91.0)
    safety = env.make(face_stand_down=True, intrusion_requires_confinement=True)
    env.hass.states.set("person.username", "home")
    env.hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    env.hass.states.set(ALARM, "armed_away")
    assert await env.tick(safety) == [] and len(env.rows()) == 1
    env.rec._FACE_LOG.clear()
    assert len(await env.tick(safety)) == 1                          # no resident in view: it alerts
