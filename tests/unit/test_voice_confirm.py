"""Spoken and phone confirmation before a protected action.

The rule that matters most: only a clear yes approves. Anything else (a no,
silence, no way to ask, a crash) must come back as "not approved" so the
unlock, open or disarm never runs. Also covered: which actions need asking,
how the channel is chosen, the phone push itself and the open follow-up.
"""
import asyncio
import sys
import types

import pytest
from fakes import FakeStates

SATELLITE = "assist_satellite.hall"
SPEAKER = "media_player.den"


class Home:
    """The slice of hass the confirmation code touches.

    ``phones`` become notify services; ``reply`` is what a service call
    returns (or raises, when it is an exception); ``on_send`` runs inside each
    call so a test can tap the phone while the push is still going out."""

    def __init__(self, phones=(), reply=None):
        self.sent = []
        self.listeners = []
        self.unsubscribed = 0
        self.reply = reply
        self.on_send = None
        self.states = FakeStates()
        notify = {name: {} for name in phones}
        self.services = types.SimpleNamespace(
            async_call=self._call, async_services=lambda: {"notify": notify})
        self.bus = types.SimpleNamespace(async_listen=self._listen)

    async def _call(self, domain, service, data, **options):
        self.sent.append((domain, service, data, options))
        if self.on_send is not None:
            self.on_send(data)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply

    def _listen(self, event_type, handler):
        assert event_type == "mobile_app_notification_action"
        self.listeners.append(handler)

        def _stop():
            self.unsubscribed += 1
        return _stop

    def tap(self, action):
        for handler in self.listeners:
            handler(types.SimpleNamespace(data={"action": action}))

    def pushes(self):
        return [(service, data) for domain, service, data, _ in self.sent if domain == "notify"]


def _buttons(payload):
    return [button["action"] for button in payload["data"]["actions"]]


@pytest.fixture
def vc(load):
    return load("voice_confirm")


@pytest.fixture
def settings(vc, monkeypatch):
    """Panel settings for the test; the module reads them through _cfg."""
    values = {}
    monkeypatch.setattr(vc, "_cfg", lambda _hass, key, default=None: values.get(key, default))
    return values


@pytest.fixture
def on(settings):
    settings["voice_confirm_enabled"] = True
    return settings


@pytest.fixture
def gated_steps(vc, monkeypatch):
    """Fake speaker, playback wait and mic, logging each step in order."""
    steps = []

    async def announce(hass, text, engine, speakers, context=""):
        steps.append(("say", text, engine, list(speakers), context))

    async def wait(hass, speakers, timeout=None):
        steps.append(("wait", list(speakers)))

    async def listen(hass, satellite):
        steps.append(("listen", satellite))
        return True

    fake_tts = types.ModuleType("jc.tts_helper")
    fake_tts.async_announce = announce
    monkeypatch.setitem(sys.modules, "jc.tts_helper", fake_tts)
    monkeypatch.setattr(sys.modules["jc"], "tts_helper", fake_tts, raising=False)
    monkeypatch.setattr(vc, "_wait_for_playback", wait)
    monkeypatch.setattr(vc, "_start_listening", listen)
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda _h, _e: SATELLITE)
    monkeypatch.setattr(vc, "_speaker_for_satellite", lambda _h, _s: ("tts.piper", [SPEAKER]))
    return steps


def _native_answer(vc, monkeypatch, answer):
    async def _native(_hass, _satellite, _question, _timeout):
        return answer
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda _h, _e: SATELLITE)
    monkeypatch.setattr(vc, "_confirm_native", _native)


async def _both_views(vc, hass, entity="lock.front"):
    """(confirm, confirm_typed) for one question, so each test checks both."""
    flag = await vc.confirm(hass, "sure?", entity_id=entity)
    word = await vc.confirm_typed(hass, "sure?", entity_id=entity)
    return flag, word


# ── Which actions need asking ───────────────────────────────────────────────

