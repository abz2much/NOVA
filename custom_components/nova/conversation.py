"""Nova's conversation entity: one turn in, one reply out.

A turn passes, in order: the runtime check, the Chat tab owner check, Nova
Voice ID, the multi wake dedup, the optional presence gate and the relevance
gate. Only then does it touch memory, and only then is it answered: a held
request's yes, a proactive offer's answer, a voice phrase, the local engine
and, last, the agent. The reply is spoken on the room's paired speaker when
there is one, and the mic may reopen for a follow up."""
from __future__ import annotations

import contextlib
import logging
import os
import re
import time
import weakref
from typing import Literal, Optional

from homeassistant.components import conversation
from homeassistant.components.conversation import ConversationEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, llm
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import paths
from .audio_routing import reply_targets
from .const import (
    CONF_BROADCAST_SPEAKERS,
    CONF_CAST_SPEAKERS,
    CONF_DIRECTIVE,
    CONF_DIRECTIVE_PRESET,
    CONF_HONORIFIC,
    CONF_MODEL,
    CONF_REPLY_SPEAKERS,
    CONF_ROOM_ROUTING,
    CONF_TTS_ENGINE,
    CONF_USE_HASS_API,
    CONF_VOICE_SATELLITES,
    DEFAULT_DIRECTIVE_PRESET,
    DEFAULT_HONORIFIC,
    DEFAULT_MODEL,
    DEFAULT_ROOM_ROUTING,
    DEFAULT_TTS_ENGINE,
    DOMAIN,
    NOVA_PERSONA,
    get_directive,
)
from .database import save_message
from .llm_provider import resolve_provider_credential, resolve_provider_endpoint
from .locale_format import format_date
from .presence import presence_context_string
from .runtime import get_runtime, runtime_config_snapshot
from .speech_text import speech_text
from .tts_helper import async_announce, resolve_tts_entity

_LOGGER = logging.getLogger(__name__)

try:   # faces seen recently, for the prompt; optional
    from .recognition import recognition_context_string
except ImportError:
    recognition_context_string = None

MAX_HISTORY = 20    # turns kept per conversation for the agent
MAX_ITERS = 8       # the agent's tool loop limit
PERSONA_FILE: Optional[str] = None   # a test override; None means paths.py decides

# Added to the agent's prompt for a turn from a voice satellite (8.34.0).
VOICE_REPLY_RULE = (
    "This reply is spoken aloud on the voice satellite the user is talking to. "
    "Answer in one or two short sentences. Never mention voice phrases, tools, "
    "matching, prompts or anything else about how you work."
)

_FALLBACKS = [
    "Technical difficulties, {honorific}. Even I have them occasionally. Please try again.",
    "I appear to be having connectivity issues, {honorific}. Bear with me.",
    "Something is interfering with my systems, {honorific}. One moment.",
]

_UNREACHABLE = ("unavailable", "unknown")
_SATELLITE = "assist_satellite."


# ── Several satellites hearing one request ──────────────────────────────────
# Each satellite that hears a wake word runs its own pipeline. The first one
# to arrive claims the words; any other device sending the same words within
# the window gets the winner's reply back as text and nothing is spoken twice.

_DEDUP_WINDOW = 4.0   # seconds; transcripts from two rooms land this close
# normalised words -> (claimed at, the winner's reply or None, the winner's device)
_dedup_cache: dict[str, tuple[float, str | None, str | None]] = {}


def _dedup_key(text: str) -> str:
    """The words compared across satellites: lower case, no end punctuation."""
    return text.lower().strip().rstrip(".,!?")


def _check_and_claim_dedup(text: str, device_id: str | None) -> tuple[bool, str | None]:
    """(True, the winner's reply so far) when another device already claimed
    these words within the window; otherwise this device claims them and
    gets (False, None). Claims older than twice the window are swept."""
    now = time.time()
    for key in [k for k, row in _dedup_cache.items() if now - row[0] > 2 * _DEDUP_WINDOW]:
        del _dedup_cache[key]
    key = _dedup_key(text)
    claim = _dedup_cache.get(key)
    if claim is not None:
        claimed_at, reply, owner = claim
        if owner != device_id and now - claimed_at < _DEDUP_WINDOW:
            return True, reply
    _dedup_cache[key] = (now, None, device_id)
    return False, None


def _record_dedup_response(text: str, response: str) -> None:
    """Attach the winner's finished reply to its claim, if it still has one."""
    key = _dedup_key(text)
    claim = _dedup_cache.get(key)
    if claim is not None:
        _dedup_cache[key] = (claim[0], response, claim[2])


# ── Persona file ────────────────────────────────────────────────────────────
# An optional nova_persona.txt replaces the built in character. It is read in
# the executor, at most every _PERSONA_MTIME_TTL seconds, and kept here.

_persona_cache: dict = {"mtime": 0.0, "text": None, "checked": 0.0}
_PERSONA_MTIME_TTL = 30.0


def _persona_file() -> str:
    return PERSONA_FILE or paths.config_path("nova_persona.txt")


def _sync_load_persona() -> tuple[float, str | None]:
    """(mtime, trimmed text or None); (0.0, None) when there is no file.
    Blocking, so only ever run in the executor."""
    path = _persona_file()
    try:
        if not os.path.exists(path):
            return 0.0, None
        stamp = os.path.getmtime(path)
        with open(path) as handle:
            body = handle.read().strip()
    except OSError:
        return 0.0, None
    return stamp, body or None


async def _ensure_persona_loaded(hass: HomeAssistant) -> None:
    """Refresh the cached persona when the file changed. A filled cache is
    only rechecked once its TTL has passed."""
    now = time.time()
    cached = _persona_cache
    if cached["text"] is not None and now - cached["checked"] < _PERSONA_MTIME_TTL:
        return
    cached["checked"] = now
    stamp, body = await hass.async_add_executor_job(_sync_load_persona)
    if cached["text"] is None or stamp != cached["mtime"]:
        cached.update(mtime=stamp, text=body)


