"""Who hears an announcement, and how one bad speaker is contained.

Two modules are covered here:

* audio_routing picks the targets. Nothing is spoken through a speaker the
  user never chose (fresh installs stay silent), each room has at most one
  assigned speaker, and a caller's own "is anyone home" answer beats a
  presence sensor that is slow to clear.
* tts_helper.async_announce delivers. Every speaker gets its own
  media_player.play_media call with ``announce`` set (volume held at the
  speaker's current level on Sonos), so a dead TV or stale Cast entity can
  never take the rest of a broadcast down, and no speaker ends up louder or
  quieter than before. A call that returns is trusted; only a call that
  raises falls back to tts.speak for that one speaker. Spoken History gets
  exactly the speakers that took the message.

test_notify_only.py imports ``_Hass``, ``_State``, ``_set_nova_config`` and
the ``routing`` fixture from this file, so their names and call shapes stay.
"""
import asyncio
import sys
import types
import urllib.parse
from typing import NamedTuple

import pytest


# ── stand-ins ───────────────────────────────────────────────────────────────

class _State:
    """A media player's reading. ``volume_level`` lands in attributes only
    when given, as Home Assistant leaves it out for players that never
    report one."""

    def __init__(self, entity_id, state, volume_level=None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = {}
        if volume_level is not None:
            self.attributes["volume_level"] = volume_level
        self.last_updated = 0


class _Call(NamedTuple):
    kind: str        # "play_media" or "tts.speak"
    where: object    # the speaker id, or the list handed to tts.speak
    data: dict
    target: dict
    blocking: bool


class _Hass:
    """Home Assistant cut down to states, service calls and executor jobs.

    ``fail_on`` speakers raise on play_media and on the tts.speak fallback;
    ``fail_play_media_on`` speakers raise on play_media only; ``hang_on``
    speakers accept play_media and then never return. Every service call is
    kept in ``calls`` in the order it was made.
    """

    def __init__(self, players, fail_on=None, fail_play_media_on=None, hang_on=None):
        self._players = dict(players)
        self._dead = set(fail_on or ())
        self._no_media = set(fail_play_media_on or ())
        self._hang = set(hang_on or ())
        self.calls: list[_Call] = []
        self.states = types.SimpleNamespace(get=self._players.get, async_all=self._all)
        self.services = types.SimpleNamespace(async_call=self._service)

    def _all(self, domain=None):
        prefix = f"{domain}." if domain else ""
        return [st for eid, st in self._players.items() if eid.startswith(prefix)]

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    async def _service(self, domain, service, data, target=None, blocking=False):
        target = dict(target or {})
        if (domain, service) == ("media_player", "play_media"):
            speaker = target.get("entity_id")
            self.calls.append(_Call("play_media", speaker, dict(data), target, blocking))
            if speaker in self._dead | self._no_media:
                raise RuntimeError(f"{speaker} refused play_media")
            if speaker in self._hang:
                await asyncio.Event().wait()
            return None
        if (domain, service) == ("tts", "speak"):
            listed = list(data.get("media_player_entity_id") or ())
            self.calls.append(_Call("tts.speak", listed, dict(data), target, blocking))
            broken = [spk for spk in listed if spk in self._dead]
            if broken:
                raise RuntimeError(f"{broken[0]} refused tts.speak")
            return None
        raise AssertionError(f"unexpected service {domain}.{service}")

    def made(self, speaker):
        """The kinds of call made for one speaker, in order."""
        return [c.kind for c in self.calls
                if c.where == speaker or (isinstance(c.where, list) and speaker in c.where)]


def _players(*specs):
    """{entity_id: _State} from (entity_id, state[, volume]) tuples."""
    return {spec[0]: _State(*spec) for spec in specs}


def _set_nova_config(routing, monkeypatch, mapping):
    """Answer audio_routing's nova_config.get(...) from ``mapping``."""
    package = routing.__name__.rpartition(".")[0]
    settings = types.ModuleType(f"{package}.nova_config")
    settings.get = lambda key, default=None: mapping.get(key, default)
    monkeypatch.setitem(sys.modules, settings.__name__, settings)


@pytest.fixture
def tts(load):
    return load("tts_helper")


@pytest.fixture
def routing(load, monkeypatch):
    """audio_routing, with empty registries guaranteed even when an earlier
    test swapped Home Assistant's helper modules about."""
    helpers = sys.modules.get("homeassistant.helpers") or types.ModuleType("homeassistant.helpers")
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    blank = {
        "area_registry": types.SimpleNamespace(
            async_list_areas=lambda: [], async_get_area=lambda area_id: None),
        "entity_registry": types.SimpleNamespace(
            entities={}, async_get=lambda entity_id: None),
        "device_registry": types.SimpleNamespace(devices={}, async_get=lambda dev_id: None),
    }
    for name, registry in blank.items():
        stub = types.ModuleType(f"homeassistant.helpers.{name}")
        stub.async_get = lambda hass, _reg=registry: _reg
        monkeypatch.setitem(sys.modules, stub.__name__, stub)
        monkeypatch.setattr(helpers, name, stub, raising=False)
    return load("audio_routing")


KITCHEN, DEN, TV = "media_player.kitchen", "media_player.den", "media_player.tv"
GROUP = "media_player.home_group"


# ── whole house broadcasts: silent until the user picks speakers ────────────

@pytest.mark.parametrize("present, kwargs, expected", [
    # Nothing configured: never every device in the house, TV included.
    ([KITCHEN, TV], {}, []),
    ([GROUP], {"broadcast_group": GROUP}, [GROUP]),
    ([], {"broadcast_group": "media_player.gone"}, []),
    ([KITCHEN, DEN], {"announcement_speakers": [KITCHEN, DEN]}, [KITCHEN, DEN]),
    ([KITCHEN], {"announcement_speakers": [KITCHEN, "media_player.gone"]}, [KITCHEN]),
    # The panel can store the list as JSON text, or as comma separated text.
    ([KITCHEN], {"announcement_speakers": f'["{KITCHEN}"]'}, [KITCHEN]),
    ([KITCHEN, DEN], {"announcement_speakers": f"{DEN}, {KITCHEN}"}, [DEN, KITCHEN]),
    # Speakers win over the group; a list of only gone speakers falls to it.
    ([KITCHEN, GROUP], {"announcement_speakers": [KITCHEN], "broadcast_group": GROUP}, [KITCHEN]),
    ([GROUP], {"announcement_speakers": ["media_player.gone"], "broadcast_group": GROUP}, [GROUP]),
], ids=["unset", "group", "group-gone", "speakers", "drops-gone", "json-text",
        "comma-text", "speakers-beat-group", "all-gone-uses-group"])
def test_broadcast_target_uses_only_chosen_speakers(routing, monkeypatch, present, kwargs,
                                                     expected):
    hass = _Hass(_players(*[(eid, "idle") for eid in present]))
    # Even with media players discoverable, none is picked unasked.
    monkeypatch.setattr(routing, "_entities_by_domain", lambda h, d: list(present))
    assert routing.broadcast_target(hass, **kwargs) == expected


@pytest.mark.parametrize("present, kwargs, expected", [
    ([KITCHEN, TV], {}, ([], "notify_only")),
    ([KITCHEN], {"announcement_speakers": [KITCHEN]}, ([KITCHEN], "broadcast")),
    ([GROUP], {"broadcast_group": GROUP}, ([GROUP], "broadcast")),
], ids=["unset-goes-to-phones", "speakers", "group"])
def test_critical_alert_needs_chosen_speakers(routing, monkeypatch, present, kwargs, expected):
    hass = _Hass(_players(*[(eid, "idle") for eid in present]))
    monkeypatch.setattr(routing, "_entities_by_domain", lambda h, d: list(present))
    targets, mode = routing.observer_speak_target(hass, urgency="critical", **kwargs)
    assert (targets, mode) == expected


# ── replies: one assigned speaker per room ──────────────────────────────────
# Speakers are assigned per area by the user (room_speakers), with one
# general_speaker as the fallback. Nothing is discovered from the area: a
# single TV can show up as several media players (Music Assistant, AirPlay,
# Cast), and any filter that tries to weed those out can miss one.

SAT = "assist_satellite.living_room"
ASSIGNED, GENERAL = "media_player.assigned_speaker", "media_player.general"
STRAY = "media_player.living_room_samsung_cast"
PAIRED = "media_player.paired"


@pytest.mark.parametrize("present, config, pairings, expected", [
    # An untagged duplicate player sits in the same room: the assignment wins.
    ([ASSIGNED, STRAY], {"room_speakers": {"living_room": ASSIGNED}}, None, ASSIGNED),
    ([GENERAL], {"room_speakers": {}, "general_speaker": GENERAL}, None, GENERAL),
    ([], {}, None, SAT),
    ([PAIRED, ASSIGNED], {"room_speakers": {"living_room": ASSIGNED}}, {SAT: PAIRED}, PAIRED),
    # A pairing to a player that has gone falls through to the room speaker.
    ([ASSIGNED], {"room_speakers": {"living_room": ASSIGNED}}, {SAT: PAIRED}, ASSIGNED),
    # An assigned speaker that no longer exists falls through to the general one.
    ([GENERAL], {"room_speakers": {"living_room": ASSIGNED}, "general_speaker": GENERAL},
     None, GENERAL),
], ids=["assigned-beats-stray", "general", "satellite", "pairing", "pairing-gone",
        "assigned-gone"])
def test_reply_target(routing, monkeypatch, present, config, pairings, expected):
    hass = _Hass(_players(*[(eid, "idle") for eid in present]))
    monkeypatch.setattr(routing, "entity_area", lambda h, e: "living_room")
    _set_nova_config(routing, monkeypatch, config)
    kwargs = {"satellite_pairings": pairings} if pairings else {}
    assert routing.reply_target(hass, satellite_entity_id=SAT, **kwargs) == expected


# ── observer announcements below critical ───────────────────────────────────
# authoritative_anyone_home exists because of a real departure: the last
# person's tracker had gone not_home, but a phone presence sensor stayed "on"
# for about a minute, and audio_routing's own anyone_home() still counted it,
# so the departure notice played to an empty house. The observer now passes
# the answer it already worked out; room choice still reads live sensors so
# someone genuinely at home keeps occupied room routing.

ENTRY = "media_player.entry"
LIVING = "media_player.living_room"


class _Case(NamedTuple):
    urgency: str
    present: list
    config: dict
    occupied: list
    sensors_say_home: object      # None leaves anyone_home() as it is
    kwargs: dict
    expected: tuple


_OBSERVER_CASES = {
    "medium-assigned-room": _Case(
        "medium", [ASSIGNED], {"room_speakers": {"living_room": ASSIGNED}},
        ["living_room"], True, {}, ([ASSIGNED], "local")),
    "medium-general": _Case(
        "medium", [GENERAL], {"room_speakers": {}, "general_speaker": GENERAL},
        ["living_room"], True, {}, ([GENERAL], "local")),
    "low-nothing-configured": _Case(
        "low", [], {}, ["living_room"], None, {}, ([], "suppressed")),
    "departure-no-stale-sensor": _Case(
        "medium", [ENTRY], {"room_speakers": {"entry": ENTRY}, "general_speaker": ENTRY},
        [], None, {"authoritative_anyone_home": False}, ([], "notify_only")),
    "departure-with-stale-sensor": _Case(
        "medium", [ENTRY], {"room_speakers": {"entry": ENTRY}, "general_speaker": ENTRY},
        ["entry"], True, {"authoritative_anyone_home": False}, ([], "notify_only")),
    "someone-else-home": _Case(
        "medium", [LIVING], {"room_speakers": {"living_room": LIVING}},
        ["living_room"], None, {"authoritative_anyone_home": True}, ([LIVING], "local")),
    "high-departure-stale-sensor": _Case(
        "high", [ENTRY], {"general_speaker": ENTRY},
        [], True, {"authoritative_anyone_home": False}, ([], "notify_only")),
    # A caller that does not pass the argument (briefings, the cognitive core)
    # keeps the old behaviour, stale sensor and all. Unchanged, not fixed.
    "no-argument-keeps-old-behaviour": _Case(
        "medium", [ENTRY], {"room_speakers": {"entry": ENTRY}},
        ["entry"], True, {}, ([ENTRY], "local")),
}


@pytest.mark.parametrize("case", list(_OBSERVER_CASES.values()), ids=list(_OBSERVER_CASES))
def test_observer_routing_below_critical(routing, monkeypatch, case):
    hass = _Hass(_players(*[(eid, "idle") for eid in case.present]))
    monkeypatch.setattr(routing, "currently_occupied_areas", lambda h: list(case.occupied))
    if case.sensors_say_home is not None:
        monkeypatch.setattr(routing, "anyone_home", lambda h: case.sensors_say_home)
    _set_nova_config(routing, monkeypatch, case.config)
    got = routing.observer_speak_target(hass, urgency=case.urgency, **case.kwargs)
    assert got == case.expected


# ── the television guard ────────────────────────────────────────────────────

def test_drop_display_targets_removes_tvs_and_the_film_player(routing, nova_runtime):
    # Last line of defence: whatever chose the targets, a TV and the film
    # player from the entry's runtime config are never spoken to.
    tv = _State("media_player.samsung_tv", "on")
    tv.attributes = {"device_class": "tv"}
    speaker = _State("media_player.living_room_speaker", "idle")
    projector = _State("media_player.projector", "idle")
    hass = _Hass({st.entity_id: st for st in (speaker, tv, projector)})
    nova_runtime(hass, {"movie_media_player": "media_player.projector"})
    ids = [speaker.entity_id, tv.entity_id, projector.entity_id]
    assert routing.drop_display_targets(hass, ids, "unit-test") == [speaker.entity_id]


def test_drop_display_targets_keeps_ordinary_speakers_in_order(routing):
    hass = _Hass(_players((KITCHEN, "idle"), (DEN, "idle")))
    assert routing.drop_display_targets(hass, [KITCHEN, DEN], "unit-test") == [KITCHEN, DEN]


# ── delivery: one play_media per speaker ────────────────────────────────────

A, B = "media_player.a", "media_player.b"


def _sonos(tts, monkeypatch, *ids):
    """Mark ``ids`` as Sonos players and everything else as not."""
    monkeypatch.setattr(tts, "_is_sonos", lambda hass, eid: eid in ids)


async def test_each_speaker_gets_its_own_announce_at_its_own_volume(tts, monkeypatch):
    _sonos(tts, monkeypatch, A, B)
    hass = _Hass(_players((A, "idle", 0.3), (B, "idle", 0.7)))
    assert await tts.async_announce(hass, "hello", "tts.piper", [A, B]) is True

    assert [c.kind for c in hass.calls] == ["play_media", "play_media"]  # no tts.speak
    by_speaker = {c.where: c for c in hass.calls}
    assert list(by_speaker) == [A, B]
    expected_id = "media-source://tts/tts.piper?message=hello&cache=true"
    for speaker, volume in ((A, 0.3), (B, 0.7)):
        call = by_speaker[speaker]
        assert call.data == {"media_content_id": expected_id, "media_content_type": "music",
                             "announce": True, "extra": {"volume": volume}}
        assert call.target == {"entity_id": speaker}


async def test_volume_is_only_pinned_on_sonos(tts, monkeypatch):
    # Cast hands `extra` to pychromecast, which rejects `volume` with a
    # TypeError, so every Cast announcement used to fail outright.
    sonos, cast = "media_player.sonos", "media_player.cast"
    _sonos(tts, monkeypatch, sonos)
    hass = _Hass(_players((sonos, "idle", 0.3), (cast, "off", 0.4)))
    await tts.async_announce(hass, "hello", "tts.piper", [sonos, cast])
    sent = {c.where: c.data for c in hass.calls if c.kind == "play_media"}
    assert sent[sonos]["extra"] == {"volume": 0.3}
    assert "extra" not in sent[cast]


@pytest.mark.parametrize("volume", [None, "loud"], ids=["no-volume", "not-a-number"])
async def test_sonos_without_a_numeric_volume_gets_no_extra(tts, monkeypatch, volume):
    _sonos(tts, monkeypatch, A)
    hass = _Hass({A: _State(A, "idle", volume)})
    assert await tts.async_announce(hass, "hello", "tts.piper", [A]) is True
    assert "extra" not in hass.calls[0].data


async def test_play_media_is_a_blocking_call(tts):
    # Fire and forget can never raise, so a failing speaker would be logged
    # as delivered and its fallback would never run.
    hass = _Hass(_players((A, "idle")))
    assert await tts.async_announce(hass, "hello", "tts.piper", [A]) is True
    assert [(c.kind, c.blocking) for c in hass.calls] == [("play_media", True)]


async def test_play_media_is_awaited_through_the_service_registry(tts):
    # Same guarantee, seen from a spy on the service call itself.
    hass = _Hass(_players((A, "idle")))
    real = hass.services.async_call
    seen = []

    async def spy(domain, service, data, target=None, blocking=False):
        seen.append((domain, service, blocking))
        return await real(domain, service, data, target=target, blocking=blocking)

    hass.services.async_call = spy
    assert await tts.async_announce(hass, "hello", "tts.piper", [A])
    assert seen == [("media_player", "play_media", True)]


@pytest.fixture
def history_spy(tts, monkeypatch):
    """Swap Spoken History for a list, labelling every context "briefing"."""
    rows = []
    fake = types.ModuleType("spoken_history")
    fake.record = lambda *args, **kwargs: rows.append(args)
    package = tts.__name__.rpartition(".")[0]
    monkeypatch.setitem(sys.modules, f"{package}.spoken_history", fake)
    monkeypatch.setattr(tts, "_history_source", lambda ctx: "briefing")
    return rows


@pytest.mark.parametrize("reading", ["unavailable", "unknown", None],
                         ids=["unavailable", "unknown", "missing"])
async def test_a_speaker_that_cannot_play_is_not_counted_as_delivered(tts, history_spy,
                                                                      reading):
    players = {} if reading is None else {A: _State(A, reading)}
    hass = _Hass(players)
    ok = await tts.async_announce(hass, "hello", "tts.piper", [A], context="briefing")
    assert ok is False
    assert hass.calls == []
    assert history_spy == []


async def test_a_quiet_speaker_is_trusted_and_never_announced_twice(tts):
    # Polling for a speaker to "visibly respond" was tried and dropped: Sonos
    # plays an announcement without changing any state Home Assistant can
    # see, so the poll always gave up and fired an unpinned tts.speak on top,
    # doubling the message and jumping the volume. A play_media call that
    # returns is now enough.
    player = _State(A, "idle", 0.3)
    hass = _Hass({A: player})
    assert await tts.async_announce(hass, "hello", "tts.piper", [A]) is True
    assert hass.made(A) == ["play_media"]
    assert (player.state, player.last_updated) == ("idle", 0)


async def test_a_speaker_still_busy_at_the_timeout_counts_as_delivered(tts, monkeypatch):
    monkeypatch.setattr(tts, "_PLAY_MEDIA_TIMEOUT", 0.01)  # 10 ms, not 15 s
    hass = _Hass(_players((A, "idle")), hang_on={A})
    assert await tts.async_announce(hass, "hello", "tts.piper", [A]) is True
    assert hass.made(A) == ["play_media"]  # no fallback on a timeout


async def test_a_play_media_error_falls_back_to_tts_speak_for_that_speaker(tts):
    hass = _Hass(_players((A, "idle", 0.3)), fail_play_media_on={A})
    assert await tts.async_announce(hass, "hello", "tts.piper", [A]) is True
    assert hass.made(A) == ["play_media", "tts.speak"]
    fallback = hass.calls[1]
    assert fallback.where == [A]
    assert fallback.data == {"media_player_entity_id": [A], "message": "hello", "cache": True}
    assert fallback.target == {"entity_id": "tts.piper"}
    assert fallback.blocking is True


async def test_one_dead_speaker_does_not_silence_the_rest(tts):
    # One dead target used to end the whole broadcast. Here both play_media
    # and the fallback fail for it; the good speaker must still play.
    dead, good = "media_player.dead", "media_player.good"
    hass = _Hass(_players((dead, "idle", 0.5), (good, "idle", 0.5)), fail_on={dead})
    ok = await tts.async_announce(hass, "briefing", "tts.piper", [dead, good])
    assert ok is True, "at least one speaker still got the message"
    assert hass.made(good) == ["play_media"]
    assert hass.made(dead) == ["play_media", "tts.speak"]


async def test_every_speaker_failing_reports_failure(tts):
    hass = _Hass(_players((A, "idle"), (B, "idle")), fail_on={A, B})
    assert await tts.async_announce(hass, "hello", "tts.piper", [A, B]) is False
    assert hass.made(A) == hass.made(B) == ["play_media", "tts.speak"]


@pytest.mark.parametrize("text, voice, speakers", [
    ("hello", "tts.piper", []),        # nowhere to speak
    ("hello", None, [A]),              # nothing to speak with
    ("", "tts.piper", [A]),            # nothing to say
    ("**", "tts.piper", [A]),          # nothing left once markdown is gone
], ids=["no-speakers", "no-voice", "empty-text", "markdown-only"])
async def test_nothing_to_say_or_nowhere_to_say_it_does_nothing(tts, text, voice, speakers):
    hass = _Hass(_players((A, "idle")))
    assert await tts.async_announce(hass, text, voice, speakers) is False
    assert hass.calls == []


async def test_a_tv_in_the_list_is_never_spoken_to(tts, nova_runtime):
    tv = _State(TV, "on")
    tv.attributes = {"device_class": "tv"}
    hass = _Hass({TV: tv, A: _State(A, "idle")})
    nova_runtime(hass, {})
    assert await tts.async_announce(hass, "hello", "tts.piper", [TV, A]) is True
    assert [c.where for c in hass.calls] == [A]
    hass.calls.clear()
    assert await tts.async_announce(hass, "hello", "tts.piper", [TV]) is False
    assert hass.calls == []


async def test_markdown_is_stripped_before_speaking(tts, monkeypatch):
    # The model, or any caller, may hand over markdown; speakers get plain text.
    _sonos(tts, monkeypatch)
    hass = _Hass(_players((A, "idle")))
    ok = await tts.async_announce(
        hass, "Sir, two things: - **Weather** — cloudy? - **Doors** — locked?",
        "tts.piper", [A], context="summary")
    assert ok is True
    spoken = [urllib.parse.unquote_plus(c.data["media_content_id"]) for c in hass.calls]
    assert len(spoken) == 1
    assert "*" not in spoken[0] and " - " not in spoken[0]
    assert "Weather" in spoken[0] and "Doors" in spoken[0]


# ── Spoken History ──────────────────────────────────────────────────────────
# async_announce is the one recorder for anything sent through it: once per
# real delivery, never on total failure, and a broken store never turns a
# delivered message into a failed one. The store itself is tested in
# test_spoken_history.py and "repeat that" in test_local_engine.py.

@pytest.fixture
def sh(load, tmp_path, monkeypatch):
    # tts_helper imports spoken_history lazily from the same module cache
    # `load` fills, so this is the module async_announce writes to.
    module = load("spoken_history")
    module._last = None
    monkeypatch.setattr(module, "_DEFAULT_DB", str(tmp_path / "history.db"))
    return module


async def test_a_delivery_is_recorded_once_with_its_details(tts, sh):
    hass = _Hass(_players((A, "idle", 0.5)))
    ok = await tts.async_announce(hass, "Welcome home, sir.", "tts.piper", [A],
                                  context="routine", action_request_id="req-1")
    assert ok is True
    [row] = sh.list_recent()
    assert row["text"] == "Welcome home, sir."
    assert row["source"] == "routine"
    assert row["speakers"] == [A]
    assert row["delivery_state"] == "sent"
    assert row["action_request_id"] == "req-1"


@pytest.mark.parametrize("context, source", [
    ("sentinel", "alert"), ("package", "alert"), ("doorbell", "camera"),
    ("summary", "briefing"), ("test", "manual"), ("reminder", "reminder"),
])
async def test_history_labels_follow_the_context(tts, sh, context, source):
    hass = _Hass(_players((A, "idle")))
    assert await tts.async_announce(hass, "hello", "tts.piper", [A], context=context)
    assert [row["source"] for row in sh.list_recent()] == [source]


async def test_a_failed_delivery_is_never_recorded(tts, sh):
    hass = _Hass(_players((A, "idle")), fail_on={A})
    ok = await tts.async_announce(hass, "hello", "tts.piper", [A], context="reminder")
    assert ok is False
    assert sh.list_recent() == []


async def test_only_the_speakers_that_took_it_are_recorded(tts, sh):
    dead, good = "media_player.dead", "media_player.good"
    hass = _Hass(_players((dead, "idle", 0.5), (good, "idle", 0.5)), fail_on={dead})
    ok = await tts.async_announce(hass, "briefing text", "tts.piper", [dead, good],
                                  context="briefing")
    assert ok is True
    [row] = sh.list_recent()
    assert row["speakers"] == [good]


async def test_plain_chat_is_never_recorded(tts, sh):
    # Only Nova's own labelled announcements belong in the history; the
    # default context is "chat".
    hass = _Hass(_players((A, "idle", 0.5)))
    assert await tts.async_announce(hass, "hello", "tts.piper", [A]) is True
    assert sh.list_recent() == []


async def test_a_broken_history_store_still_reports_delivery(tts, sh, monkeypatch):
    def no_disk(_db):
        raise RuntimeError("disk full")
    monkeypatch.setattr(sh, "_connect", no_disk)
    hass = _Hass(_players((A, "idle", 0.5)))
    assert await tts.async_announce(hass, "hello", "tts.piper", [A], context="reminder") is True


async def test_a_repeat_links_back_to_what_it_repeats(tts, sh):
    hass = _Hass(_players((A, "idle", 0.5)))
    first = sh.record("hello", "reminder", [A])
    ok = await tts.async_announce(hass, "hello", "tts.piper", [A],
                                  context="repeat", repeat_of_id=first)
    assert ok is True
    newest, oldest = sh.list_recent()
    assert (newest["source"], newest["repeat_of_id"]) == ("repeat", first)
    assert oldest["id"] == first
