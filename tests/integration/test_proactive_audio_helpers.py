"""nova.speak's helpers against a real Home Assistant: area lookup, speaker
choice, the light and noise readings that shape the voice, ducking and
restoring volume, the tts.speak retry, spoken history, the audit's history
phrase and habit sampling, and the state ledger check at start up.

No real speaker is driven: media_player and tts services are recorders
registered here."""
import types

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .test_runtime_data import (  # noqa: F401  (autouse fixtures)
    _no_real_config,
    _restore_nova_config,
)


@pytest.fixture
def pa():
    from custom_components.nova import proactive_audio
    return proactive_audio


@pytest.fixture
def cfg(monkeypatch):
    """Saved settings seen through nova_config."""
    from custom_components.nova import nova_config
    values = {}
    monkeypatch.setattr(nova_config, "get", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(nova_config, "runtime_get",
                        lambda hass, entry, key, default=None: values.get(key, default))
    return values


def _runtime(**config):
    return types.SimpleNamespace(runtime_config=dict(config))


@pytest.fixture
def calls(hass):
    """Recorders for media_player.volume_set and tts.speak."""
    seen = {"volume": [], "tts": [], "reject_options": False, "fail_tts": None}

    async def volume(call):
        seen["volume"].append((call.data["entity_id"], call.data["volume_level"]))

    async def speak(call):
        if seen["fail_tts"]:
            raise seen["fail_tts"]
        if seen["reject_options"] and "options" in call.data:
            raise HomeAssistantError("options not supported")
        seen["tts"].append(dict(call.data))

    hass.services.async_register("media_player", "volume_set", volume)
    hass.services.async_register("tts", "speak", speak)
    return seen


# ── area lookup ─────────────────────────────────────────────────────────────

async def test_area_by_id_or_name(hass, pa):
    office = ar.async_get(hass).async_create("Office Room")
    assert pa._resolve_area_id(hass, office.id) == office.id
    assert pa._resolve_area_id(hass, "Office Room") == office.id
    assert pa._resolve_area_id(hass, "office room") == office.id
    assert pa._resolve_area_id(hass, "Nowhere") is None


# ── small helpers ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,want", [
    (None, None), (True, None), ("x", None), ("nan", None), ("inf", None),
    (float("-inf"), None), ("3.5", 3.5), (2, 2.0), ([1], None),
])
def test_as_float(pa, value, want):
    assert pa._as_float(value) == want


def test_estimate_duration(pa):
    assert pa._estimate_duration("one", 1.0) == pa.TTS_MIN_S
    words = " ".join(["w"] * 26)
    assert pa._estimate_duration(words, 1.0) == pytest.approx(26 / 2.6 + 0.9)
    # slow rates are floored at half speed
    assert pa._estimate_duration(words, 0.1) == pytest.approx(26 / 1.3 + 0.9)
    assert pa._estimate_duration(" ".join(["w"] * 500), 1.0) == pa.TTS_MAX_S
    assert pa._estimate_duration("", 1.0) == pa.TTS_MIN_S


def test_tts_options_and_entity(pa):
    assert pa._tts_options({"speech_rate": 0.9666}) == {"rate": 0.97}
    assert pa._resolve_tts_entity(_runtime()) == "tts.piper"
    assert pa._resolve_tts_entity(_runtime(proactive_tts_entity="tts.cloud")) == "tts.cloud"
    assert pa._resolve_tts_entity(_runtime(proactive_tts_entity="")) == "tts.piper"


def test_constants(pa):
    assert (pa.SERVICE_SPEAK, pa.SERVICE_PROCESS_INTENT) == ("speak", "process_intent")
    assert pa.AUDIT_AREA_KEY == "infrastructure_audit_area"
    assert pa.AUDIT_SENSORS_KEY == "infrastructure_audit_sensors"
    assert pa.AUDIT_INTERVAL.total_seconds() == 900
    assert pa.AUDIT_STARTUP_DELAY.total_seconds() == 60
    assert pa.PREDICTOR_AUTOEXECUTE is False
    assert pa.DEFAULT_ANNOUNCE_PLAYER == "" and pa.MEDIA_DUCK_LEVEL == 0.10


