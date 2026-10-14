"""Which speaker Nova talks through: area lookup, room and general speakers,
presence, the reply target, observer urgency routing, broadcasts and the
television guard."""
import types

import pytest


@pytest.fixture
def cfg(load, monkeypatch):
    """Saved settings read through nova_config.get, plus the notify only flag."""
    values = {}
    nc = load("nova_config")
    monkeypatch.setattr(nc, "get", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(nc, "announce_notify_only", lambda hass: values.get("_notify_only", False))
    return values


@pytest.fixture
def excluded(load, monkeypatch):
    ids = set()
    monkeypatch.setattr(load("entity_filter"), "is_excluded", lambda hass, eid: eid in ids)
    return ids


@pytest.fixture
def ar(load, cfg, excluded):
    return load("audio_routing")


@pytest.fixture
def areas(ar, monkeypatch):
    """entity_id -> area_id, served through entity_area."""
    where = {}
    monkeypatch.setattr(ar, "entity_area", lambda hass, eid: where.get(eid))
    return where


def _registries(ar, monkeypatch, entities, devices):
    ents = {eid: types.SimpleNamespace(entity_id=eid, domain=eid.split(".")[0], **kw)
            for eid, kw in entities.items()}
    devs = {did: types.SimpleNamespace(area_id=a) for did, a in devices.items()}
    monkeypatch.setattr(ar.er, "async_get", lambda hass: types.SimpleNamespace(
        entities=ents, async_get=ents.get))
    monkeypatch.setattr(ar.dr, "async_get", lambda hass: types.SimpleNamespace(
        async_get=devs.get))


# ── entity_area ─────────────────────────────────────────────────────────────

def test_area_from_entity_then_device(ar, fake_hass, monkeypatch):
    _registries(ar, monkeypatch, {
        "light.a": {"area_id": "kitchen", "device_id": "d1"},
        "light.b": {"area_id": None, "device_id": "d1"},
        "light.c": {"area_id": None, "device_id": "d2"},
        "light.d": {"area_id": None, "device_id": None},
        "light.e": {"area_id": None, "device_id": "missing"},
    }, {"d1": "hall", "d2": None})
    assert ar.entity_area(fake_hass, "light.a") == "kitchen"
    assert ar.entity_area(fake_hass, "light.b") == "hall"
    assert ar.entity_area(fake_hass, "light.c") is None
    assert ar.entity_area(fake_hass, "light.d") is None
    assert ar.entity_area(fake_hass, "light.e") is None
    assert ar.entity_area(fake_hass, "light.unknown") is None


def test_area_lookup_failure_is_none(ar, fake_hass, monkeypatch):
    def broken(hass):
        raise RuntimeError("registry down")
    monkeypatch.setattr(ar.er, "async_get", broken)
    assert ar.entity_area(fake_hass, "light.a") is None


def test_entities_by_domain(ar, fake_hass):
    fake_hass.states.set("light.a", "on")
    fake_hass.states.set("switch.b", "on")
    assert ar._entities_by_domain(fake_hass, "light") == ["light.a"]


# ── satellites and configured speakers ──────────────────────────────────────

def test_satellites_in_area(ar, fake_hass, areas):
    for eid in ("assist_satellite.a", "assist_satellite.b", "assist_satellite.c"):
        fake_hass.states.set(eid, "idle")
    areas.update({"assist_satellite.a": "den", "assist_satellite.b": "hall",
                  "assist_satellite.c": "den"})
    assert ar.satellites_in_area(fake_hass, "den") == ["assist_satellite.a",
                                                       "assist_satellite.c"]
    assert ar.all_areas_with_satellite(fake_hass) == {"den", "hall"}


def test_satellite_without_area_is_not_counted(ar, fake_hass, areas):
    fake_hass.states.set("assist_satellite.a", "idle")
    assert ar.all_areas_with_satellite(fake_hass) == set()


def test_room_speaker_must_be_assigned_and_exist(ar, fake_hass, cfg):
    fake_hass.states.set("media_player.den", "idle")
    cfg["room_speakers"] = {"den": "media_player.den", "hall": "media_player.gone",
                            "attic": ""}
    assert ar.room_speaker(fake_hass, "den") == "media_player.den"
    assert ar.room_speaker(fake_hass, "hall") is None
    assert ar.room_speaker(fake_hass, "attic") is None
    assert ar.room_speaker(fake_hass, "cellar") is None
    cfg["room_speakers"] = None
    assert ar.room_speaker(fake_hass, "den") is None


def test_speaker_settings_that_fail_to_read_count_as_unset(ar, load, fake_hass, monkeypatch):
    fake_hass.states.set("media_player.x", "idle")

    def broken(key, default=None):
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(load("nova_config"), "get", broken)
    assert ar.room_speaker(fake_hass, "den") is None
    assert ar.general_speaker_target(fake_hass) is None


def test_general_speaker(ar, fake_hass, cfg):
    assert ar.general_speaker_target(fake_hass) is None
    cfg["general_speaker"] = "media_player.gone"
    assert ar.general_speaker_target(fake_hass) is None
    fake_hass.states.set("media_player.gen", "idle")
    cfg["general_speaker"] = "media_player.gen"
    assert ar.general_speaker_target(fake_hass) == "media_player.gen"
    cfg["general_speaker"] = None
    assert ar.general_speaker_target(fake_hass) is None


# ── television guard ────────────────────────────────────────────────────────

def test_display_target(ar, fake_hass):
    fake_hass.states.set("media_player.tv", "on", device_class="tv")
    fake_hass.states.set("media_player.spk", "on", device_class="speaker")
    assert ar._is_display_target(fake_hass, "media_player.tv", "")
    assert ar._is_display_target(fake_hass, "media_player.film", "media_player.film")
    assert not ar._is_display_target(fake_hass, "media_player.spk", "")
    assert not ar._is_display_target(fake_hass, "media_player.absent", "")
    assert not ar._is_display_target(fake_hass, "", "")


def test_drop_display_targets(ar, load, fake_hass, monkeypatch, caplog):
    monkeypatch.setattr(load("runtime"), "domain_runtime_config",
                        lambda hass: {"movie_media_player": "media_player.film"})
    fake_hass.states.set("media_player.tv", "on", device_class="tv")
    fake_hass.states.set("media_player.spk", "on")
    with caplog.at_level("WARNING"):
        kept = ar.drop_display_targets(
            fake_hass, ["media_player.spk", "media_player.tv", "media_player.film"], "test")
    assert kept == ["media_player.spk"]
    assert "media_player.tv" in caplog.text and "context=test" in caplog.text


def test_drop_display_targets_quiet_when_nothing_dropped(ar, load, fake_hass,
                                                          monkeypatch, caplog):
    monkeypatch.setattr(load("runtime"), "domain_runtime_config", lambda hass: {})
    with caplog.at_level("WARNING"):
        assert ar.drop_display_targets(fake_hass, None) == []
        assert ar.drop_display_targets(fake_hass, ("media_player.a",)) == ["media_player.a"]
    assert "refused" not in caplog.text


# ── presence ────────────────────────────────────────────────────────────────

def _sensors(fake_hass, areas):
    fake_hass.states.set("binary_sensor.den_occ", "on", device_class="occupancy")
    fake_hass.states.set("binary_sensor.den_motion", "off", device_class="motion")
    fake_hass.states.set("binary_sensor.hall_pres", "off", device_class="presence")
    fake_hass.states.set("binary_sensor.den_door", "on", device_class="door")
    fake_hass.states.set("binary_sensor.drive_car_occupancy", "on", device_class="occupancy")
    fake_hass.states.set("binary_sensor.loft_occ", "on", device_class="occupancy")
    fake_hass.states.set("binary_sensor.nowhere", "on", device_class="motion")
    areas.update({"binary_sensor.den_occ": "den", "binary_sensor.den_motion": "den",
                  "binary_sensor.hall_pres": "hall", "binary_sensor.den_door": "den",
                  "binary_sensor.drive_car_occupancy": "drive",
                  "binary_sensor.loft_occ": "loft"})


def test_presence_entities_skip_excluded_objects_and_other_classes(ar, fake_hass, areas,
                                                                     excluded):
    _sensors(fake_hass, areas)
    excluded.add("binary_sensor.loft_occ")
    assert ar.presence_entities_in_area(fake_hass, "den") == [
        "binary_sensor.den_occ", "binary_sensor.den_motion"]
    assert ar.presence_entities_in_area(fake_hass, "drive") == []
    assert ar.presence_entities_in_area(fake_hass, "loft") == []
    assert ar.all_areas_with_presence(fake_hass) == {"den", "hall"}


def test_occupancy(ar, fake_hass, areas):
    _sensors(fake_hass, areas)
    assert ar.is_area_occupied(fake_hass, "den")
    assert not ar.is_area_occupied(fake_hass, "hall")
    assert sorted(ar.currently_occupied_areas(fake_hass)) == ["den", "loft"]


@pytest.mark.parametrize("value", ["on", "Home", "detected", "TRUE", "occupied"])
def test_occupied_words(ar, fake_hass, areas, value):
    fake_hass.states.set("binary_sensor.x", value, device_class="motion")
    areas["binary_sensor.x"] = "den"
    assert ar.is_area_occupied(fake_hass, "den")


def test_bedroom_occupancy(ar, fake_hass, areas):
    _sensors(fake_hass, areas)
    assert ar.is_any_bedroom_occupied(fake_hass, ["hall", "den", "loft"]) == (True, "den")
    assert ar.is_any_bedroom_occupied(fake_hass, ["hall"]) == (False, None)
    assert ar.is_any_bedroom_occupied(fake_hass, []) == (False, None)


def test_anyone_home_from_any_sensor_or_tracker(ar, fake_hass):
    assert not ar.anyone_home(fake_hass)
    fake_hass.states.set("binary_sensor.drive_car_occupancy", "on", device_class="occupancy")
    fake_hass.states.set("binary_sensor.door", "on", device_class="door")
    fake_hass.states.set("person.a", "not_home")
    assert not ar.anyone_home(fake_hass)
    fake_hass.states.set("binary_sensor.m", "Detected", device_class="motion")
    assert ar.anyone_home(fake_hass)


@pytest.mark.parametrize("entity", ["person.a", "device_tracker.phone"])
def test_anyone_home_from_people(ar, fake_hass, entity):
    fake_hass.states.set(entity, "HOME")
    assert ar.anyone_home(fake_hass)


def test_anyone_home_skips_excluded_sensors(ar, fake_hass, excluded):
    fake_hass.states.set("binary_sensor.m", "on", device_class="motion")
    excluded.add("binary_sensor.m")
    assert not ar.anyone_home(fake_hass)
    fake_hass.states.set("binary_sensor.n", "on", device_class="motion")
    assert ar.anyone_home(fake_hass)


# ── reply target ────────────────────────────────────────────────────────────

def test_reply_pairing_wins_when_it_exists(ar, fake_hass, areas, cfg):
    fake_hass.states.set("media_player.paired", "idle")
    fake_hass.states.set("media_player.den", "idle")
    cfg["room_speakers"] = {"den": "media_player.den"}
    areas["assist_satellite.s"] = "den"
    assert ar.reply_target(fake_hass, satellite_entity_id="assist_satellite.s",
                           satellite_pairings={"assist_satellite.s": "media_player.paired"}
                           ) == "media_player.paired"
    # a paired speaker that has gone falls through to the room speaker
    assert ar.reply_target(fake_hass, satellite_entity_id="assist_satellite.s",
                           satellite_pairings={"assist_satellite.s": "media_player.gone"}
                           ) == "media_player.den"
    assert ar.reply_target(fake_hass, satellite_entity_id="assist_satellite.s",
                           satellite_pairings={"assist_satellite.s": ""}) == "media_player.den"


def test_reply_room_then_general_then_satellite(ar, fake_hass, areas, cfg):
    areas["assist_satellite.s"] = "den"
    assert ar.reply_target(fake_hass, satellite_entity_id="assist_satellite.s") == (
        "assist_satellite.s")
    fake_hass.states.set("media_player.gen", "idle")
    cfg["general_speaker"] = "media_player.gen"
    assert ar.reply_target(fake_hass, satellite_entity_id="assist_satellite.s") == (
        "media_player.gen")
    fake_hass.states.set("media_player.den", "idle")
    cfg["room_speakers"] = {"den": "media_player.den"}
    assert ar.reply_target(fake_hass, satellite_entity_id="assist_satellite.s") == (
        "media_player.den")


def test_reply_without_area_speaks_through_the_satellite(ar, fake_hass, areas, cfg):
    fake_hass.states.set("media_player.gen", "idle")
    cfg["general_speaker"] = "media_player.gen"
    assert ar.reply_target(fake_hass, satellite_entity_id="assist_satellite.s") == (
        "assist_satellite.s")
    assert ar.reply_target(fake_hass) is None


def test_reply_from_a_device(ar, fake_hass, monkeypatch, cfg):
    _registries(ar, monkeypatch, {
        "sensor.other": {"device_id": "dev"},
        "assist_satellite.s": {"device_id": "dev"},
    }, {"dev": "den"})
    fake_hass.states.set("media_player.den", "idle")
    fake_hass.states.set("media_player.paired", "idle")
    cfg["room_speakers"] = {"den": "media_player.den"}
    assert ar.reply_target(fake_hass, device_id="dev") == "media_player.den"
    # the device's satellite is found for its pairing
    assert ar.reply_target(fake_hass, device_id="dev", satellite_pairings={
        "assist_satellite.s": "media_player.paired"}) == "media_player.paired"


def test_device_reply_falls_back_to_its_satellite(ar, fake_hass, monkeypatch):
    _registries(ar, monkeypatch, {"assist_satellite.s": {"device_id": "dev"}}, {"dev": "den"})
    assert ar.reply_target(fake_hass, device_id="dev") == "assist_satellite.s"
    _registries(ar, monkeypatch, {"assist_satellite.s": {"device_id": "dev"}}, {})
    assert ar.reply_target(fake_hass, device_id="dev") == "assist_satellite.s"


def test_reply_from_an_unknown_or_broken_device(ar, fake_hass, monkeypatch):
    _registries(ar, monkeypatch, {}, {})
    assert ar.reply_target(fake_hass, device_id="nope") is None

    def broken(hass):
        raise RuntimeError("registry down")
    monkeypatch.setattr(ar.dr, "async_get", broken)
    assert ar.reply_target(fake_hass, device_id="dev") is None


def test_reply_targets_shim(ar, fake_hass, monkeypatch):
    monkeypatch.setattr(ar, "reply_target", lambda hass, **kw: "media_player.r"
                        if kw["device_id"] == "dev" else None)
    monkeypatch.setattr(ar, "broadcast_target", lambda hass: ["media_player.b"])
    assert ar.reply_targets(fake_hass, device_id="dev") == ["media_player.r"]
    assert ar.reply_targets(fake_hass, device_id="other") == ["media_player.b"]
    # a reply that resolves only to the satellite still goes to the broadcast speakers
    monkeypatch.setattr(ar, "reply_target", lambda hass, **kw: "assist_satellite.s")
    assert ar.reply_targets(fake_hass, device_id="dev") == ["media_player.b"]
    assert ar.reply_targets(fake_hass, voice_satellites=["x"], reply_speakers=["y"],
                            broadcast_speakers=["z"], legacy_cast_speakers=["w"],
                            room_routing=False) == ["media_player.b"]


def test_reply_targets_passes_pairings(ar, fake_hass, monkeypatch):
    seen = {}
    monkeypatch.setattr(ar, "reply_target", lambda hass, **kw: seen.update(kw) or "x")
    ar.reply_targets(fake_hass, device_id="dev", satellite_pairings={"a": "b"})
    assert seen == {"device_id": "dev", "satellite_pairings": {"a": "b"}}


# ── broadcast ───────────────────────────────────────────────────────────────

def test_broadcast_target(ar, fake_hass):
    fake_hass.states.set("media_player.a", "idle")
    fake_hass.states.set("media_player.grp", "idle")
    assert ar.broadcast_target(fake_hass) == []
    assert ar.broadcast_target(fake_hass, broadcast_group="media_player.grp") == [
        "media_player.grp"]
    assert ar.broadcast_target(fake_hass, broadcast_group="media_player.gone") == []
    assert ar.broadcast_target(fake_hass, announcement_speakers=[
        "media_player.a", "", "media_player.gone"], broadcast_group="media_player.grp") == [
        "media_player.a"]
    assert ar.broadcast_target(fake_hass, announcement_speakers=["media_player.gone"],
                               broadcast_group="media_player.grp") == ["media_player.grp"]
    assert ar.broadcast_target(fake_hass, announcement_speakers=("media_player.a",)) == [
        "media_player.a"]


def test_broadcast_speaker_text_forms(ar, fake_hass):
    fake_hass.states.set("media_player.a", "idle")
    fake_hass.states.set("media_player.b", "idle")
    assert ar.broadcast_target(fake_hass, announcement_speakers='["media_player.a"]') == [
        "media_player.a"]
    assert ar.broadcast_target(
        fake_hass, announcement_speakers=" media_player.a, ,media_player.b ") == [
        "media_player.a", "media_player.b"]
    # JSON that is not a list is ignored
    assert ar.broadcast_target(fake_hass, announcement_speakers='{"a": 1}') == []
    assert ar.broadcast_target(fake_hass, announcement_speakers=5) == []


# ── observer routing ────────────────────────────────────────────────────────

@pytest.fixture
def house(ar, fake_hass, cfg, monkeypatch):
    """A house with speakers and controllable occupancy."""
    state = {"occupied": [], "home": True}
    monkeypatch.setattr(ar, "currently_occupied_areas", lambda hass: list(state["occupied"]))
    monkeypatch.setattr(ar, "anyone_home", lambda hass: state["home"])
    for eid in ("media_player.den", "media_player.gen", "media_player.grp",
                "media_player.a", "media_player.b"):
        fake_hass.states.set(eid, "idle")
    return state


def _route(ar, fake_hass, **kw):
    return ar._observer_route(fake_hass, **kw)


def test_critical_broadcasts_even_when_asleep(ar, fake_hass, house):
    assert _route(ar, fake_hass, urgency="critical", is_sleeping=True,
                  announcement_speakers=["media_player.a", "media_player.gone"],
                  broadcast_group="media_player.grp") == (["media_player.a"], "broadcast")
    assert _route(ar, fake_hass, urgency="critical", announcement_speakers=["media_player.x"],
                  broadcast_group="media_player.grp") == (["media_player.grp"], "broadcast")
    assert _route(ar, fake_hass, urgency="critical",
                  broadcast_group="media_player.gone") == ([], "notify_only")


def test_high(ar, fake_hass, house, cfg):
    kw = {"urgency": "high", "broadcast_group": "media_player.grp"}
    assert _route(ar, fake_hass, is_sleeping=True, **kw) == ([], "notify_only")
    house["home"] = False
    assert _route(ar, fake_hass, **kw) == ([], "notify_only")
    house["home"] = True
    assert _route(ar, fake_hass, **kw) == (["media_player.grp"], "broadcast")


def test_high_without_broadcast_speakers_uses_rooms_then_general(ar, fake_hass, house, cfg):
    house["occupied"] = ["den", "hall"]
    assert _route(ar, fake_hass, urgency="high") == ([], "notify_only")
    cfg["general_speaker"] = "media_player.gen"
    assert _route(ar, fake_hass, urgency="high") == (["media_player.gen"], "broadcast")
    cfg["room_speakers"] = {"den": "media_player.den", "hall": "media_player.a"}
    assert _route(ar, fake_hass, urgency="high") == (
        ["media_player.den", "media_player.a"], "broadcast")


def test_medium(ar, fake_hass, house, cfg):
    assert _route(ar, fake_hass, urgency="medium", is_sleeping=True) == ([], "suppressed")
    house["home"] = False
    assert _route(ar, fake_hass, urgency="medium") == ([], "notify_only")
    house["home"] = True
    # home, nobody in a room, nothing to broadcast on
    assert _route(ar, fake_hass, urgency="medium") == ([], "notify_only")
    assert _route(ar, fake_hass, urgency="medium",
                  broadcast_group="media_player.grp") == (["media_player.grp"], "broadcast")
    # someone in a room with nothing assigned and no general speaker
    house["occupied"] = ["hall"]
    assert _route(ar, fake_hass, urgency="medium",
                  broadcast_group="media_player.grp") == (["media_player.grp"], "broadcast")
    cfg["general_speaker"] = "media_player.gen"
    assert _route(ar, fake_hass, urgency="medium") == (["media_player.gen"], "local")
    house["occupied"] = ["hall", "den"]
    cfg["room_speakers"] = {"den": "media_player.den"}
    assert _route(ar, fake_hass, urgency="medium") == (["media_player.den"], "local")


def test_medium_without_room_presence_skips_the_general_speaker(ar, fake_hass, house, cfg):
    cfg["general_speaker"] = "media_player.gen"
    assert _route(ar, fake_hass, urgency="medium") == ([], "notify_only")


def test_low(ar, fake_hass, house, cfg):
    assert _route(ar, fake_hass, urgency="low", is_sleeping=True) == ([], "suppressed")
    assert _route(ar, fake_hass, urgency="low",
                  broadcast_group="media_player.grp") == ([], "suppressed")
    house["occupied"] = ["hall"]
    assert _route(ar, fake_hass, urgency="low") == ([], "suppressed")
    cfg["general_speaker"] = "media_player.gen"
    assert _route(ar, fake_hass, urgency="low") == (["media_player.gen"], "local")
    house["occupied"] = ["hall", "den"]
    cfg["room_speakers"] = {"den": "media_player.den"}
    assert _route(ar, fake_hass, urgency="low") == (["media_player.den"], "local")


def test_low_ignores_whether_anyone_is_home(ar, fake_hass, house, cfg, monkeypatch):
    def boom(hass):
        raise AssertionError("not consulted")
    house["occupied"] = ["den"]
    cfg["room_speakers"] = {"den": "media_player.den"}
    monkeypatch.setattr(ar, "anyone_home", boom)
    assert _route(ar, fake_hass, urgency="low", authoritative_anyone_home=False) == (
        ["media_player.den"], "local")


def test_unknown_urgency_is_suppressed(ar, fake_hass, house):
    assert _route(ar, fake_hass, urgency="whatever",
                  broadcast_group="media_player.grp") == ([], "suppressed")


def test_authoritative_home_overrides_sensors(ar, fake_hass, house, monkeypatch):
    monkeypatch.setattr(ar, "anyone_home", lambda hass: pytest.fail("re-derived"))
    assert _route(ar, fake_hass, urgency="high", broadcast_group="media_player.grp",
                  authoritative_anyone_home=False) == ([], "notify_only")
    assert _route(ar, fake_hass, urgency="medium", broadcast_group="media_player.grp",
                  authoritative_anyone_home=True) == (["media_player.grp"], "broadcast")


def test_notify_only_turns_spoken_routes_into_notifications(ar, fake_hass, house, cfg):
    cfg["_notify_only"] = True
    assert ar.observer_speak_target(fake_hass, urgency="high",
                                    broadcast_group="media_player.grp") == ([], "notify_only")
    house["occupied"] = ["den"]
    cfg["room_speakers"] = {"den": "media_player.den"}
    assert ar.observer_speak_target(fake_hass, urgency="low") == ([], "notify_only")
    assert ar.observer_speak_target(fake_hass, urgency="low", is_sleeping=True) == (
        [], "suppressed")
    # critical always speaks
    assert ar.observer_speak_target(fake_hass, urgency="critical",
                                    broadcast_group="media_player.grp") == (
        ["media_player.grp"], "broadcast")


def test_observer_speak_target_passes_everything_through(ar, fake_hass, monkeypatch, cfg):
    seen = {}

    def route(hass, **kw):
        seen.update(kw)
        return (["media_player.x"], "local")
    monkeypatch.setattr(ar, "_observer_route", route)
    out = ar.observer_speak_target(fake_hass, urgency="medium", broadcast_group="g",
                                   announcement_speakers=["s"], is_sleeping=False,
                                   authoritative_anyone_home=True)
    assert out == (["media_player.x"], "local")
    assert seen == {"urgency": "medium", "broadcast_group": "g",
                    "announcement_speakers": ["s"], "is_sleeping": False,
                    "authoritative_anyone_home": True}


def test_notify_only_flag(ar, load, fake_hass, cfg, monkeypatch):
    assert ar.notify_only_enabled(fake_hass) is False
    cfg["_notify_only"] = 1
    assert ar.notify_only_enabled(fake_hass) is True

    def broken(hass):
        raise RuntimeError("unreadable")
    monkeypatch.setattr(load("nova_config"), "announce_notify_only", broken)
    assert ar.notify_only_enabled(fake_hass) is False
