"""Broadcast announcements must not be all-or-nothing (v6.78.2), and must
never change a speaker's volume (v7.86.0).

Delivery is one media_player.play_media (announce=true, extra.volume pinned
to that speaker's current volume) call per speaker — not a single batched
tts.speak call — so one bad target (an off TV, a stale Cast entity) can never
take the rest of a broadcast down with it, and no speaker is ever left louder
or quieter than it started. play_media/announce is trusted once the service
call itself succeeds; only an actual exception from that call falls back to
plain tts.speak for that one speaker (a "did it visibly respond" poll was
tried and removed — see tts_helper.async_announce's docstring)."""
import pytest


class _State:
    def __init__(self, entity_id, state, volume_level=None):
        self.entity_id, self.state = entity_id, state
        self.attributes = {} if volume_level is None else {"volume_level": volume_level}
        self.last_updated = 0


class _Hass:
    def __init__(self, players, fail_on=None, fail_batch=False, unresponsive=None,
                 fail_play_media_on=None):
        self._players = players
        self._fail_on = set(fail_on or [])                       # fails BOTH play_media and its tts.speak fallback
        self._fail_play_media_on = set(fail_play_media_on or [])  # fails only play_media, fallback still works
        self._fail_batch = fail_batch          # legacy tts.speak-batch scenarios
        self._unresponsive = set(unresponsive or [])   # play_media that silently no-ops
        self.calls = []                        # list of ("tts.speak"|"play_media", eid_or_targets, data)
        self.services = self
    def async_all(self, domain=None):
        return list(self._players.values())
    class _S: pass
    @property
    def states(self):
        s = _Hass._S()
        s.get = lambda eid: self._players.get(eid)
        s.async_all = lambda domain=None: list(self._players.values())
        return s
    async def async_call(self, domain, service, data, target=None, blocking=False):
        if domain == "tts" and service == "speak":
            targets = data.get("media_player_entity_id") or []
            self.calls.append(("tts.speak", list(targets), dict(data)))
            if self._fail_batch and len(targets) > 1:
                raise RuntimeError("batch rejected")
            for t in targets:
                if t in self._fail_on:
                    raise RuntimeError(f"{t} unavailable")
            return
        if domain == "media_player" and service == "play_media":
            eid = (target or {}).get("entity_id")
            self.calls.append(("play_media", eid, dict(data)))
            if eid in self._fail_on or eid in self._fail_play_media_on:
                raise RuntimeError(f"{eid} unavailable")
            st = self._players.get(eid)
            if st is not None and eid not in self._unresponsive:
                st.last_updated += 1   # simulate the device actually responding
            return
        raise AssertionError(f"unexpected service call: {domain}.{service}")


@pytest.fixture
def tts(load):
    return load("tts_helper")


@pytest.fixture
def routing(load, monkeypatch):
    # audio_routing imports area_registry at module level; the synthetic HA
    # stub doesn't provide it, so supply a minimal one for the load.
    import sys, types
    helpers = sys.modules.get("homeassistant.helpers") or types.ModuleType("homeassistant.helpers")
    ar = types.ModuleType("homeassistant.helpers.area_registry")
    ar.async_get = lambda hass: types.SimpleNamespace(
        async_list_areas=lambda: [], async_get_area=lambda i: None)
    er = types.ModuleType("homeassistant.helpers.entity_registry")
    er.async_get = lambda hass: types.SimpleNamespace(
        entities={}, async_get=lambda e: None)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.area_registry", ar)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", er)
    monkeypatch.setattr(helpers, "area_registry", ar, raising=False)
    monkeypatch.setattr(helpers, "entity_registry", er, raising=False)
    return load("audio_routing")


# ── broadcast target: silent-until-configured (v7.83.0) ─────────────────────

def test_broadcast_silent_until_configured(routing, monkeypatch):
    # Fresh install: nothing configured → [] (never blast every device / TV).
    players = {
        "media_player.kitchen": _State("media_player.kitchen", "idle"),
        "media_player.tv": _State("media_player.tv", "on"),
    }
    hass = _Hass(players)
    monkeypatch.setattr(routing, "_entities_by_domain", lambda h, d: list(players))
    assert routing.broadcast_target(hass) == []