def test_schemas(pa):
    assert pa.SPEAK_SCHEMA({"message": "hi", "target_area": "a"}) == {
        "message": "hi", "target_area": "a", "critical": False, "expect_response": False}
    with pytest.raises(vol.Invalid):
        pa.SPEAK_SCHEMA({"message": "hi"})
    assert pa.PROCESS_INTENT_SCHEMA({"phrase": "p", "target_area": "a", "user_id": "u"}) == {
        "phrase": "p", "target_area": "a", "user_id": "u"}


def test_history_phrase(pa):
    assert pa._history_phrase([], "sir") == ""
    assert pa._history_phrase([{}], "sir") == " For context, Sir, this has occurred once before."
    assert pa._history_phrase([{}, {}, {}], "") == (
        " For context, this has occurred 3 times before.")


async def test_honorific_falls_back_to_the_setting(hass, pa, cfg, monkeypatch):
    from custom_components.nova import honorific
    monkeypatch.setattr(honorific, "effective_honorific", lambda hass: "ma'am")
    assert pa._resolve_honorific(hass, None) == "ma'am"

    def broken(hass):
        raise RuntimeError("no presence")
    monkeypatch.setattr(honorific, "effective_honorific", broken)
    cfg["honorific"] = "boss"
    assert pa._resolve_honorific(hass, None) == "boss"


# ── speakers ────────────────────────────────────────────────────────────────

async def test_broadcast_speakers(hass, pa, cfg):
    hass.states.async_set("media_player.a", "idle")
    hass.states.async_set("media_player.grp", "idle")
    assert pa._resolve_broadcast_speakers(hass, _runtime(announcement_speakers=[
        "media_player.a", "media_player.gone"])) == ["media_player.a"]
    # nothing configured: silent
    assert pa._resolve_broadcast_speakers(hass, _runtime()) == []
    MockConfigEntry(domain="nova").add_to_hass(hass)
    cfg["broadcast_group"] = "media_player.grp"
    assert pa._resolve_broadcast_speakers(hass, _runtime(announcement_speakers=[
        "media_player.gone"])) == ["media_player.grp"]


async def test_targets_room_then_general_then_broadcast(hass, pa, cfg):
    for eid in ("media_player.den", "media_player.gen", "media_player.a",
                "assist_satellite.s"):
        hass.states.async_set(eid, "idle")
    runtime = _runtime(announcement_speakers=["assist_satellite.s", "media_player.a"])
    assert pa._resolve_targets(hass, runtime, "den") == (["media_player.a"], "broadcast")
    cfg["general_speaker"] = "media_player.gen"
    assert pa._resolve_targets(hass, runtime, "den") == (["media_player.gen"], "area")
    cfg["room_speakers"] = {"den": "media_player.den"}
    assert pa._resolve_targets(hass, runtime, "den") == (["media_player.den"], "area")
    cfg["general_speaker"] = ""
    assert pa._resolve_targets(hass, _runtime(announcement_speakers=[
        "assist_satellite.s"]), "hall") == ([], "none")


# ── telemetry ───────────────────────────────────────────────────────────────

@pytest.fixture
def quiet_engines(pa, monkeypatch):
    seen = {}

    class Spatial:
        def __init__(self, hass):
            pass

        def evaluate(self, area_id):
            seen["area"] = area_id
            return {"skip_preamble": True, "confidence": 0.7}

    class Gate:
        def __init__(self, hass):
            pass

        def compensated_db(self, raw):
            seen["raw_db"] = raw
            return None if raw is None else raw - 5

    monkeypatch.setattr(pa, "SpatialContextEngine", Spatial)
    monkeypatch.setattr(pa, "NoiseGate", Gate)
    monkeypatch.setattr(pa.delivery, "in_quiet_hours", lambda hass: "quiet?")
    return seen