# ── Relevance gate vocabulary ───────────────────────────────────────────────
# Verbs that make an imperative on their own ("turn off the lights").
_COMMAND_VERBS = frozenset("""
    turn set lock unlock dim brighten arm disarm activate pause mute unmute
    increase decrease switch enable disable resume lower raise toggle snooze
    remind ignore announce
""".split())
# Verbs as common in ordinary speech as in requests ("give the line", "stop
# it", "make a wish"): a request only with an object such as "me" or "it", a
# known target ("the news") or a home keyword (8.39.1).
_WEAK_VERBS = frozenset("""
    open close shut run play stop start cancel tell show give list check read
    make put add
""".split())
_WEAK_VERB_OBJECTS = frozenset("me us my our it everything all".split())
_WEAK_VERB_TARGETS = frozenset("""
    news post mail messages briefing headlines list agenda schedule radio
    podcast playlist song
""".split())
_QUESTION_STARTS = frozenset("""
    what what's whats when when's where where's who who's why how how's is are
    can could would should do does did will whose which
""".split())
# Device and control nouns. Room names are left out on purpose: they turn up
# in ambient speech, and a real room command nearly always names a device too.
_DOMAIN_KEYWORDS = frozenset("""
    light lights lamp lamps door doors lock locks window windows thermostat
    temperature heat heating cooling alarm scene fan blinds shades cover
    curtains volume plug outlet sensor camera cameras climate brightness
    weather forecast sump dehumidifier washer dryer dishwasher heater boiler
    radiator tv music timer timers reminder reminders calendar garage gate
    doorbell energy electricity solar battery bin bins humidity aircon ac
    vacuum kettle oven socket radio podcast playlist spotify shopping
""".split())
# Words that make a question about the home ("is anyone home?"). Questions
# only: in a statement they are far too common.
_QUESTION_TOPICS = frozenset("""
    home house anyone anybody everyone everybody today tonight tomorrow time
    date day news phone car rain raining cold hot warm outside inside upstairs
    downstairs wifi password code
""".split())
# Things people say to a voice assistant with no command word in them
# ("good night", "I'm cold"), matched at the start.
_HOW_I_AM = ("going to bed|off to bed|heading (?:out|off|to bed)|leaving|going out|"
             "off out|home|back|up|awake|cold|freezing|hot|too hot|too cold|warm|too warm")
_HOME_PHRASES = re.compile("^(?:" + "|".join((
    "good (?:morning|night|evening|afternoon)", "goodnight", "night night",
    "(?:i'?m|i am|we'?re|we are) (?:" + _HOW_I_AM + ")",
    "going to bed", "off to bed", r"(?:it'?s|it is)(?: \w+){1,3} in here",
    "thank you", "thanks", "cheers", "hello", "hi", "hey", "never mind", "nevermind",
)) + r")\b")
# Discourse markers that are never an instruction or an answer by themselves.
# Greetings, yes, no and thanks are real answers, so they are not here.
_FILLER = frozenset("""
    so um uh hmm oh mean like know and but wait well anyway actually i me you
    we they it that this there here
""".split())
# The most words an utterance with no signal at all may have and still pass:
# "the kitchen one" is an answer, a longer one is usually someone else (8.39.1).
_NO_SIGNAL_MAX_WORDS = 3


def _current_time_context(hass, now=None) -> str:
    """The live prompt's date/time line; ``now`` is injectable for tests."""
    if now is None:
        import datetime as _dt
        now = _dt.datetime.now()
    return (f"Current time: {format_date(now, hass)}, "
            f"{now.strftime('%-I:%M %p')}.")


def _offer_answer(text: str | None) -> str | None:
    """"yes" or "no" if the whole utterance is a plain answer to an offer,
    else None. The words are answers.py's, the one list every yes/no
    question reads (8.49.0)."""
    from .answers import yes_or_no
    return yes_or_no(text)


def _turn_user_id(user_input) -> str | None:
    """The Home Assistant user who sent this turn, if it says."""
    return getattr(getattr(user_input, "context", None), "user_id", None)


_AGENTS: "weakref.WeakSet" = weakref.WeakSet()   # every live NovaAgent


def clear_chat_thread(conversation_id: str) -> None:
    """NEW CHAT: every live agent forgets this Chat tab thread. The caller
    deletes its rows from conversations.db."""
    for agent in list(_AGENTS):
        for store in (agent._histories, agent._last_seen):
            store.pop(conversation_id, None)


def _is_addressed_to_nova(text: str, voice: bool = True) -> bool:
    """Whether these words are meant for Nova rather than ambient speech a
    satellite picked up (a TV, people talking, a fragment).

    Yes for: the name "Nova"; a home keyword; a command verb first; a weak
    verb first with an object or a known target, or as a two word request
    ("play radio"); a question that is short, asks for something or is about
    the home; a home phrase. Words with none of these pass only when they are
    a short answer (three words at most) that is not pure filler. A typed turn
    (voice=False) cannot hear a TV, so a longer sentence still passes there
    unless it reads as several sentences of narrative (8.39.1)."""
    lowered = (text or "").strip().lower()
    words = [w for w in (raw.strip(".,!?;:\"…") for raw in lowered.split()) if w]
    if not words:
        return False
    if "nova" in words or not _DOMAIN_KEYWORDS.isdisjoint(words):
        return True
    phrase = " ".join(words)
    if words[0] == "please" and len(words) > 1:
        words = words[1:]
    head, rest = words[0], words[1:]
    if head in _COMMAND_VERBS:
        return True
    if head in _WEAK_VERBS:
        if not rest or rest[0] in _WEAK_VERB_OBJECTS:
            return True                       # "stop", "tell me"
        if not _WEAK_VERB_TARGETS.isdisjoint(rest[:3]):
            return True                       # "read the news"
        return len(words) == 2 and rest[0] not in ("the", "a", "an", "that", "this")
    if head in _QUESTION_STARTS:
        if not voice or len(words) <= 4:
            return True
        polite_request = not _COMMAND_VERBS.isdisjoint(rest) or any(
            verb in _WEAK_VERBS and obj in _WEAK_VERB_OBJECTS
            for verb, obj in zip(rest, rest[1:]))
        return polite_request or not _QUESTION_TOPICS.isdisjoint(words)
    if _HOME_PHRASES.match(phrase):
        return True
    if len(words) <= 3:
        return not all(w.strip("'") in _FILLER for w in words)
    if len(words) <= _NO_SIGNAL_MAX_WORDS:
        return True
    if voice:
        return False
    stops = sum(lowered.count(mark) for mark in ".?!")
    return stops < 2 or len(words) < 8