def test_broadcast_uses_broadcast_group(routing):
    hass = _Hass({"media_player.home_group": _State("media_player.home_group", "idle")})
    assert routing.broadcast_target(
        hass, broadcast_group="media_player.home_group") == ["media_player.home_group"]


def test_broadcast_group_missing_falls_silent(routing):
    assert routing.broadcast_target(
        _Hass({}), broadcast_group="media_player.gone") == []


def test_broadcast_uses_announcement_speakers(routing):
    hass = _Hass({
        "media_player.kitchen": _State("media_player.kitchen", "idle"),
        "media_player.den": _State("media_player.den", "idle"),
    })
    out = routing.broadcast_target(
        hass, announcement_speakers=["media_player.kitchen", "media_player.den"])
    assert out == ["media_player.kitchen", "media_player.den"]


def test_broadcast_filters_missing_announcement_speakers(routing):
    hass = _Hass({"media_player.kitchen": _State("media_player.kitchen", "idle")})
    out = routing.broadcast_target(
        hass, announcement_speakers=["media_player.kitchen", "media_player.gone"])
    assert out == ["media_player.kitchen"]


def test_broadcast_announcement_speakers_accepts_json_string(routing):
    # panel/config may store the list as a JSON string
    hass = _Hass({"media_player.kitchen": _State("media_player.kitchen", "idle")})
    out = routing.broadcast_target(
        hass, announcement_speakers='["media_player.kitchen"]')
    assert out == ["media_player.kitchen"]


def test_observer_critical_silent_until_configured(routing, monkeypatch):
    # A critical alert broadcasts — but with nothing configured it must degrade
    # to notify_only, never fall back to every speaker in the house.
    players = {
        "media_player.kitchen": _State("media_player.kitchen", "idle"),
        "media_player.tv": _State("media_player.tv", "on"),
    }
    hass = _Hass(players)
    monkeypatch.setattr(routing, "_entities_by_domain", lambda h, d: list(players))
    targets, mode = routing.observer_speak_target(hass, urgency="critical")
    assert targets == [] and mode == "notify_only"


def test_observer_critical_uses_announcement_speakers(routing):
    hass = _Hass({"media_player.kitchen": _State("media_player.kitchen", "idle")})
    targets, mode = routing.observer_speak_target(
        hass, urgency="critical", announcement_speakers=["media_player.kitchen"])
    assert targets == ["media_player.kitchen"] and mode == "broadcast"


def test_speakers_in_area_excludes_tv_and_movie_player(routing, monkeypatch):
    # A living room with a real speaker, a TV (device_class tv), and a projector
    # designated as the movie player. TTS routing must return only the speaker.
    spk = _State("media_player.living_room_speaker", "idle")
    tv = _State("media_player.samsung_tv", "on"); tv.attributes = {"device_class": "tv"}
    proj = _State("media_player.projector", "idle")   # movie_media_player, no dc
    players = {s.entity_id: s for s in (spk, tv, proj)}
    hass = _Hass(players)
    monkeypatch.setattr(routing, "_entities_by_domain", lambda h, d: list(players))
    monkeypatch.setattr(routing, "entity_area", lambda h, e: "living_room")
    import sys, types
    pkg = routing.__name__.rsplit(".", 1)[0]
    jc = types.SimpleNamespace(
        get=lambda k, d=None: "media_player.projector" if k == "movie_media_player" else d)
    monkeypatch.setitem(sys.modules, f"{pkg}.nova_config", jc)
    assert routing.speakers_in_area(hass, "living_room") == ["media_player.living_room_speaker"]


# ── per-speaker delivery: volume-pinned play_media, with tts.speak fallback ─

async def test_announce_plays_via_play_media_pinned_to_current_volume(tts):
    hass = _Hass({
        "media_player.a": _State("media_player.a", "idle", volume_level=0.3),
        "media_player.b": _State("media_player.b", "idle", volume_level=0.7),
    })
    await tts.async_announce(hass, "hello", "tts.piper",
                             ["media_player.a", "media_player.b"])
    # One play_media call per speaker (no batching), each announcing at that
    # speaker's own current volume — never a different level.
    play_calls = [c for c in hass.calls if c[0] == "play_media"]
    assert [c[1] for c in play_calls] == ["media_player.a", "media_player.b"]
    assert play_calls[0][2]["announce"] is True
    assert play_calls[0][2]["extra"] == {"volume": 0.3}
    assert play_calls[1][2]["extra"] == {"volume": 0.7}
    # No fallback needed — both speakers "responded".
    assert not any(c[0] == "tts.speak" for c in hass.calls)