def test_the_feature_starts_switched_off(vc, settings):
    assert vc.is_enabled(Home()) is False
    # Switched off, even an unlock goes straight through.
    assert vc.action_is_protected(Home(), "lock", "unlock", "lock.front") is False


@pytest.mark.parametrize("domain, service, entity, protected", [
    ("lock", "unlock", "lock.front", True),
    ("lock", "lock", "lock.front", False),
    ("cover", "open", "cover.garage", True),
    ("cover", "open_cover", "cover.garage", True),
    ("cover", "toggle", "cover.garage", True),
    ("cover", "set_cover_position", "cover.garage", True),
    ("cover", "close_cover", "cover.garage", False),
    ("alarm_control_panel", "alarm_disarm", "alarm_control_panel.home", True),
    ("alarm_control_panel", "alarm_arm_away", "alarm_control_panel.home", False),
    ("light", "turn_on", "light.kitchen", False),
    # Switches only count when the id sounds like security.
    ("switch", "turn_off", "switch.alarm_siren", True),
    ("switch", "toggle", "switch.security_camera", True),
    ("switch", "turn_off", "switch.desk_lamp", False),
    ("switch", "turn_on", "switch.alarm_siren", False),
    # Scenes, scripts and automations can do anything, bar stop and reload.
    ("script", "front_door_routine", "", True),
    ("scene", "turn_on", "scene.evening", True),
    ("automation", "trigger", "automation.hall", True),
    ("automation", "turn_off", "automation.hall", False),
    ("script", "reload", "", False),
    ("script", "stop", "", False),
])
def test_protected_actions(vc, on, domain, service, entity, protected):
    assert vc.action_is_protected(Home(), domain, service, entity) is protected


@pytest.mark.parametrize("overrides, domain, service, entity, protected", [
    # A "!" exemption beats a normally protected action.
    (["!lock.test_deadbolt"], "lock", "unlock", "lock.test_deadbolt", False),
    # A plain entry opts a normally free entity in.
    (["switch.pool_pump"], "switch", "turn_off", "switch.pool_pump", True),
    # The exemption wins even when the same id is also listed plainly.
    (["lock.side", "!lock.side"], "lock", "unlock", "lock.side", False),
    # A setting that is not a list is ignored, not trusted.
    ("lock.front", "lock", "unlock", "lock.front", True),
])
def test_entity_overrides(vc, on, overrides, domain, service, entity, protected):
    on["voice_confirm_entities"] = overrides
    assert vc.action_is_protected(Home(), domain, service, entity) is protected


@pytest.mark.parametrize("stored, mode", [
    (None, "auto"), ("nonsense", "auto"), ("gated", "gated"),
    ("native", "native"), ("NATIVE", "native"), ("", "auto"),
])
def test_mode_setting(vc, settings, stored, mode):
    if stored is not None:
        settings["voice_confirm_mode"] = stored
    assert vc._mode(Home()) == mode


@pytest.mark.parametrize("mode, audio_out, native", [
    ("native", None, True),
    ("gated", True, False),
    ("auto", True, True),
    ("auto", None, False),
    ("auto", {SATELLITE: True}, True),
    ("auto", {"assist_satellite.other": True}, False),
])
def test_auto_mode_goes_native_only_with_known_audio(vc, settings, mode, audio_out, native):
    settings["voice_confirm_mode"] = mode
    if audio_out is not None:
        settings["satellite_audio_out"] = audio_out
    assert vc._use_native(Home(), SATELLITE) is native


def test_yes_and_no_words_never_overlap(vc):
    assert set(vc._YES).isdisjoint(vc._NO)
    assert {"yes", "unlock it", "open it"} <= set(vc._YES)
    assert {"no", "cancel", "don't"} <= set(vc._NO)


# ── confirm(): only a clear yes approves ───────────────────────────────────

async def test_no_satellite_and_no_phone_is_an_error(vc, on, monkeypatch):
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda _h, _e: None)
    hass = Home()
    # Falls back to the phone, finds none to ask, and refuses.
    assert await _both_views(vc, hass) == (False, "error")
    assert hass.sent == []