async def test_telemetry_reads_the_area(hass, pa, quiet_engines):
    office = ar.async_get(hass).async_create("Office")
    reg = er.async_get(hass)
    for uid, eid in (("1", "sensor.desk_lux"), ("2", "sensor.window_light"),
                     ("3", "sensor.room_noise"), ("4", "sensor.mic_level"),
                     ("5", "sensor.temp"), ("6", "sensor.hall_lux"),
                     ("7", "sensor.bad_illuminance")):
        entry = reg.async_get_or_create("sensor", "test", uid, suggested_object_id=eid[7:])
        if eid != "sensor.hall_lux":
            reg.async_update_entity(entry.entity_id, area_id=office.id)
    hass.states.async_set("sensor.desk_lux", "120")
    hass.states.async_set("sensor.window_light", "40", {"device_class": "illuminance"})
    hass.states.async_set("sensor.room_noise", "48")
    hass.states.async_set("sensor.mic_level", "61", {"device_class": "sound_pressure"})
    hass.states.async_set("sensor.temp", "21", {"device_class": "temperature"})
    hass.states.async_set("sensor.hall_lux", "5")
    hass.states.async_set("sensor.bad_illuminance", "unknown")
    hass.states.async_set("media_player.a", "playing")
    hass.states.async_set("media_player.b", "idle")

    out = pa._build_telemetry(hass, office.id, ["media_player.b", "media_player.a"], True)
    assert out == {"critical_alert": True, "ambient_lux": 40.0, "ambient_db": 56.0,
                   "media_active": True, "skip_preamble": True, "spatial_confidence": 0.7,
                   "quiet_hours": "quiet?"}
    assert quiet_engines == {"area": office.id, "raw_db": 61.0}


async def test_telemetry_with_nothing_in_the_area(hass, pa, quiet_engines):
    hass.states.async_set("media_player.a", "PLAYING")
    out = pa._build_telemetry(hass, "none", ["media_player.a", "media_player.gone"], False)
    assert out["ambient_lux"] is None and out["ambient_db"] is None
    assert out["media_active"] is True and out["critical_alert"] is False


# ── speaking ────────────────────────────────────────────────────────────────

async def test_speak_tts_with_options_then_without(hass, pa, calls, monkeypatch):
    from custom_components.nova import runtime as rt
    monkeypatch.setattr(rt, "domain_runtime_config", lambda hass: {})
    hass.states.async_set("media_player.tv", "on", {"device_class": "tv"})
    runtime = _runtime(proactive_tts_entity="tts.x")
    got = await pa._speak_tts(hass, runtime, ["media_player.a", "media_player.tv"], "Hi",
                              {"speech_rate": 1.15})
    assert got == ["media_player.a"]
    assert calls["tts"] == [{"entity_id": "tts.x", "media_player_entity_id": ["media_player.a"],
                             "message": "Hi", "options": {"rate": 1.15}}]
    calls["reject_options"] = True
    got = await pa._speak_tts(hass, runtime, ["media_player.a"], "Again", {"speech_rate": 1})
    assert got == ["media_player.a"]
    assert calls["tts"][-1] == {"entity_id": "tts.x",
                                "media_player_entity_id": ["media_player.a"],
                                "message": "Again"}


async def test_speak_tts_with_only_televisions_says_nothing(hass, pa, calls, monkeypatch):
    from custom_components.nova import runtime as rt
    monkeypatch.setattr(rt, "domain_runtime_config", lambda hass: {})
    hass.states.async_set("media_player.tv", "on", {"device_class": "tv"})
    assert await pa._speak_tts(hass, _runtime(), ["media_player.tv"], "Hi",
                               {"speech_rate": 1}) == []
    assert calls["tts"] == []