async def test_unresponsive_speaker_is_trusted_not_double_announced(tts):
    # v5.9.11 added a Cast-group silent-failure guard; v7.86.0-v7.87.0 tried
    # polling for the target to "visibly respond" before trusting it, which
    # is what caused the Sonos double-announcement/volume-jump bug (Sonos
    # plays an announcement without reliably updating any state HA can see,
    # so the poll always timed out and fired the un-pinned tts.speak fallback
    # on top of the announcement that had already played correctly). That
    # poll is gone: a play_media/announce call that doesn't raise is now
    # trusted outright, even if the target never visibly reacts.
    hass = _Hass({
        "media_player.a": _State("media_player.a", "idle", volume_level=0.3),
    }, unresponsive={"media_player.a"})
    ok = await tts.async_announce(hass, "hello", "tts.piper", ["media_player.a"])
    assert ok is True
    assert [c[0] for c in hass.calls] == ["play_media"]
    assert hass.calls[0][1] == "media_player.a"


async def test_play_media_error_falls_back_to_tts_speak(tts):
    hass = _Hass({
        "media_player.a": _State("media_player.a", "idle", volume_level=0.3),
    }, fail_play_media_on={"media_player.a"})
    ok = await tts.async_announce(hass, "hello", "tts.piper", ["media_player.a"])
    assert ok is True
    assert hass.calls[0][0] == "play_media"
    assert hass.calls[1][0] == "tts.speak" and hass.calls[1][1] == ["media_player.a"]


async def test_one_bad_speaker_does_not_silence_the_rest(tts):
    # the real bug this guards against: one dead target used to kill the
    # whole broadcast. Both play_media AND its tts.speak fallback fail for
    # the dead speaker; the good speaker must still play.
    hass = _Hass({
        "media_player.dead": _State("media_player.dead", "idle", volume_level=0.5),
        "media_player.good": _State("media_player.good", "idle", volume_level=0.5),
    }, fail_on={"media_player.dead"})
    ok = await tts.async_announce(hass, "briefing", "tts.piper",
                             ["media_player.dead", "media_player.good"])
    assert ok is True, "at least one speaker must still get the message"
    good_calls = [c for c in hass.calls if c[1] == "media_player.good"]
    assert good_calls, "the working speaker must still play"


async def test_no_speakers_is_a_noop(tts):
    hass = _Hass({})
    await tts.async_announce(hass, "hello", "tts.piper", [])
    assert hass.calls == []


async def test_no_tts_entity_is_a_noop(tts):
    hass = _Hass({})
    await tts.async_announce(hass, "hello", None, ["media_player.a"])
    assert hass.calls == []


def test_drop_display_targets_filters_tv_and_movie(routing, load):
    # The output choke point strips TVs + the movie player from ANY target list,
    # regardless of how they were resolved — the safety net for TV takeovers.
    # movie_media_player is read from runtime_config (hass.data), not nova_config.
    const = load("const")
    spk = _State("media_player.living_room_speaker", "idle")
    tv = _State("media_player.samsung_tv", "on"); tv.attributes = {"device_class": "tv"}
    movie = _State("media_player.projector", "idle")
    players = {s.entity_id: s for s in (spk, tv, movie)}
    hass = _Hass(players)
    hass.data = {const.DOMAIN: {"e1": {"runtime_config": {
        "movie_media_player": "media_player.projector"}}}}
    kept = routing.drop_display_targets(
        hass,
        ["media_player.living_room_speaker", "media_player.samsung_tv", "media_player.projector"],
        "unit-test")
    assert kept == ["media_player.living_room_speaker"]


def test_drop_display_targets_keeps_plain_speakers(routing, load):
    const = load("const")
    hass = _Hass({"media_player.kitchen": _State("media_player.kitchen", "idle"),
                  "media_player.den": _State("media_player.den", "idle")})
    hass.data = {const.DOMAIN: {}}
    kept = routing.drop_display_targets(
        hass, ["media_player.kitchen", "media_player.den"], "unit-test")
    assert kept == ["media_player.kitchen", "media_player.den"]