def _matches_voice_phrase(text: str) -> bool:
    """Whether these words match one of the user's voice phrases. Reads a
    copy of the saved phrases and never raises."""
    try:
        import copy
        from . import nova_config, voice_phrases
        saved = copy.deepcopy(nova_config.get(voice_phrases.CONFIG_KEY))
        return voice_phrases.match(text, saved) is not None
    except Exception:  # pylint: disable=broad-except
        return False


def _is_connectivity_failure(text: str) -> bool:
    """Whether the agent's reply is its own "can't reach my reasoning
    systems" sentence (it returns that rather than raising). An empty reply
    counts too. A real answer that merely mentions connectivity does not."""
    if not text:
        return True
    lowered = text.lower()
    if "reasoning systems" not in lowered:
        return False
    return any(marker in lowered for marker in ("connecting to", "connectivity issues"))


def _ha_kwargs(cls, **kwargs):
    """Only the keyword arguments cls accepts. Home Assistant's LLM classes
    changed fields between versions (2026.8 moved the context into LLMContext
    and dropped user_prompt), so Nova passes the overlap (v7.21.1). When the
    signature can't be read everything is passed. Never raises."""
    try:
        import inspect
        accepted = set(inspect.signature(cls).parameters)
    except Exception:
        return kwargs
    return {name: value for name, value in kwargs.items() if name in accepted}


def _silent(user_input, conversation_id, speech: str = ""):
    """A finished turn that says ``speech`` (nothing by default)."""
    ir = intent.IntentResponse(language=user_input.language)
    ir.async_set_speech(speech)
    return conversation.ConversationResult(response=ir, conversation_id=conversation_id)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    agent = NovaAgent(hass, config_entry)
    _AGENTS.add(agent)
    async_add_entities([agent])