@pytest.mark.parametrize("answer, approved", [("approved", True), ("rejected", False),
                                              ("expired", False)])
async def test_native_answer_is_passed_through(vc, on, monkeypatch, answer, approved):
    on["voice_confirm_mode"] = "native"
    _native_answer(vc, monkeypatch, answer)
    assert await _both_views(vc, Home()) == (approved, answer)


async def test_gated_asks_aloud_but_never_approves(vc, on, gated_steps):
    on["voice_confirm_mode"] = "gated"
    assert await vc.confirm(Home(), "Unlock the front door?", entity_id="lock.front") is False
    # The prompt plays out before the mic reopens, so it cannot hear itself.
    assert gated_steps == [
        ("say", "Unlock the front door?", "tts.piper", [SPEAKER], "confirm"),
        ("wait", [SPEAKER]),
        ("listen", SATELLITE),
    ]
    assert await vc.confirm_typed(Home(), "sure?", entity_id="lock.front") == "deferred"


async def test_native_with_no_readable_answer_falls_back_to_gated(vc, on, gated_steps,
                                                                  monkeypatch):
    on["voice_confirm_mode"] = "native"
    _native_answer(vc, monkeypatch, None)
    assert await _both_views(vc, Home()) == (False, "deferred")
    assert [step[0] for step in gated_steps[:3]] == ["say", "wait", "listen"]


async def test_gated_without_a_speaker_is_an_error(vc, on, gated_steps, monkeypatch):
    on["voice_confirm_mode"] = "gated"
    monkeypatch.setattr(vc, "_speaker_for_satellite", lambda _h, _s: ("tts.piper", []))
    assert await _both_views(vc, Home()) == (False, "error")
    assert gated_steps == []


async def test_a_crash_while_choosing_counts_as_no(vc, on, monkeypatch):
    def _broken(_hass, _entity):
        raise RuntimeError("registry gone")
    monkeypatch.setattr(vc, "_satellite_for_entity", _broken)
    assert await _both_views(vc, Home()) == (False, "error")


# ── The native question ─────────────────────────────────────────────────────

@pytest.mark.parametrize("reply, outcome", [
    ({SATELLITE: {"id": "confirm"}}, "approved"),
    ({"id": "confirm"}, "approved"),
    ({"id": "deny"}, "rejected"),
    ({SATELLITE: {"id": "deny"}}, "rejected"),
    ({"something": "else"}, None),
    ("not a dict", None),
    (None, None),
    (RuntimeError("service missing"), None),
])
async def test_native_reads_the_matched_answer(vc, reply, outcome):
    hass = Home(reply=reply)
    assert await vc._confirm_native(hass, SATELLITE, "Unlock?", 5) == outcome


async def test_native_asks_with_both_sentence_sets(vc):
    hass = Home(reply={"id": "confirm"})
    await vc._confirm_native(hass, SATELLITE, "Unlock?", 5)
    ((domain, service, data, options),) = hass.sent
    assert (domain, service) == ("assist_satellite", "ask_question")
    assert data["entity_id"] == SATELLITE and data["question"] == "Unlock?"
    assert data["preannounce"] is False
    assert data["answers"] == [{"id": "confirm", "sentences": vc._YES},
                               {"id": "deny", "sentences": vc._NO}]
    assert options == {"blocking": True, "return_response": True}


async def test_native_gives_up_when_the_satellite_stays_silent(vc):
    """The answer time limit holds even if the satellite never replies."""
    hass = Home()

    async def _hang(*_args, **_kwargs):
        await asyncio.Event().wait()
    hass.services.async_call = _hang
    outcome = await asyncio.wait_for(vc._confirm_native(hass, SATELLITE, "Unlock?", 0.05), 2)
    assert outcome == "expired"


# ── The phone push ─────────────────────────────────────────────────────────