@pytest.fixture
def announce_env(hass, pa, cfg, calls, monkeypatch):
    """_announce with a fixed voice profile, no waiting and recorded history."""
    from custom_components.nova import runtime as rt, spoken_history
    monkeypatch.setattr(rt, "domain_runtime_config", lambda hass: {})
    profile = {"volume": 0.6, "speech_rate": 1.0, "style": "neutral", "duck_media": False}
    telemetry = []
    monkeypatch.setattr(pa, "_build_telemetry",
                        lambda hass, area, targets, critical: telemetry.append(
                            (area, list(targets), critical)) or {"t": 1})
    monkeypatch.setattr(pa, "_PROSODY", types.SimpleNamespace(
        calculate_vocal_profile=lambda t: dict(profile)))
    slept = []

    async def nap(seconds):
        slept.append(seconds)
    monkeypatch.setattr(pa, "asyncio", types.SimpleNamespace(sleep=nap))
    history = []
    monkeypatch.setattr(spoken_history, "record", lambda *a: history.append(a))
    cfg["room_speakers"] = {"den": "media_player.den"}
    cfg["general_speaker"] = ""
    return types.SimpleNamespace(profile=profile, telemetry=telemetry, slept=slept,
                                 history=history, calls=calls)


async def test_announce_ducks_speaks_and_restores(hass, pa, announce_env):
    hass.states.async_set("media_player.den", "playing", {"volume_level": 0.3})
    ok = await pa._announce(hass, _runtime(), "Two words", "den", True)
    assert ok is True
    assert announce_env.telemetry == [("den", ["media_player.den"], True)]
    assert announce_env.calls["volume"] == [("media_player.den", 0.6),
                                            ("media_player.den", 0.3)]
    assert announce_env.calls["tts"][0]["message"] == "Two words"
    assert announce_env.slept == [pa._estimate_duration("Two words", 1.0)]
    assert announce_env.history == [("Two words", "manual", ["media_player.den"], None)]


async def test_announce_without_a_volume_reading_leaves_volume_alone(hass, pa, announce_env):
    hass.states.async_set("media_player.den", "idle")
    announce_env.profile["duck_media"] = True
    assert await pa._announce(hass, _runtime(), "Hi", "den", False) is True
    assert announce_env.calls["volume"] == []


async def test_announce_clamps_the_volume(hass, pa, announce_env):
    hass.states.async_set("media_player.den", "idle", {"volume_level": 0.5})
    announce_env.profile["volume"] = 1.7
    await pa._announce(hass, _runtime(), "Hi", "den", False)
    assert announce_env.calls["volume"][0] == ("media_player.den", 1.0)


async def test_announce_failure_still_restores(hass, pa, announce_env):
    hass.states.async_set("media_player.den", "idle", {"volume_level": 0.2})
    announce_env.calls["fail_tts"] = RuntimeError("engine down")
    assert await pa._announce(hass, _runtime(), "Hi", "den", False) is False
    assert announce_env.calls["volume"][-1] == ("media_player.den", 0.2)
    assert announce_env.history == []


async def test_announce_with_no_speaker(hass, pa, announce_env):
    assert await pa._announce(hass, _runtime(), "Hi", "attic", False) is False
    assert announce_env.telemetry == [] and announce_env.calls["tts"] == []


async def test_history_failure_keeps_the_announcement(hass, pa, announce_env, monkeypatch):
    from custom_components.nova import spoken_history

    def broken(*a):
        raise RuntimeError("db locked")
    monkeypatch.setattr(spoken_history, "record", broken)
    hass.states.async_set("media_player.den", "idle")
    assert await pa._announce(hass, _runtime(), "Hi", "den", False) is True


# ── audit helpers ───────────────────────────────────────────────────────────

async def test_predictor_records_occupied_areas_and_logs(hass, pa, monkeypatch, caplog):
    from custom_components.nova import audio_routing
    monkeypatch.setattr(audio_routing, "currently_occupied_areas", lambda hass: ["den", "hall"])
    recorded = []
    predictor = types.SimpleNamespace(
        record_event=recorded.append,
        due_preemptions=lambda: [{"key": "light.den", "probability": 0.8}])
    with caplog.at_level("INFO"):
        await pa._run_predictor(hass, predictor)
    assert recorded == ["den_entry", "hall_entry"]
    assert "Predictor suggestion: light.den likely soon (p=0.80)" in caplog.text

    def broken(hass):
        raise RuntimeError("no sensors")
    monkeypatch.setattr(audio_routing, "currently_occupied_areas", broken)
    recorded.clear()
    await pa._run_predictor(hass, predictor)
    assert recorded == []