class NovaAgent(conversation.ConversationEntity):
    """Nova as a Home Assistant conversation agent that may control the home."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        # The entry's NovaRuntime owns the LLM client: an entry without one
        # is broken, so this raises NovaRuntimeUnavailable before anything else.
        runtime = get_runtime(entry)
        self.hass = hass
        self.entry = entry
        self._client = runtime.client
        self._histories: dict[str, list[dict]] = {}
        self._last_seen: dict[str, float] = {}   # cid -> its last turn, for the reseed gap
        self._fallback_idx = 0
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name="Nova", manufacturer="Nova",
            model="Nova AI Assistant", sw_version="4.0.0")
        _LOGGER.info("Nova agent initialised — provider=%s, model=%s",
                     getattr(self._client, "name", "unknown"), self._model())

    # ── Settings ─────────────────────────────────────────────────────────────

    def _runtime_config(self) -> dict:
        """The panel's live runtime_config, NovaRuntime's own dict, never a
        copy, so a panel change applies to the next turn. Raises
        NovaRuntimeUnavailable when the entry has no runtime."""
        return get_runtime(self.entry).runtime_config

    def _opt(self, key: str, default=None):
        """A setting: a set (not None or "") runtime value first, then
        nova_config.runtime_get's own order (config.json, options, data,
        default). hass=None keeps runtime_get from looking at the runtime
        a second time."""
        rc = self._runtime_config()
        live = rc.get(key)
        if key in rc and live not in (None, ""):
            return live
        from . import nova_config
        return nova_config.runtime_get(None, self.entry, key, default)

    def _rt_opt(self, key: str, default=None):
        """The same read as _opt, named for the provider and model reads the
        agent re-resolves each turn."""
        return self._opt(key, default)

    def _in_quiet_hours(self) -> bool:
        """Whether now is inside the observer's quiet hours. False on error."""
        try:
            from . import sleep_detection
            start = self._opt("observer_quiet_start", "22:00") or "22:00"
            end = self._opt("observer_quiet_end", "07:00") or "07:00"
            return sleep_detection._in_quiet_hours(start, end)
        except Exception:
            return False

    def _speaker_hint(self, turn_voice, voice_ident) -> str:
        """The agent's hint about who is speaking, only when voice decided it.
        The name is given only when the turn's identity is the same person as
        the voice (8.39.0); otherwise the hint says Nova is not sure."""
        if turn_voice is None or voice_ident is None:
            return ""
        try:
            from . import identity, presence, voice_id
            first, agrees = None, False
            if turn_voice.person:
                full = presence.person_name(self.hass, turn_voice.person) or ""
                first = next(iter(full.split()), None)
                agrees = bool(full) and voice_ident.known and (
                    identity.normalize(voice_ident.person) == identity.normalize(full))
            return voice_id.speaker_hint(turn_voice, first, identity_agrees=agrees)
        except Exception:
            return ""

    def _voice_id_set(self) -> bool:
        """Whether a Nova Voice ID app address is set (8.36.0)."""
        try:
            from .voice_id_store import get_uri
            return bool(get_uri(self.hass, self.entry))
        except Exception:
            return False

    def _satellite_pairings(self) -> dict | None:
        """The panel's satellite to speaker pairings (a dict or its JSON
        text) as a non empty dict, or None when unset, empty, unreadable or
        not a mapping. Read fresh each call."""
        raw = self._runtime_config().get("satellite_pairings")
        if not raw:
            return None
        if isinstance(raw, str):
            import json as _json
            try:
                raw = _json.loads(raw)
            except Exception:
                return None
        return raw if isinstance(raw, dict) and raw else None

    def _model(self) -> str:
        return self._opt(CONF_MODEL, DEFAULT_MODEL)

    def _speakers(self, device_id: str | None = None) -> list[str]:
        """The speakers a direct reply may play on (audio_routing.reply_targets):
        never a voice satellite; the reply speakers, room aware and following
        the panel's pairings; else the broadcast speakers; else the old Cast
        list."""
        def listed(key):
            return self._opt(key, []) or []
        return reply_targets(
            self.hass,
            device_id=device_id,
            voice_satellites=listed(CONF_VOICE_SATELLITES),
            reply_speakers=listed(CONF_REPLY_SPEAKERS),
            broadcast_speakers=listed(CONF_BROADCAST_SPEAKERS),
            legacy_cast_speakers=listed(CONF_CAST_SPEAKERS),
            room_routing=bool(self._opt(CONF_ROOM_ROUTING, DEFAULT_ROOM_ROUTING)),
            satellite_pairings=self._satellite_pairings(),
        )

    def _tts_entity(self) -> str | None:
        return resolve_tts_entity(self.hass, self._opt(CONF_TTS_ENGINE, DEFAULT_TTS_ENGINE))

    def _honorific(self) -> str:
        """How Nova addresses people ("sir" and the like)."""
        return self._opt(CONF_HONORIFIC, DEFAULT_HONORIFIC)

    def _use_hass_api(self) -> bool:
        """Whether the agent gets Home Assistant's Assist tools."""
        return bool(self._opt(CONF_USE_HASS_API, True))

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return "*"

    # ── The prompt ───────────────────────────────────────────────────────────

    def _directive(self) -> str:
        """The prime directive in force: a preset, or the user's own text."""
        return get_directive(self._opt(CONF_DIRECTIVE_PRESET, DEFAULT_DIRECTIVE_PRESET),
                             self._opt(CONF_DIRECTIVE, ""))

    def _live_context(self) -> list[str]:
        """The prompt's live lines: time, temperature unit, presence, faces
        seen, the first weather entity and the home state summary. A source
        that fails is left out."""
        hass = self.hass
        lines = [_current_time_context(hass)]
        try:
            unit = hass.config.units.temperature_unit
        except Exception:
            unit = None
        if unit:
            # Freeform mentions follow the house's unit, not just sensor values.
            lines.append(f"This household uses {unit} for temperature; always express "
                         f"temperatures in {unit}.")
        try:
            who = presence_context_string(hass)
            if who:
                lines.append(f"Presence: {who}")
        except Exception as exc:
            _LOGGER.debug("Nova: presence context error: %s", exc)
        if recognition_context_string is not None:
            try:
                faces = recognition_context_string(hass)
                if faces:
                    lines.append(faces)
            except Exception as exc:
                _LOGGER.debug("Nova: recognition context error: %s", exc)
        try:
            weather = next(iter(hass.states.async_all("weather")), None)
            if weather is not None:
                attrs = weather.attributes
                lines.append(f"Weather: {weather.state}, {attrs.get('temperature')}"
                             f"{attrs.get('temperature_unit', '°')}.")
        except Exception:
            pass
        try:
            from .home_state import get_home_summary
            summary = get_home_summary(hass)
            if summary:
                lines.append(f"\n## Home state snapshot\n{summary}")
        except Exception as exc:
            _LOGGER.debug("Nova: home state summary error: %s", exc)
        return lines

    def _persona(self) -> str:
        """The system prompt: the prime directive first, then the character
        (the persona file's text when there is one), then the live context."""
        from .directive_helper import fill_honorific
        character = _persona_cache.get("text") or NOVA_PERSONA
        prompt = (f"{self._directive()}\n\n---\n\n{character}"
                  "\n\n## Current context (live data)\n" + "\n".join(self._live_context()))
        return fill_honorific(prompt, self._honorific())

    # ── History ──────────────────────────────────────────────────────────────

    def _history(self, cid: str) -> list[dict]:
        """This conversation's turns, trimmed to the last MAX_HISTORY."""
        turns = self._histories.setdefault(cid, [])
        if len(turns) > MAX_HISTORY:
            turns = self._histories[cid] = turns[-MAX_HISTORY:]
        return turns

    async def _maybe_seed_history(self, cid: str, history: list,
                                   subject: str | None = None) -> None:
        """Catch this conversation up on its own recent past: on its first
        turn and whenever it resumes after the configured gap (default 48h),
        decided by memory_thread.should_reseed (11 Sept 2026). The pull
        replaces the window rather than adding to it, so repeated gaps never
        pile up copies. It reads this conversation's history only (v7.87.0),
        and falls back to the person's own history only when ``subject`` is a
        confidently resolved person, never "primary"."""
        from . import memory_thread
        enabled, hours, limit = memory_thread.config()
        now = time.time()
        last_seen = self._last_seen.get(cid)
        self._last_seen[cid] = now
        if not (enabled and memory_thread.should_reseed(last_seen, now, hours)):
            return
        seeded = await memory_thread.load_recent(
            self.hass, hours, limit, device_id=cid, subject=subject)
        if seeded:
            # One background note, not raw turns: raw turns read as live and
            # had Nova re-arguing exchanges finished days ago.
            history[:] = [memory_thread.format_seed_message(seeded)]

    # ── Home Assistant's tools ───────────────────────────────────────────────

    async def _get_hass_api(self, user_input: conversation.ConversationInput):
        """Home Assistant's Assist tools for this turn, or None (chat only)."""
        try:
            fields = _ha_kwargs(
                llm.LLMContext,
                platform=DOMAIN, context=user_input.context, user_prompt=user_input.text,
                language=user_input.language, assistant=conversation.HOME_ASSISTANT_AGENT,
                device_id=user_input.device_id,
            )
            api = await llm.async_get_api(self.hass, "assist", llm.LLMContext(**fields))
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Nova: HA Assist API unavailable (%s) — chat-only mode", exc)
            return None
        _LOGGER.debug("Nova: %d HA tools available", len(api.tools))
        return api

    # ── A turn ───────────────────────────────────────────────────────────────

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log=None,
    ) -> conversation.ConversationResult:
        """Home Assistant's own async_process (final) sets up the chat log and
        calls this, so Nova must not override async_process. Any error in the
        turn is logged with its trace and answered, rather than surfacing as
        Home Assistant's bare "Unexpected error during intent recognition"."""
        try:
            from . import policy, voice_learn
            voice_learn.reset()
            # A Chat tab turn tells the gate so (8.31.0): unlocking, opening,
            # disarming and standing down then need a phone tap, as for voice.
            chat_id = getattr(user_input, "conversation_id", None)
            with policy.chat_turn(chat_id, _turn_user_id(user_input)):
                result = await self._handle_message_impl(user_input, chat_log)
            # The reply exists, so the voice sample may now be learned in the
            # background without delaying it (8.38.0).
            voice_learn.schedule(self.hass, self.entry)
            return result
        except Exception:
            import traceback
            final_line = traceback.format_exc().strip().splitlines()[-1][:200]
            try:
                from .websocket import nova_log
                nova_log("ERROR", f"conversation handler crashed: {final_line}")
            except Exception:
                pass
            _LOGGER.exception("Nova conversation handler crashed")
            return _silent(user_input, getattr(user_input, "conversation_id", None) or "default",
                           "I ran into an internal error handling that request.")

    async def _handle_message_impl(
        self,
        user_input: conversation.ConversationInput,
        chat_log=None,
    ) -> conversation.ConversationResult:
        from .websocket import nova_log
        device_id = getattr(user_input, "device_id", None)
        nova_log("CONV", f"ENTRY text='{user_input.text[:60]}' device={device_id}")
        # Only the length goes to Home Assistant's log, never the words.
        _LOGGER.debug("Nova async_process ENTRY: %d chars, device_id='%s'",
                      len(user_input.text or ""), device_id)
        # An entry that lost its runtime fails here, before any engine, tool
        # or speaker is touched; the wrapper above answers the error.
        get_runtime(self.entry)

        # A Chat tab thread (8.31.0) is nova_chat_<user id>. It skips the
        # relevance gate and the dedup, is never spoken, and belongs to that
        # user alone: another account sending the same id is refused.
        from . import policy
        turn_user = _turn_user_id(user_input)
        is_chat = str(user_input.conversation_id or "").startswith(policy.CHAT_ID_PREFIX)
        if is_chat:
            owner_id = policy.chat_conversation_id(turn_user) if turn_user else None
            if user_input.conversation_id != owner_id:
                nova_log("CONV", "chat thread refused: not this user's conversation")
                return _silent(user_input, user_input.conversation_id,
                               "That chat thread belongs to someone else.")

        voiced = bool(device_id and (user_input.text or "").strip())
        if voiced:
            # Transcribed words reaching Nova prove STT works (v6.70.3).
            try:
                from .diagnostics.service_health import record_usage
                record_usage("stt", True)
            except Exception:
                pass

        # ── Nova Voice ID (8.36.0, 8.37.0) ──────────────────────────────────
        # The voice result the STT entity left for this pipeline, read once.
        # A satellite with a voice recording armed answers the recording and
        # stops. With "use voice to tell who is speaking" on, identity is
        # resolved here once (voice_ident) and reused below; otherwise
        # voice_ident stays None. Phrases and dedup only ever see the words.
        turn_voice = None
        voice_ident = None
        if device_id and not is_chat:
            voice_line = None
            try:
                from . import voice_id as _voice_id
                turn_voice = _voice_id.take_turn_voice()
                # One VOICE line per voice turn, none where Voice ID is unused.
                if turn_voice is not None or self._voice_id_set():
                    voice_line = _voice_id.log_line(turn_voice, device_id)
            except Exception as exc:
                _LOGGER.debug("Nova Voice ID: turn result unavailable: %s", exc)
            from . import voice_enrol
            enrol_reply = await voice_enrol.async_handle_turn(
                self.hass, self.entry, device_id, turn_voice)
            recording = enrol_reply is not None
            if not recording and turn_voice is not None:
                try:
                    from . import identity as _identity
                    if _identity.voice_identity_enabled():
                        voice_ident = _identity.resolve(
                            self.hass, device_id=device_id, voice=turn_voice)
                        if voice_line is not None:
                            voice_line += (f" → {voice_ident.person} ({voice_ident.method}, "
                                           f"{voice_ident.confidence:.2f})")
                        # Whether to learn this sample is decided after the
                        # reply (voice_learn.py, 8.38.0).
                        from . import voice_learn as _voice_learn
                        _voice_learn.offer(device_id, turn_voice, voice_ident)
                except Exception as exc:
                    _LOGGER.debug("Voice identity resolve: %s", exc)
                    voice_ident = None
            if voice_line is not None:
                nova_log("VOICE", voice_line + (" recording" if recording else ""))
            if recording:
                return _silent(user_input, user_input.conversation_id, enrol_reply)

        # ── Several satellites, one request (v5.7.01) ───────────────────────
        is_dup, cached = False, None
        if not is_chat:   # Chat turns are never duplicates and claim nothing
            is_dup, cached = _check_and_claim_dedup(user_input.text, device_id)
        if is_dup:
            _LOGGER.warning("Nova dedup: suppressing duplicate from device=%s "
                            "(already handled)", device_id)
            nova_log("DEDUP", f"suppressed duplicate from {device_id}")
            return _silent(user_input, user_input.conversation_id or device_id or "default",
                           cached or "")

        # ── Presence gate (v5.7.01, off by default since v6.36.0) ───────────
        # Only a satellite in an occupied room answers. Opt in: a slow or
        # flaky room sensor silences the satellite being spoken to, and the
        # dedup already stops several rooms answering. When no room at all
        # reads occupied the sensors are taken to be down and the gate stands
        # aside.
        if device_id and self._opt("presence_gate", False):
            try:
                from homeassistant.helpers import device_registry as _dr
                from .audio_routing import currently_occupied_areas, is_area_occupied
                satellite = _dr.async_get(self.hass).async_get(device_id)
                room = satellite.area_id if satellite else None
                if room and not is_area_occupied(self.hass, room):
                    occupied = currently_occupied_areas(self.hass)
                    if not occupied:
                        _LOGGER.debug("Nova presence gate: no occupied areas detected "
                                      "anywhere — skipping gate (sensors may be offline)")
                    else:
                        _LOGGER.warning("Nova presence gate: suppressing pipeline from "
                                        "device=%s area='%s' (not occupied, occupied=%s)",
                                        device_id, room, occupied)
                        nova_log("GATE", f"suppressed {room} (not occupied, "
                                         f"occupied={occupied})")
                        return _silent(user_input,
                                       user_input.conversation_id or device_id or "default")
            except Exception as exc:
                _LOGGER.debug("Presence gate check failed (non-fatal): %s", exc)

        cid       = user_input.conversation_id or user_input.device_id or "default"
        honorific = self._honorific()
        await _ensure_persona_loaded(self.hass)   # the file read, off the loop
        persona   = self._persona()

        # ── Relevance, decided before anything is remembered ────────────────
        # Ambient speech must not reach history, the database, semantic
        # recall or the knowledge block. A pending offer is read once here and
        # reused by the offer handling below; only a voice turn in the room
        # where it was spoken, within a minute, can answer it (8.39.1).
        try:
            from . import cognitive_core
            pending_offer = (cognitive_core.pending_offer_for(self.hass, device_id)
                             if device_id and not is_chat else None)
        except Exception:
            cognitive_core = None
            pending_offer = None
        # A plain yes or no to that offer passes; anything else said in the
        # room must still be addressed to Nova. Chat turns always are, and so
        # is a configured voice phrase such as "im cold" (8.32.0).
        offer_answer = _offer_answer(user_input.text) if pending_offer else None
        gate_enabled = self._opt("relevance_gate", True)
        is_addressed = (is_chat
                        or _is_addressed_to_nova(user_input.text, voice=bool(device_id))
                        or _matches_voice_phrase(user_input.text))
        relevant = bool(offer_answer) or not gate_enabled or is_addressed

        if not relevant:
            nova_log("GATE", f"ignored ambient input: '{user_input.text.strip()[:60]}'")
            _LOGGER.info("Nova relevance gate: ignored ambient input (%d chars)",
                         len(user_input.text.strip()))
            ir = intent.IntentResponse(language=user_input.language)
            ir.async_set_speech("")
            return conversation.ConversationResult(response=ir, conversation_id=cid)

        # ── Who is speaking, once per turn (Phase 2) ────────────────────────
        # episodic_subject is set only for a confidently named person, so an
        # unresolved speaker never falls back to the shared "primary" bucket.
        # Seeding, semantic memory, knowledge and the command log all reuse
        # this; when it fails they behave as with nobody resolved.
        identity_module = None
        ident = None
        episodic_subject = None
        try:
            from . import identity as identity_module
            if voice_ident is not None:
                ident = voice_ident
            else:
                ident = identity_module.resolve(
                    self.hass, device_id=device_id,
                    # Chat: only the signed in user decides who this is.
                    user_id=turn_user if is_chat else None)
            episodic_subject = identity_module.normalize(ident.person) if ident.known else None
        except Exception as exc:
            _LOGGER.debug("Identity resolve: %s", exc)

        # A satellite Nova reopened itself starts a new conversation id. Within
        # a minute and on the same device it carries on the conversation that
        # asked (8.34.0): its history is copied once and marked seen, so the
        # reseed below can't replace it.
        if device_id and not is_chat and cid not in self._histories:
            try:
                from . import continued_conversation as _cc_carry
                asked_in = _cc_carry.take_reopened(device_id)
                if asked_in and asked_in != cid and self._histories.get(asked_in):
                    self._last_seen[cid] = time.time()
                    self._histories[cid] = list(self._histories[asked_in])
            except Exception as exc:
                _LOGGER.debug("Reopened conversation carry-over skipped: %s", exc)

        history   = self._history(cid)
        await self._maybe_seed_history(cid, history, subject=episodic_subject)
        history.append({"role": "user", "content": user_input.text})
        # The saved row's id links both halves of the exchange; a failed save
        # (None) gives no turn id rather than the text "None".
        user_row_id = await self.hass.async_add_executor_job(
            save_message, "user", user_input.text, cid, episodic_subject)
        turn_id = str(user_row_id) if user_row_id is not None else None

        # Recall first, then store, so this message is never its own best
        # match. Each is fail open on its own.
        mem_context = ""
        try:
            from .memory import get_conversation_context
            mem_context = await self.hass.async_add_executor_job(
                get_conversation_context, user_input.text, 3, cid, episodic_subject)
        except Exception as exc:
            _LOGGER.debug("Memory retrieve: %s", exc)
        if mem_context:
            persona = persona + "\n\n" + mem_context
        try:
            from .memory import store_memory
            await self.hass.async_add_executor_job(
                lambda: store_memory(user_input.text, role="user",
                                     conversation_id=cid, device_id=user_input.device_id or "",
                                     turn_id=turn_id, subject=episodic_subject))
        except Exception as exc:
            _LOGGER.debug("Memory store: %s", exc)

        # Known facts for this person and the household (v6.25.0, v6.29.0),
        # by meaning when semantic search is on, else by keyword. With no
        # identity at all the whole block is skipped for the turn.
        kn_block = None
        if ident is not None:
            try:
                from . import fact_recall, knowledge
                subjects = [identity_module.subject_for(ident), "household"]
                kn_block = await fact_recall.semantic_prompt_block(
                    self.hass, user_input.text, subjects=subjects)
                if kn_block is None:
                    kn_block = await self.hass.async_add_executor_job(
                        lambda: knowledge.prompt_block(user_input.text, subjects=subjects))
            except Exception as exc:
                _LOGGER.debug("Knowledge inject: %s", exc)
        extras = [kn_block]
        # Confirmed relations only, household wide (8.7.13).
        try:
            from . import knowledge as _knowledge
            extras.append(await self.hass.async_add_executor_job(
                lambda: _knowledge.relations_prompt_block(user_input.text)))
        except Exception as exc:
            _LOGGER.debug("Relations inject: %s", exc)
        # Voice phrases the agent may choose by meaning (8.32.0).
        try:
            from . import voice_phrase_runner as _vpr
            extras.append(_vpr.prompt_block())
        except Exception as exc:
            _LOGGER.debug("Voice phrases inject: %s", exc)
        # A satellite's reply is spoken, so it is kept short (8.34.0).
        if device_id and not is_chat:
            try:
                from .voice_confirm import is_voice_satellite_device
                if is_voice_satellite_device(self.hass, device_id):
                    extras.append(VOICE_REPLY_RULE)
            except Exception as exc:
                _LOGGER.debug("Voice reply rule skipped: %s", exc)
        for block in extras:
            if block:
                persona += "\n\n" + block

        hass_api = await self._get_hass_api(user_input) if self._use_hass_api() else None
        addr = f", {honorific}" if honorific else ""   # "" when nobody is addressed

        # ── A held request's yes (8.33.0) ───────────────────────────────────
        # Only a voice turn from the satellite that holds the request reads
        # it. Checked before the offer below, which would take any "yes".
        confirm_result = None
        if device_id and not is_chat:
            try:
                from . import pending_confirm
                confirm_result = await pending_confirm.answer(
                    self.hass, device_id, user_input.text, honorific=honorific,
                    user_id=turn_user)
            except Exception as exc:
                _LOGGER.debug("Pending confirmation answer skipped: %s", exc)
            if confirm_result is not None:
                from . import voice_learn as _voice_learn
                _voice_learn.block("confirmation answer")

        # ── A proactive offer's answer (v5.9.07) ────────────────────────────
        # The offer read before the relevance decision; any reply that is not
        # a plain yes or no declines it.
        offer_reply = None
        try:
            if pending_offer and confirm_result is None:
                offer_id = pending_offer.get("offer_id")
                who_answered = f"by {turn_user or 'unknown'} on {device_id}"
                if offer_answer == "yes":
                    outcome = await cognitive_core.accept_pending_offer(offer_id)
                    if outcome.get("ok"):
                        offer_reply = f"Done{addr}."
                    elif outcome.get("reason") == "offer replaced":
                        offer_reply = f"That offer has passed{addr}, so I've left it."
                    else:
                        offer_reply = f"I wasn't able to complete that{addr}."
                    nova_log("OFFER", f"accepted {offer_id} {who_answered}: {offer_reply[:60]}")
                else:
                    cognitive_core.decline_pending_offer(offer_id)
                    if offer_answer == "no":
                        offer_reply = f"Understood{addr}. I'll leave it."
                        nova_log("OFFER", f"declined {offer_id} {who_answered}")
        except Exception as exc:
            _LOGGER.debug("Offer handling skipped: %s", exc)

        if offer_reply is not None:
            from . import voice_learn as _voice_learn
            _voice_learn.block("offer answer")
            ir = intent.IntentResponse(language=user_input.language)
            ir.async_set_speech(offer_reply.replace("{honorific}", honorific))
            return conversation.ConversationResult(response=ir, conversation_id=cid)

        # The routing gate (v5.9.16, v7.105.0): reached only when an offer
        # made the turn relevant for memory but the words were neither its
        # answer nor addressed to Nova. Same values as the decision above.
        if gate_enabled and not is_addressed:
            nova_log("GATE", f"ignored ambient input: '{user_input.text.strip()[:60]}'")
            _LOGGER.info("Nova relevance gate: ignored ambient input (%d chars)",
                         len(user_input.text.strip()))
            return _silent(user_input, cid)

        # Every early return is behind us: Nova answers, so the voice sample
        # may be learned (8.38.0).
        from . import voice_learn as _voice_learn
        _voice_learn.mark_answered()

        cast_routed = False                  # a paired speaker is saying the reply
        reopen_speaker: str | None = None    # that speaker
        reopen_device: str | None = None     # the satellite that heard the turn
        try:
            # The user's voice phrases come first (8.32.0), then the local
            # engine (v5.7.00), and the agent only for what neither handles.
            # A held request's yes is the reply itself.
            from .local_engine import try_local, score_complexity
            from . import voice_phrase_runner
            if confirm_result is not None:
                phrase = voice_phrase_runner.PhraseStep(confirm_result, None)
            else:
                phrase = await voice_phrase_runner.try_phrase(
                    self.hass, user_input.text, honorific=honorific,
                    device_id=device_id, user_id=turn_user)
            local_result = phrase.result
            # A phrase that needs confirming skips the local engine and asks
            # the agent to confirm instead.
            if local_result is None and not phrase.defer_hint:
                local_result = await try_local(
                    self.hass, user_input.text, honorific, device_id=device_id)
            if phrase.defer_hint:
                persona = persona + "\n\n" + phrase.defer_hint

            handled_locally = bool(local_result and local_result.handled)
            if local_result and local_result.handled:
                response_text = local_result.text
                _LOGGER.info("Nova local: %s", response_text[:100])
                nova_log("LOCAL", f"handled: {response_text[:80]}")
            else:
                from . import connectivity
                complexity = score_complexity(user_input.text)
                if not connectivity.allow_request():
                    # The breaker is open (v5.9.06): don't call a provider known
                    # to be down; answer what can be answered offline.
                    nova_log("OFFLINE", f"LLM down — local salvage: {user_input.text[:60]}")
                    salvage = await try_local(self.hass, user_input.text, honorific,
                                              force=True, device_id=device_id)
                    if salvage and salvage.handled:
                        response_text = salvage.text
                        _LOGGER.info("Nova offline-salvage: %s", response_text[:100])
                    else:
                        response_text = (
                            f"I'm offline at the moment{addr}, so I can't handle that "
                            "request — it needs my reasoning systems. I can still "
                            "control your devices, report status, and run scenes. "
                            "I'll be back to full capability once connectivity returns.")
                else:
                    nova_log("AGENT", f"LLM needed (complexity={complexity}): "
                                      f"{user_input.text[:60]}")
                    from .agent import run_agent
                    from . import nova_config as _jc
                    provider_name = self._rt_opt("llm_provider", "groq")
                    model_val = self._rt_opt(CONF_MODEL, DEFAULT_MODEL)
                    # The reasoning fallback needs the whole config: entry data
                    # and options are empty on a panel set up install. The
                    # executor gets a snapshot of the live runtime values,
                    # never the loop's own dict.
                    eff_config = await self.hass.async_add_executor_job(
                        _jc.effective_config_with_runtime, self.entry,
                        runtime_config_snapshot(self.entry, strict=True))
                    credential = resolve_provider_credential(eff_config, provider_name)
                    endpoint = resolve_provider_endpoint(eff_config, provider_name)
                    try:
                        response_text = await run_agent(
                            self.hass,
                            messages=history,
                            persona=persona,
                            # 8.37.0, with voice identity on: who is speaking,
                            # for the situation block and for remembered facts.
                            speaker_hint=self._speaker_hint(turn_voice, voice_ident),
                            identification=voice_ident,
                            provider_name=provider_name,
                            api_key=credential,
                            model=model_val,
                            base_url=endpoint,
                            hass_api=hass_api,
                            user_input=user_input,
                            temperature=0.7,
                            config=eff_config,
                        )
                        if not _is_connectivity_failure(response_text):
                            connectivity.record_success()
                        else:
                            # The agent's own offline sentence: a failure, and
                            # the local engine gets a try before it is spoken.
                            connectivity.record_failure()
                            salvage = await try_local(self.hass, user_input.text, honorific,
                                                      force=True, device_id=device_id)
                            if salvage and salvage.handled:
                                response_text = salvage.text
                    except Exception as agent_exc:  # pylint: disable=broad-except
                        _LOGGER.warning("Agent call raised: %s", agent_exc)
                        connectivity.record_failure()
                        salvage = await try_local(self.hass, user_input.text, honorific,
                                                  force=True, device_id=device_id)
                        response_text = salvage.text if salvage and salvage.handled else (
                            f"I've lost connection to my reasoning systems{addr}. I can "
                            "still control devices and report status while I reconnect.")
            from .directive_helper import fill_honorific
            response_text = speech_text(fill_honorific(response_text, honorific))
            # Late duplicates from other satellites get this reply (v5.7.01).
            _record_dedup_response(user_input.text, response_text)

            # The command log for pattern learning (v5.8.03), under the person
            # resolved above (v6.29.0), "unknown" otherwise.
            try:
                from . import cognitive_core, voice_recognition
                if ident is not None:
                    who = ident.person
                else:
                    who = identity_module.UNKNOWN if identity_module is not None else "unknown"
                cognitive_core.log_command(text=user_input.text, person=who,
                                           handled_by="local" if handled_locally else "agent")
                # The legacy voice sensor may label its pending sample when
                # other signals know the speaker. Never on a Voice ID turn.
                if turn_voice is None:
                    voice_recognition.maybe_fire_enrollment(self.hass, device_id)
            except Exception:
                pass

            # ── The reply on the room's speaker (v5.7.03) ───────────────────
            try:
                if device_id and not is_chat:
                    from .audio_routing import reply_target
                    speaker = reply_target(self.hass, device_id=device_id,
                                           satellite_pairings=self._satellite_pairings())
                    if not speaker or speaker.startswith(_SATELLITE):
                        # A satellite that can't play audio, or a room with no
                        # speaker: use the first configured reply speaker that
                        # is up, as proactive audio does.
                        for candidate in self._speakers(device_id):
                            state = (None if candidate.startswith(_SATELLITE)
                                     else self.hass.states.get(candidate))
                            if state is not None and state.state not in _UNREACHABLE:
                                _LOGGER.info("Nova reply: satellite can't speak, using "
                                             "configured reply speaker %s", candidate)
                                speaker = candidate
                                break
                    if not speaker or speaker.startswith(_SATELLITE):
                        _LOGGER.info("Nova reply: no Cast pairing for device=%s, "
                                     "pipeline speaker fallback", device_id)
                        try:
                            nova_log("REPLY", f"no Cast pairing for device={device_id} "
                                              f"— satellite plays (resolved={speaker})")
                        except Exception:
                            pass
                    else:
                        tts_ent = resolve_tts_entity(self.hass, self._opt("tts_engine", "auto"))
                        found = self.hass.states.get(speaker)
                        reachable = found is not None and found.state not in _UNREACHABLE
                        if not reachable:
                            # Silencing the satellite now would lose the reply.
                            _LOGGER.warning("Nova reply: Cast speaker %s is unavailable — "
                                            "the satellite will speak the reply instead",
                                            speaker)
                            log_line = f"speaker {speaker} unavailable — satellite will speak"
                        elif not tts_ent:
                            _LOGGER.warning("Nova reply: no TTS entity found")
                            log_line = "no TTS entity — satellite will speak"
                        else:
                            _LOGGER.info("Nova reply → Cast: tts=%s speaker=%s",
                                         tts_ent, speaker)
                            # Fire and forget, then the satellite stays quiet.
                            # Waiting for a "playing" state (v7.50, v7.51) broke
                            # idle Cast speakers that never report it.
                            # Spoken History (v7.104.0): only this path knows
                            # the reply was delivered, so only here is a
                            # "repeat that" answer recorded, as a repeat of the
                            # original rather than a new reply.
                            repeat_of_id = (local_result.repeat_of_id if local_result
                                            and getattr(local_result, "repeat_of_id", None)
                                            else None)
                            self.hass.async_create_task(async_announce(
                                self.hass, response_text, tts_ent, [speaker],
                                context="repeat" if repeat_of_id else "reply",
                                repeat_of_id=repeat_of_id))
                            cast_routed = True
                            reopen_speaker, reopen_device = speaker, device_id
                            log_line = f"→ Cast {speaker} via {tts_ent}"
                        try:
                            nova_log("REPLY", log_line)
                        except Exception:
                            pass
            except Exception as exc:
                _LOGGER.warning("Reply routing error: %s", exc)

            history.append({"role": "assistant", "content": response_text})
            # The same subject as the user's row, so both halves can be found
            # again; this row is never a turn id itself.
            with contextlib.suppress(Exception):
                await self.hass.async_add_executor_job(save_message, "assistant",
                                                       response_text, cid, episodic_subject)
            _LOGGER.debug("Nova → %s", response_text[:120])
            # The reply's semantic record carries the user message's turn id,
            # pairing the two. A turn that ended early leaves its user half
            # unpaired, and a later reply always has its own id.
            try:   # store_memory was imported for the user's half above
                await self.hass.async_add_executor_job(
                    lambda: store_memory(response_text, role="assistant",
                                         subject=episodic_subject, turn_id=turn_id,
                                         device_id=user_input.device_id or "",
                                         conversation_id=cid))
            except Exception:
                pass

        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.error("Nova API error (%s): %s | model=%s",
                          type(exc).__name__, exc, self._model())
            from .directive_helper import fill_honorific
            canned = _FALLBACKS[self._fallback_idx % len(_FALLBACKS)]
            self._fallback_idx += 1
            response_text = fill_honorific(canned, honorific)
            if history and history[-1]["role"] == "user":
                history.pop()

        # A paired speaker has the reply, so the satellite says nothing;
        # otherwise the pipeline speaks it on the satellite.
        result = _silent(user_input, cid, "" if cast_routed else response_text)

        # Keep listening when Nova invited a follow up (v6.88.0).
        try:
            from . import continued_conversation as _cc
            from . import pending_confirm as _pc
            # A held request always reopens this same satellite for its yes
            # or no (8.33.0), after this pipeline has finished.
            held = bool(device_id and not is_chat and _pc.get(device_id))
            # In quiet hours only a held request reopens the mic (8.39.1).
            follow_up = (not is_chat and _cc.enabled() and _cc.should_continue(response_text)
                         and not self._in_quiet_hours())
            if not is_chat and (held or follow_up):
                sat_ent = (_cc.satellite_for_device(self.hass, reopen_device)
                           if reopen_device else None)
                watch_speaker = (cast_routed and reopen_speaker and sat_ent
                                 and (held or _cc.speaker_reopen_enabled()))
                if not watch_speaker:
                    # The satellite plays the reply, so Home Assistant's own
                    # reopen timing is right.
                    result.continue_conversation = True
                elif _cc.schedule_reopen(self.hass, sat_ent, reopen_speaker, response_text,
                                         follow=not held):
                    # Home Assistant would reopen as soon as the satellite's
                    # own (instant) playback ended and hear Nova's reply on
                    # the speaker, so the mic opens once the speaker is idle.
                    # The answer arrives under a new id (8.34.0).
                    _cc.remember_reopen(reopen_device, cid)
        except Exception:
            pass
        return result