def _answer_after_send(hass, button):
    """Tap the given button (0 confirm, 1 deny) once the push has gone out."""
    def _schedule(payload):
        action = _buttons(payload)[button]
        asyncio.get_running_loop().call_soon(hass.tap, action)
    hass.on_send = _schedule


@pytest.mark.parametrize("button, outcome", [(0, "approved"), (1, "rejected")])
async def test_a_tap_decides(vc, button, outcome):
    hass = Home(phones=("mobile_app_alex",))
    _answer_after_send(hass, button)
    assert await vc._confirm_via_notification(hass, "Unlock the door?", timeout=2) == outcome
    assert hass.unsubscribed == 1


async def test_the_push_is_a_confirm_or_deny_question(vc):
    hass = Home(phones=("mobile_app_alex", "persistent", "mobile_app_sam"))
    _answer_after_send(hass, 1)
    await vc._confirm_via_notification(hass, "Unlock the door?", timeout=2)

    pushes = hass.pushes()
    # Only phone apps are asked; other notify services are skipped.
    assert [service for service, _ in pushes] == ["mobile_app_alex", "mobile_app_sam"]
    payload = pushes[0][1]
    assert payload["title"] == "Nova needs your OK"
    assert payload["message"] == "Unlock the door?"
    assert [b["title"] for b in payload["data"]["actions"]] == ["Confirm", "Deny"]
    confirm_id, deny_id = _buttons(payload)
    assert confirm_id.startswith("NOVA_CONFIRM_") and deny_id.startswith("NOVA_DENY_")
    assert confirm_id[len("NOVA_CONFIRM_"):] == deny_id[len("NOVA_DENY_"):]


async def test_each_question_gets_fresh_buttons(vc):
    """A stale tap from an earlier question must not answer a new one."""
    hass = Home(phones=("mobile_app_alex",))
    _answer_after_send(hass, 1)
    await vc._confirm_via_notification(hass, "first?", timeout=2)
    old_confirm = _buttons(hass.pushes()[0][1])[0]

    hass.on_send = lambda _payload: asyncio.get_running_loop().call_soon(hass.tap, old_confirm)
    assert await vc._confirm_via_notification(hass, "second?", timeout=0.05) == "expired"
    assert _buttons(hass.pushes()[1][1])[0] != old_confirm


async def test_no_tap_expires(vc):
    hass = Home(phones=("mobile_app_alex",))
    assert await vc._confirm_via_notification(hass, "Unlock the door?", timeout=0.01) == "expired"
    assert hass.unsubscribed == 1


@pytest.mark.parametrize("phones", [(), ("persistent", "email")])
async def test_no_phone_to_ask_is_an_error(vc, phones):
    hass = Home(phones=phones)
    assert await vc._confirm_via_notification(hass, "Unlock the door?", timeout=1) == "error"
    assert hass.pushes() == []
    assert hass.unsubscribed == 1


async def test_a_tap_while_still_sending_is_caught(vc):
    """The listener is up before the push goes out, so a tap that arrives
    during the send is not lost."""
    hass = Home(phones=("mobile_app_alex",))
    hass.on_send = lambda payload: hass.tap(_buttons(payload)[0])
    assert await vc._confirm_via_notification(hass, "Unlock the door?", timeout=0.05) == "approved"


async def test_the_first_tap_wins(vc):
    hass = Home(phones=("mobile_app_alex",))

    def _both(payload):
        confirm_id, deny_id = _buttons(payload)
        hass.tap(deny_id)
        hass.tap(confirm_id)
    hass.on_send = _both
    assert await vc._confirm_via_notification(hass, "Unlock?", timeout=0.05) == "rejected"


async def test_phone_only_passes_the_answer_and_the_phone_list(vc, monkeypatch):
    seen = []
    answers = iter(["approved", "approved", "rejected", "rejected"])

    async def _ask(hass, question, timeout=None, **extra):
        seen.append((question, extra))
        return next(answers)
    monkeypatch.setattr(vc, "_confirm_via_notification", _ask)

    assert await vc.confirm_via_phone_only_typed(Home(), "sure?") == "approved"
    assert await vc.confirm_via_phone_only(Home(), "sure?") is True
    assert await vc.confirm_via_phone_only_typed(
        Home(), "sure?", services=["mobile_app_alex"]) == "rejected"
    assert await vc.confirm_via_phone_only(Home(), "sure?") is False
    assert seen[2] == ("sure?", {"services": ["mobile_app_alex"]})
    assert seen[0] == ("sure?", {})