async def test_ledger_reconcile_reports_mismatches_and_compacts(hass, pa, caplog):
    hass.states.async_set("lock.a", "locked")
    hass.states.async_set("lock.b", "unlocked")
    compacted = []
    ledger = types.SimpleNamespace(
        pending_intents=lambda: [
            {"entity_id": "lock.a", "desired_state": "locked"},
            {"entity_id": "lock.b", "desired_state": "locked"},
            {"entity_id": "lock.gone", "desired_state": "locked"}],
        compact=lambda: compacted.append(True))
    runtime = types.SimpleNamespace(state_ledger=ledger)
    with caplog.at_level("WARNING"):
        out = await pa._reconcile_state_ledger(hass, runtime)
    assert out == [{"entity_id": "lock.b", "desired_state": "locked", "actual": "unlocked"},
                   {"entity_id": "lock.gone", "desired_state": "locked", "actual": None}]
    assert compacted == [True]
    assert "lock.b was meant to be 'locked'" in caplog.text


async def test_deliver_audit(hass, pa, monkeypatch):
    sent, said = [], []

    async def deliver(hass, req, speak=None):
        sent.append(req)
        said.append(await speak("words"))
    monkeypatch.setattr(pa.delivery, "async_deliver", deliver)

    async def announce(hass, runtime, message, area_id, critical):
        return (message, area_id, critical)
    monkeypatch.setattr(pa, "_announce", announce)
    monkeypatch.setattr(pa, "_resolve_area_id", lambda hass, t: "den" if t == "Den" else None)
    await pa._deliver_audit(hass, _runtime(), "Disk full", "Den", True)
    await pa._deliver_audit(hass, _runtime(), "Disk low", "Attic", False)
    assert [(r.source, r.message, r.category, r.urgency) for r in sent] == [
        ("infrastructure", "Disk full", "infrastructure", "critical"),
        ("infrastructure", "Disk low", "infrastructure", "high")]
    assert said == [("words", "den", True), False]


async def test_dispatch_speak_opens_a_feedback_window(hass, pa, monkeypatch):
    spoken, windows = [], []

    async def announce(hass, runtime, message, area_id, critical):
        spoken.append((message, area_id, critical))
    monkeypatch.setattr(pa, "_announce", announce)
    monkeypatch.setattr(pa, "_resolve_area_id", lambda hass, t: None if t == "x" else t)

    async def window(payload):
        windows.append(payload)
    router = types.SimpleNamespace(open_feedback_window=window)
    monkeypatch.setattr(pa, "_intent_router", lambda hass, runtime: router)

    await pa._dispatch_speak(hass, _runtime(), {"message": "m", "target_area": "x"})
    assert spoken == []
    await pa._dispatch_speak(hass, _runtime(), {"message": "m", "target_area": "den",
                                                "user_id": "u", "critical": True,
                                                "expect_response": True})
    assert spoken == [("m", "den", True)] and windows == []
    await pa._dispatch_speak(hass, _runtime(), {"message": "m", "target_area": "den",
                                                "expect_response": True,
                                                "confirm_intent": "lock_up"})
    assert windows == [{"intent": "lock_up", "area": "den"}]


async def test_dispatch_speak_survives_a_failed_window(hass, pa, monkeypatch):
    async def announce(*a):
        return True
    monkeypatch.setattr(pa, "_announce", announce)
    monkeypatch.setattr(pa, "_resolve_area_id", lambda hass, t: t)

    async def broken(payload):
        raise RuntimeError("router down")
    monkeypatch.setattr(pa, "_intent_router", lambda hass, runtime: types.SimpleNamespace(
        open_feedback_window=broken))
    await pa._dispatch_speak(hass, _runtime(), {"message": "m", "target_area": "den",
                                                "expect_response": True,
                                                "confirm_intent": "i"})