async def test_phone_only_with_a_phone_list_asks_only_those(vc):
    hass = Home(phones=("mobile_app_alex", "mobile_app_sam"))
    _answer_after_send(hass, 0)
    outcome = await vc.confirm_via_phone_only_typed(hass, "sure?", timeout=2,
                                                    services=["mobile_app_sam"])
    assert outcome == "approved"
    assert [service for service, _ in hass.pushes()] == ["mobile_app_sam"]


async def test_phone_only_crash_is_an_error(vc, monkeypatch):
    async def _broken(*_args, **_kwargs):
        raise RuntimeError("bus gone")
    monkeypatch.setattr(vc, "_confirm_via_notification", _broken)
    assert await vc.confirm_via_phone_only_typed(Home(), "sure?") == "error"
    assert await vc.confirm_via_phone_only(Home(), "sure?") is False


# ── Open follow-up and the panel test ──────────────────────────────────────

async def test_followup_without_a_speaker_leaves_the_mic_shut(vc, settings, gated_steps,
                                                             monkeypatch):
    """With nothing to speak on, the question is not asked, the mic stays
    closed and the call reports failure."""
    settings["voice_confirm_mode"] = "gated"
    monkeypatch.setattr(vc, "_speaker_for_satellite", lambda _h, _s: ("tts.piper", []))
    outcome = await vc.ask_followup(Home(), "Which one?")
    assert outcome["ok"] is False
    assert outcome["satellite"] == SATELLITE
    assert gated_steps == []


async def test_followup_gated_speaks_then_listens(vc, settings, gated_steps):
    settings["voice_confirm_mode"] = "gated"
    outcome = await vc.ask_followup(Home(), "Which one?")
    assert outcome == {"ok": True, "mode": "gated", "satellite": SATELLITE}
    assert gated_steps == [("say", "Which one?", "tts.piper", [SPEAKER], "followup"),
                           ("wait", [SPEAKER]), ("listen", SATELLITE)]


async def test_followup_native_starts_a_conversation(vc, settings, monkeypatch):
    settings["voice_confirm_mode"] = "native"
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda _h, _e: SATELLITE)
    hass = Home()
    outcome = await vc.ask_followup(hass, "Which one?", turn_context="lights")
    assert outcome == {"ok": True, "mode": "native", "satellite": SATELLITE}
    ((domain, service, data, _),) = hass.sent
    assert (domain, service) == ("assist_satellite", "start_conversation")
    assert data["start_message"] == "Which one?"
    assert data["extra_system_prompt"] == "lights"


async def test_followup_with_no_satellite_reports_it(vc, settings, monkeypatch):
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda _h, _e: None)
    outcome = await vc.ask_followup(Home(), "Which one?")
    assert outcome == {"ok": False, "error": "no assist_satellite available"}


async def test_announce_test_names_no_speaker_brand(vc):
    hass = Home()
    outcome = await vc.announce_test(hass, SATELLITE)
    assert outcome["ok"] is True and outcome["satellite"] == SATELLITE
    assert "Nest" not in outcome["note"]
    assert "your speaker" in outcome["note"]
    assert hass.sent[0][:2] == ("assist_satellite", "announce")


async def test_announce_test_failures_say_so(vc, monkeypatch):
    monkeypatch.setattr(vc, "_satellite_for_entity", lambda _h, _e: None)
    assert (await vc.announce_test(Home()))["ok"] is False
    broken = Home(reply=RuntimeError("no such service"))
    outcome = await vc.announce_test(broken, SATELLITE)
    assert outcome["ok"] is False and "gated mode" in outcome["note"]
