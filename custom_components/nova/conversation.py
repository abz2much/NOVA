"""Nova conversation agent — provider-agnostic via LLMProvider interface."""
from __future__ import annotations

import logging
import os
import time
from typing import Literal

from homeassistant.components import conversation
from homeassistant.components.conversation import ConversationEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, llm
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ALL_SPEAKERS_VALUE,
    CONF_BROADCAST_SPEAKERS,
    CONF_CAST_ANNOUNCE,
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
from .audio_routing import reply_targets
from .database import save_message
from .llm_provider import (
    resolve_provider_credential,
    resolve_provider_endpoint,
)
from .presence import presence_context_string
from .runtime import get_runtime
from .tts_helper import resolve_tts_entity, async_announce


# ── Multi-wake dedup ────────────────────────────────────────────────────────
# When multiple satellites hear "Hey Nova" simultaneously, HA fires
# separate pipelines for each. This dedup ensures only the FIRST pipeline
# actually processes the request and routes audio. Subsequent duplicates
# within the window get a silent cached response (no duplicate TTS).

_DEDUP_WINDOW = 4.0  # seconds — covers STT variance between satellites
_dedup_cache: dict[str, tuple[float, str | None, str | None]] = {}
# key = normalized text, value = (timestamp, response_text_or_None, winning_device_id)


def _dedup_key(text: str) -> str:
    """Normalize text for dedup comparison."""
    return text.lower().strip().rstrip(".,!?")


def _check_and_claim_dedup(text: str, device_id: str | None) -> tuple[bool, str | None]:
    """
    Check if this is a duplicate wake-up AND claim the slot if not.

    Returns (is_duplicate, cached_response_or_None).
    If is_duplicate is True, caller should return a silent response without
    processing or routing audio again.
    """
    key = _dedup_key(text)
    now = time.time()

    # Clean old entries
    stale = [k for k, (ts, _, _) in _dedup_cache.items() if now - ts > _DEDUP_WINDOW * 2]
    for k in stale:
        _dedup_cache.pop(k, None)

    if key in _dedup_cache:
        ts, cached_resp, winning_device = _dedup_cache[key]
        if now - ts < _DEDUP_WINDOW and device_id != winning_device:
            # Another device already claimed this text
            return True, cached_resp

    # Claim the slot — first pipeline to arrive wins
    _dedup_cache[key] = (now, None, device_id)
    return False, None


def _record_dedup_response(text: str, response: str) -> None:
    """Update the cached response after processing completes."""
    key = _dedup_key(text)
    if key in _dedup_cache:
        ts, _, device_id = _dedup_cache[key]
        _dedup_cache[key] = (ts, response, device_id)

try:
    from .recognition import recognition_context_string
    _RECOGNITION_CTX = True
except ImportError:
    _RECOGNITION_CTX = False

_LOGGER = logging.getLogger(__name__)

MAX_HISTORY  = 20   # messages kept in per-conversation context window
MAX_ITERS    = 8    # max agentic tool-call iterations per request
PERSONA_FILE = "/config/nova_persona.txt"

# Module-level persona cache. Loaded lazily via executor to avoid blocking
# the event loop with file I/O on every conversation turn. Invalidated
# automatically when the file's mtime changes.
_persona_cache: dict = {"mtime": 0.0, "text": None, "checked": 0.0}
_PERSONA_MTIME_TTL = 30.0  # re-stat at most every 30s


_COMMAND_VERBS = {
    "turn", "set", "lock", "unlock", "open", "close", "shut", "dim", "brighten",
    "arm", "disarm", "activate", "run", "play", "pause", "stop", "mute", "unmute",
    "increase", "decrease", "switch", "enable", "disable", "start", "resume",
    "lower", "raise", "toggle", "cancel", "snooze", "remind", "ignore",
    "tell", "show", "give", "list", "check", "read", "announce", "make",
}
_QUESTION_STARTS = {
    "what", "what's", "whats", "when", "when's", "where", "where's", "who",
    "who's", "why", "how", "how's", "is", "are", "can", "could", "would",
    "should", "do", "does", "did", "will", "whose", "which",
}
# Device/control nouns that signal a genuine home command. Deliberately EXCLUDES
# bare room names (basement, kitchen…) — those appear in ambient speech too, and
# a real room command almost always also carries a device or action word.
_DOMAIN_KEYWORDS = {
    "light", "lights", "lamp", "lamps", "door", "doors", "lock", "locks",
    "window", "windows", "thermostat", "temperature", "heat", "heating",
    "cooling", "alarm", "scene", "fan", "blinds", "shades", "cover", "curtains",
    "volume", "plug", "outlet", "sensor", "camera", "climate", "brightness",
    "weather", "sump", "dehumidifier", "washer", "dryer", "thermostat",
}
_FILLER = {
    # Pure discourse markers / sentence fragments that are NEVER a valid
    # standalone instruction or answer. Deliberately EXCLUDES greetings (hi,
    # hello), affirmatives/negatives (yes, no, sure, okay) and acknowledgments
    # (thanks) — those are real inputs (greeting handler, yes/no answers) and
    # must pass the gate.
    "so", "um", "uh", "hmm", "oh", "mean", "like", "know", "and", "but",
    "wait", "well", "anyway", "actually", "i", "me", "you", "we", "they",
    "it", "that", "this", "there", "here",
}


def _is_addressed_to_nova(text: str) -> bool:
    """
    Relevance gate: does this utterance look like it's actually addressed to
    Nova (a command or question), versus ambient speech a satellite happened
    to transcribe — TV dialogue, background conversation, sentence fragments?

    Tuned for HIGH PRECISION on the PASS side: it never drops anything carrying
    a command verb, a question word, a device keyword, or the name "Nova", so
    a real instruction is never rejected. It only filters input that has NONE of
    those signals and clearly reads as filler or a stray fragment. The PRIMARY
    defense against TV noise is wake-word gating on the satellite; this is a
    backstop for whatever slips through. Returns True = process, False = ignore.
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    words = t.split()

    # ── Positive signals — any one means: definitely process ──
    if "nova" in words:
        return True
    if words[0].strip(".,!?") in _COMMAND_VERBS:
        return True          # imperative — also protects compound commands
    if words[0].strip(".,!?") in _QUESTION_STARTS:
        return True          # a question — including multi-part ones
    if any(w.strip(".,!?") in _DOMAIN_KEYWORDS for w in words):
        return True          # mentions a device/control noun

    # ── No command signal at all. Reject obvious non-commands. ──
    # Bare interjection / filler ("So", "Yeah", "I mean", "Wait but")
    if len(words) <= 3 and all(w.strip(".,!?'") in _FILLER for w in words):
        return False
    # Rambling narrative with no command structure (TV dialogue tends to span
    # multiple clauses). Only applies when NO positive signal was found above.
    sentence_breaks = t.count(".") + t.count("?") + t.count("!")
    if sentence_breaks >= 2 and len(words) >= 8:
        return False

    # Default: PASS. Better to occasionally answer ambient speech than to drop
    # a real request — wake-word gating is the real filter.
    return True


def _is_connectivity_failure(text: str) -> bool:
    """
    Detect the sentinel strings run_agent returns when ALL providers fail.

    run_agent never raises on network failure — it returns an apologetic
    string. We match on the stable phrase fragments so a real LLM answer
    that happens to mention connectivity isn't misclassified (those sentinels
    always pair 'reasoning systems' with a connectivity phrase).
    """
    if not text:
        return True
    t = text.lower()
    return "reasoning systems" in t and (
        "connecting to" in t or "connectivity issues" in t
    )


def _sync_load_persona() -> tuple[float, str | None]:
    """Synchronous persona file read. Must be called from an executor thread."""
    try:
        if os.path.exists(PERSONA_FILE):
            mtime = os.path.getmtime(PERSONA_FILE)
            with open(PERSONA_FILE) as f:
                text = f.read().strip() or None
            return mtime, text
    except OSError:
        pass
    return 0.0, None


async def _ensure_persona_loaded(hass: HomeAssistant) -> None:
    """Check & refresh the persona cache if needed. Called from async context."""
    import time as _time
    now = _time.time()
    # Rate-limit mtime checks so we don't stat every turn
    if (now - _persona_cache["checked"]) < _PERSONA_MTIME_TTL and _persona_cache["text"] is not None:
        return
    _persona_cache["checked"] = now
    mtime, text = await hass.async_add_executor_job(_sync_load_persona)
    if mtime != _persona_cache["mtime"] or _persona_cache["text"] is None:
        _persona_cache["mtime"] = mtime
        _persona_cache["text"] = text


_FALLBACKS = [
    "Technical difficulties, {honorific}. Even I have them occasionally. Please try again.",
    "I appear to be having connectivity issues, {honorific}. Bear with me.",
    "Something is interfering with my systems, {honorific}. One moment.",
]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([NovaAgent(hass, config_entry)])


class NovaAgent(conversation.ConversationEntity):
    """
    Nova conversation agent — Groq-powered, HA home control.
    ConversationEntityFeature.CONTROL enables home device control.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        # The entry's NovaRuntime owns the LLM client. A loaded entry without
        # one is broken, so construction fails here (NovaRuntimeUnavailable)
        # rather than building a second provider from stored config.
        runtime = get_runtime(entry)
        self.hass  = hass
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nova",
            manufacturer="Nova",
            model="Nova AI Assistant",
            sw_version="4.0.0",
        )
        self._histories: dict[str, list[dict]] = {}
        self._last_seen: dict[str, float] = {}   # cid -> epoch seconds of its last turn, for gap-based reseed
        self._fallback_idx = 0

        # The shared LLM client setup built for this entry (same object as
        # entry.runtime_data.client), so conversation honours the chosen
        # backend (Groq/OpenAI/Anthropic/Ollama/custom) with no code here.
        self._client = runtime.client
        _LOGGER.info(
            "Nova agent initialised — provider=%s, model=%s",
            getattr(self._client, "name", "unknown"),
            self._model(),
        )

    # ── Config helpers ────────────────────────────────────────────────────────

    def _runtime_config(self) -> dict:
        """The panel's live runtime_config: NovaRuntime's own dict, read on
        every call and never copied, so in-place panel writes apply to the
        next turn. Raises NovaRuntimeUnavailable when the entry has no
        runtime. Never reads the compatibility bridge."""
        return get_runtime(self.entry).runtime_config

    def _opt(self, key: str, default=None):
        """Config read with the canonical precedence: runtime_config →
        config.json → options → data → default.

        A set (not None/"") runtime value wins, as in nova_config.runtime_get.
        Otherwise runtime_get resolves the rest; hass=None skips its bridge
        lookup, so config.json/options/data/default behave exactly as before."""
        rc = self._runtime_config()
        if key in rc and rc[key] not in (None, ""):
            return rc[key]
        from . import nova_config
        return nova_config.runtime_get(None, self.entry, key, default)

    def _rt_opt(self, key: str, default=None):
        """
        Runtime-aware read, same precedence as _opt: panel runtime_config →
        config.json → options → data → default. The panel writes model/provider
        changes to runtime_config, so those take effect on the next request
        without a restart (run_agent re-resolves provider/model per call).
        """
        return self._opt(key, default)

    def _satellite_pairings(self) -> dict | None:
        """The panel's satellite → speaker pairings from the live
        runtime_config (a dict, or its JSON string). Returns a non-empty dict,
        or None when unset, empty, malformed or not a mapping. Not cached."""
        raw = self._runtime_config().get("satellite_pairings")
        if not raw:
            return None
        try:
            import json as _json
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return None
        if isinstance(parsed, dict) and parsed:
            return parsed
        return None

    def _model(self) -> str:
        return self._opt(CONF_MODEL, DEFAULT_MODEL)

    def _honorific(self) -> str:
        return self._opt(CONF_HONORIFIC, DEFAULT_HONORIFIC)

    def _use_hass_api(self) -> bool:
        return bool(self._opt(CONF_USE_HASS_API, True))

    def _broadcast_fallback_speakers(self) -> list[str]:
        """The broadcast/announcement speakers briefings deliver to (e.g. a Cast
        group). Used as a reply fallback when the paired room speaker won't play —
        an idle/off Cast device that accepts tts.speak and produces no sound, or a
        mic-only satellite that can't speak the reply itself. Resolved directly
        (not room/pairing-aware) so it can never resolve back to the same
        non-playing speaker."""
        import json as _json
        for key in ("announcement_speakers", CONF_BROADCAST_SPEAKERS,
                    CONF_CAST_SPEAKERS, "broadcast_group"):
            raw = self._opt(key, None)
            if not raw:
                continue
            if isinstance(raw, str):
                raw = raw.strip()
                if raw.startswith("["):
                    try:
                        raw = _json.loads(raw)
                    except Exception:
                        raw = [raw]
                else:
                    raw = [raw]
            if isinstance(raw, (list, tuple)):
                out = [str(x) for x in raw
                       if x and not str(x).startswith("assist_satellite.")]
                if out:
                    return out
        return []

    def _speakers(self, device_id: str | None = None) -> list[str]:
        """
        Choose which speakers to broadcast a DIRECT REPLY to.

        Uses the three-tier audio architecture (audio_routing.reply_targets):
          - voice_satellites are excluded from speaking
          - reply_speakers used for direct conversation
          - room-aware: speak to kitchen puck → reply via kitchen Google
          - falls back to broadcast_speakers if no reply_speakers configured
          - falls back to legacy cast_speakers for backward compatibility
          - satellite_pairings from panel Settings override area registry
        """
        sat_pairings = self._satellite_pairings()

        return reply_targets(
            self.hass,
            device_id=device_id,
            voice_satellites=self._opt(CONF_VOICE_SATELLITES, []) or [],
            reply_speakers=self._opt(CONF_REPLY_SPEAKERS, []) or [],
            broadcast_speakers=self._opt(CONF_BROADCAST_SPEAKERS, []) or [],
            legacy_cast_speakers=self._opt(CONF_CAST_SPEAKERS, []) or [],
            room_routing=bool(self._opt(CONF_ROOM_ROUTING, DEFAULT_ROOM_ROUTING)),
            satellite_pairings=sat_pairings,
        )

    def _satellite_speaker(self, device_id: str) -> str | None:
        """
        Given the device_id of the wake-word/voice satellite, return its
        associated media_player entity (the same physical box).
        """
        try:
            from homeassistant.helpers import device_registry as dr, entity_registry as er
            ent_reg = er.async_get(self.hass)
            for ent in ent_reg.entities.values():
                if ent.device_id == device_id and ent.domain == "media_player":
                    if self.hass.states.get(ent.entity_id):
                        return ent.entity_id
        except Exception as exc:
            _LOGGER.debug("Nova: satellite speaker lookup error: %s", exc)
        return None

    def _tts_entity(self) -> str | None:
        return resolve_tts_entity(self.hass, self._opt(CONF_TTS_ENGINE, DEFAULT_TTS_ENGINE))

    def _use_announce(self) -> bool:
        return bool(self._opt(CONF_CAST_ANNOUNCE, True))

    # ── Supported languages ───────────────────────────────────────────────────

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return "*"

    # ── Persona ───────────────────────────────────────────────────────────────

    def _directive(self) -> str:
        """Return the active prime directive text."""
        preset = self._opt(CONF_DIRECTIVE_PRESET, DEFAULT_DIRECTIVE_PRESET)
        custom = self._opt(CONF_DIRECTIVE, "")
        return get_directive(preset, custom)

    def _persona(self) -> str:
        """Build the full system prompt.

        Order matters: prime directive FIRST (unrelenting core purpose),
        then character/style, then live context. The directive appears
        at the top of every single LLM call.
        """
        base = NOVA_PERSONA
        cached = _persona_cache.get("text")
        if cached:
            base = cached

        # Prime directive always comes first
        directive = self._directive()
        base = f"{directive}\n\n---\n\n{base}"

        # Inject live context — time, presence, weather summary
        import datetime as _dt
        ctx_parts = [f"Current time: {_dt.datetime.now().strftime('%A %B %-d, %-I:%M %p')}."]

        # Temperature unit, so freeform mentions (not just quoted sensor values)
        # follow the household's Home Assistant unit system rather than defaulting
        # to Fahrenheit.
        try:
            _tu = self.hass.config.units.temperature_unit
            if _tu:
                ctx_parts.append(
                    f"This household uses {_tu} for temperature; always express "
                    f"temperatures in {_tu}."
                )
        except Exception:
            pass

        try:
            presence = presence_context_string(self.hass)
            if presence:
                ctx_parts.append(f"Presence: {presence}")
        except Exception as exc:
            _LOGGER.debug("Nova: presence context error: %s", exc)

        # Recent face recognitions
        if _RECOGNITION_CTX:
            try:
                faces = recognition_context_string(self.hass)
                if faces:
                    ctx_parts.append(faces)
            except Exception as exc:
                _LOGGER.debug("Nova: recognition context error: %s", exc)

        # Weather summary
        try:
            for state in self.hass.states.async_all("weather"):
                temp = state.attributes.get("temperature")
                unit = state.attributes.get("temperature_unit", "°")
                ctx_parts.append(f"Weather: {state.state}, {temp}{unit}.")
                break
        except Exception:
            pass

        # v5.6.0: Full home state awareness (HGA-inspired)
        try:
            from .home_state import get_home_summary
            home_summary = get_home_summary(self.hass)
            if home_summary:
                ctx_parts.append(f"\n## Home state snapshot\n{home_summary}")
        except Exception as exc:
            _LOGGER.debug("Nova: home state summary error: %s", exc)

        context_block = "\n\n## Current context (live data)\n" + "\n".join(ctx_parts)
        from .directive_helper import fill_honorific
        return fill_honorific(base + context_block, self._honorific())

    # ── Conversation history ──────────────────────────────────────────────────

    def _history(self, cid: str) -> list[dict]:
        h = self._histories.setdefault(cid, [])
        if len(h) > MAX_HISTORY:
            self._histories[cid] = h[-MAX_HISTORY:]
        return self._histories[cid]

    async def _maybe_seed_history(self, cid: str, history: list,
                                   subject: str | None = None) -> None:
        """Seed this conversation with recent cross-session history — on its
        first-ever turn, and again any time it resumes after being idle past
        the configured window (default 48h) — so Nova keeps catching up
        instead of only ever catching up once (fixed 11 Sept 2026; see
        memory_thread.should_reseed). A reseed REPLACES this thread's window
        with the fresh pull rather than prepending onto it, so a thread that
        keeps resuming after gaps can't accumulate duplicate history.

        Scoped to this conversation's own history (v7.87.0, backlog #1) — the
        reseed used to pull globally across every device/conversation in the
        house, so one household member's exchange could leak into another's
        session on reseed.

        `subject` (Phase 2) — person-scoped fallback, used by
        memory_thread.load_recent only when this conversation_id's own
        history comes up empty and `subject` is a confidently resolved
        person (never for an unresolved identity or "primary" — see the
        identity-resolution block in _handle_message_impl)."""
        from . import memory_thread
        enabled, hours, limit = memory_thread.config()
        now = time.time()
        last_seen = self._last_seen.get(cid)
        self._last_seen[cid] = now
        if not enabled or not memory_thread.should_reseed(last_seen, now, hours):
            return
        seeded = await memory_thread.load_recent(
            self.hass, hours, limit, device_id=cid, subject=subject)
        if seeded:
            # Wrapped as one 'system' note, not raw turns — see
            # memory_thread.format_seed_message for why: raw turns read as
            # live to the model, which made Nova fixate on stale, completed
            # exchanges (e.g. re-litigating a light that was turned off days
            # ago) instead of treating them as background.
            history[:] = [memory_thread.format_seed_message(seeded)]

    # ── HA LLM tool integration ───────────────────────────────────────────────

    async def _get_hass_api(self, user_input: conversation.ConversationInput):
        try:
            ctx = llm.LLMContext(**_ha_kwargs(
                llm.LLMContext,
                platform=DOMAIN,
                context=user_input.context,
                user_prompt=user_input.text,
                language=user_input.language,
                assistant=conversation.HOME_ASSISTANT_AGENT,
                device_id=user_input.device_id,
            ))
            api = await llm.async_get_api(self.hass, "assist", ctx)
            _LOGGER.debug("Nova: %d HA tools available", len(api.tools))
            return api
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Nova: HA Assist API unavailable (%s) — chat-only mode", exc)
            return None

    # ── Main entry point ──────────────────────────────────────────────────────

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log=None,
    ) -> conversation.ConversationResult:
        # HA's ConversationEntity.async_process is @final and sets up the chat
        # session/log before calling this method — so Nova must implement only
        # _async_handle_message and must NOT override async_process (a prior shim
        # did, which bypassed that setup on current HA). Wrap the body so an
        # unexpected error becomes a spoken + logged message with a real trace,
        # instead of HA's opaque "Unexpected error during intent recognition"
        # that leaves nothing in Nova's own diagnostics.
        try:
            return await self._handle_message_impl(user_input, chat_log)
        except Exception:
            import traceback as _tb
            last = _tb.format_exc().strip().splitlines()[-1][:200]
            try:
                from .websocket import nova_log
                nova_log("ERROR", f"conversation handler crashed: {last}")
            except Exception:
                pass
            _LOGGER.exception("Nova conversation handler crashed")
            ir = intent.IntentResponse(language=user_input.language)
            ir.async_set_speech(
                "I ran into an internal error handling that request.")
            return conversation.ConversationResult(
                response=ir,
                conversation_id=getattr(user_input, "conversation_id", None)
                or "default",
            )

    async def _handle_message_impl(
        self,
        user_input: conversation.ConversationInput,
        chat_log=None,
    ) -> conversation.ConversationResult:
        from .websocket import nova_log
        device_id = getattr(user_input, 'device_id', None)
        nova_log("CONV", f"ENTRY text='{user_input.text[:60]}' device={device_id}")
        _LOGGER.warning(
            "Nova async_process ENTRY: text='%s' device_id='%s'",
            user_input.text[:60], device_id,
        )
        # A loaded entry that lost its NovaRuntime must fail this turn before
        # the local engine, the provider, any tool or TTS routing runs. The
        # wrapper in _async_handle_message logs it and speaks its error reply.
        get_runtime(self.entry)

        # Transcribed voice text arriving here means STT just worked (HA's
        # pipeline transcribed speech and routed it to us). Record it as a real
        # STT success so the health panel reflects reality, not a synthetic poke
        # (v6.70.3). Only voice-originated turns count — device_id present.
        if device_id and (user_input.text or "").strip():
            try:
                from .diagnostics.service_health import record_usage
                record_usage("stt", True)
            except Exception:
                pass

        # ── v5.7.01: Multi-wake dedup ────────────────────────────────────
        # If another satellite already processed this exact text within
        # the dedup window, return the cached response WITHOUT routing
        # audio again. This prevents 3 speakers all talking at once.
        is_dup, cached = _check_and_claim_dedup(user_input.text, device_id)
        if is_dup:
            _LOGGER.warning(
                "Nova dedup: suppressing duplicate from device=%s "
                "(already handled), text='%s'",
                device_id, user_input.text[:40],
            )
            nova_log("DEDUP", f"suppressed duplicate from {device_id}")
            ir = intent.IntentResponse(language=user_input.language)
            # Return empty speech so HA pipeline completes silently
            ir.async_set_speech(cached or "")
            return conversation.ConversationResult(
                response=ir,
                conversation_id=user_input.conversation_id or device_id or "default",
            )

        # ── v5.7.01: Presence gate — only the occupied room responds ─────
        # Uses mmWave / occupancy sensors to determine if anyone is in the
        # satellite's area. If the area is NOT occupied, suppress this
        # pipeline so only the satellite in the user's actual room answers.
        # v6.36.0: OFF by default. It silences the satellite you're speaking
        # to whenever that room's presence sensor hasn't registered you yet (or
        # is flaky), which reads as "voice stopped working". The multi-wake
        # dedup above already stops several satellites answering at once, so the
        # gate is opt-in for homes with reliable per-room presence.
        if device_id and self._opt("presence_gate", False):
            try:
                from .audio_routing import entity_area, is_area_occupied
                from homeassistant.helpers import (
                    device_registry as _dr,
                    entity_registry as _er,
                )
                dev_reg = _dr.async_get(self.hass)
                dev = dev_reg.async_get(device_id)
                sat_area = dev.area_id if dev else None

                if sat_area and not is_area_occupied(self.hass, sat_area):
                    # Double check: are ANY areas occupied? If no sensors
                    # report occupancy anywhere, skip the gate entirely
                    # (sensors might be offline / not configured).
                    from .audio_routing import currently_occupied_areas
                    occupied = currently_occupied_areas(self.hass)
                    if occupied:
                        _LOGGER.warning(
                            "Nova presence gate: suppressing pipeline from "
                            "device=%s area='%s' (not occupied, occupied=%s)",
                            device_id, sat_area, occupied,
                        )
                        nova_log(
                            "GATE",
                            f"suppressed {sat_area} (not occupied, "
                            f"occupied={occupied})",
                        )
                        ir = intent.IntentResponse(language=user_input.language)
                        ir.async_set_speech("")
                        return conversation.ConversationResult(
                            response=ir,
                            conversation_id=(
                                user_input.conversation_id
                                or device_id or "default"
                            ),
                        )
                    else:
                        _LOGGER.debug(
                            "Nova presence gate: no occupied areas "
                            "detected anywhere — skipping gate (sensors "
                            "may be offline)"
                        )
            except Exception as exc:
                _LOGGER.debug("Presence gate check failed (non-fatal): %s", exc)

        cid       = user_input.conversation_id or user_input.device_id or "default"
        honorific = self._honorific()
        # Warm persona cache (reads file in executor) before sync _persona()
        await _ensure_persona_loaded(self.hass)
        persona   = self._persona()

        # Reject irrelevant speech before it can mutate conversation memory
        # (history, DB, semantic recall, knowledge injection). Pending-offer
        # state is captured once here and reused by the offer handler below.
        try:
            from . import cognitive_core
            pending_offer = cognitive_core.get_pending_offer()
        except Exception:
            cognitive_core = None
            pending_offer = None
        gate_enabled = self._opt("relevance_gate", True)
        is_addressed = _is_addressed_to_nova(user_input.text)
        relevant = bool(pending_offer) or not gate_enabled or is_addressed

        if not relevant:
            nova_log("GATE", f"ignored ambient input: '{user_input.text.strip()[:60]}'")
            _LOGGER.info("Nova relevance gate: ignored '%s'", user_input.text.strip()[:80])
            ir = intent.IntentResponse(language=user_input.language)
            ir.async_set_speech("")  # silence — do not respond to ambient speech
            return conversation.ConversationResult(response=ir, conversation_id=cid)

        # Phase 2: resolve identity exactly once for this turn, fail-open.
        # `episodic_subject` is the safe-person predicate: a confidently
        # resolved named person gets a real subject string (person-scoped
        # episodic fallback allowed); an unresolved identity gets None
        # (never the shared "primary" bucket — that string is only ever
        # produced by identity.subject_for()'s fallback branch, which is
        # never called here). Reused below by transcript seeding, semantic
        # storage/retrieval, knowledge injection, and command-log
        # attribution — identity.resolve() is never called again this turn.
        identity_module = None
        ident = None
        episodic_subject = None
        try:
            from . import identity as identity_module
            ident = identity_module.resolve(
                self.hass,
                device_id=getattr(user_input, "device_id", None),
            )
            if ident.known:
                episodic_subject = identity_module.normalize(ident.person)
        except Exception as exc:
            _LOGGER.debug("Identity resolve: %s", exc)
            # ident/episodic_subject stay None — conversation-id-scoped
            # storage/retrieval below continue unaffected; person-scoped
            # fallback simply doesn't fire (same as a genuinely unresolved
            # identity); knowledge injection and command logging degrade to
            # their existing no-identity behavior, defined at their own
            # call sites below.

        history   = self._history(cid)
        await self._maybe_seed_history(cid, history, subject=episodic_subject)

        history.append({"role": "user", "content": user_input.text})
        # Phase 2: capture the inserted row id as the stable exchange link —
        # save_message() now returns lastrowid (or None on its existing
        # fail-open write failure). turn_id is only ever created from a real
        # id, never the string "None".
        user_row_id = await self.hass.async_add_executor_job(
            save_message, "user", user_input.text, cid, episodic_subject)
        turn_id = str(user_row_id) if user_row_id is not None else None

        # v5.6.1 / Phase 2: retrieve previous semantic context BEFORE storing
        # this turn's own message — storing first let the current message
        # become its own top search result. Retrieval and storage are
        # separate fail-open blocks: a retrieval failure must not prevent
        # this message from being stored for future recall, and a storage
        # failure must not discard context already retrieved and already
        # folded into `persona` below.
        mem_context = ""
        try:
            from .memory import get_conversation_context
            mem_context = await self.hass.async_add_executor_job(
                get_conversation_context, user_input.text, 3, cid, episodic_subject,
            )
        except Exception as exc:
            _LOGGER.debug("Memory retrieve: %s", exc)
        if mem_context:
            persona = persona + "\n\n" + mem_context

        try:
            from .memory import store_memory
            await self.hass.async_add_executor_job(
                lambda: store_memory(user_input.text, role="user",
                    device_id=user_input.device_id or "", conversation_id=cid,
                    subject=episodic_subject, turn_id=turn_id)
            )
        except Exception as exc:
            _LOGGER.debug("Memory store: %s", exc)

        # v6.25.0: Inject curated knowledge — durable facts/preferences Nova
        # knows (distinct from the transcript recall above), scored against the
        # current message so the most relevant facts lead.
        # v6.29.0: scope to *this* person + household so one resident's private
        # facts don't leak into another's context.
        # Phase 2: reuses `ident` captured above — no second
        # identity.resolve() call. When ident is None (import/resolve failed
        # earlier), this reproduces the pre-Phase-2 failure behavior exactly:
        # knowledge injection (person AND household) is skipped entirely for
        # the turn, not partially degraded to household-only.
        kn_block = None
        if ident is not None:
            try:
                from . import knowledge
                subjects = [identity_module.subject_for(ident), "household"]
                kn_block = await self.hass.async_add_executor_job(
                    lambda: knowledge.prompt_block(user_input.text, subjects=subjects))
            except Exception as exc:
                _LOGGER.debug("Knowledge inject: %s", exc)
        if kn_block:
            persona = persona + "\n\n" + kn_block

        hass_api = await self._get_hass_api(user_input) if self._use_hass_api() else None

        cast_routed = False  # tracks whether Cast speaker is handling TTS
        reopen_speaker: str | None = None   # separate speaker the reply played on
        reopen_device: str | None = None    # the wake/voice satellite's device_id

        # v5.9.07: Proactive offer yes/no — resolved before main pipeline.
        # honorific may be "" once nobody specific is home to address (see
        # honorific.py) — addr collapses the trailing ", {honorific}" to
        # nothing rather than leaving a dangling comma.
        addr = f", {honorific}" if honorific else ""
        offer_reply = None
        try:
            # Reuses the SAME pending_offer object captured before the
            # relevance decision above — no second get_pending_offer() read.
            if pending_offer:
                low = user_input.text.strip().lower()
                affirm = low in ("yes", "yes please", "yeah", "yep", "sure",
                                 "do it", "go ahead", "please do", "okay", "ok",
                                 "affirmative", "please")
                deny = low in ("no", "no thanks", "nope", "don't", "do not",
                               "negative", "leave it", "nevermind", "never mind",
                               "cancel", "stop")
                if affirm:
                    res = await cognitive_core.accept_pending_offer()
                    if res.get("now_autonomous"):
                        offer_reply = (
                            f"Done{addr}. I've noticed you consistently "
                            f"want this — I'll handle it automatically from now on. "
                            f"Say 'stop doing that on your own' to revoke."
                        )
                    elif res.get("ok"):
                        left = max(0, 3 - res.get("approvals", 0))
                        offer_reply = f"Done{addr}."
                        if 0 < left <= 2:
                            offer_reply += (
                                " (A couple more times and I'll handle this "
                                "automatically.)"
                            )
                    else:
                        offer_reply = f"I wasn't able to complete that{addr}."
                    nova_log("OFFER", f"accepted: {offer_reply[:60]}")
                elif deny:
                    cognitive_core.decline_pending_offer()
                    offer_reply = f"Understood{addr}. I'll leave it."
                    nova_log("OFFER", "declined")
                else:
                    cognitive_core.decline_pending_offer()
        except Exception as exc:
            _LOGGER.debug("Offer handling skipped: %s", exc)

        if offer_reply is not None:
            # Short-circuit: deliver the offer response directly.
            offer_reply = offer_reply.replace("{honorific}", honorific)
            ir = intent.IntentResponse(language=user_input.language)
            ir.async_set_speech(offer_reply)
            return conversation.ConversationResult(response=ir, conversation_id=cid)

        # v5.9.16: Relevance gate. Satellites without strict wake-word gating
        # pick up TV/background speech, which the pipeline transcribes and sends
        # here as if it were a command. Drop input that clearly isn't addressed
        # to Nova (filler, fragments, rambling dialogue) BEFORE it reaches the
        # local engine or the agent — staying silent rather than acting on, or
        # chattering back at, ambient noise. Toggle off via `relevance_gate`.
        # v7.105.0: reachable only when a pending offer made the turn relevant
        # for persistence above but this reply didn't parse as accept/decline
        # and isn't addressed to Nova either — still don't route it. Reuses
        # the values computed above so this and the persistence gate above
        # can never disagree.
        if gate_enabled and not is_addressed:
            nova_log("GATE", f"ignored ambient input: '{user_input.text.strip()[:60]}'")
            _LOGGER.info("Nova relevance gate: ignored '%s'", user_input.text.strip()[:80])
            ir = intent.IntentResponse(language=user_input.language)
            ir.async_set_speech("")  # silence — do not respond to ambient speech
            return conversation.ConversationResult(response=ir, conversation_id=cid)

        try:
            # v5.7.00: Local engine is PRIMARY. Complexity scoring decides
            # whether to escalate to LLM. Handles 95%+ of requests at zero
            # API cost. Only genuinely complex/creative/analytical requests
            # fall through to the LLM agent (Groq/Gemini).
            from .local_engine import try_local, score_complexity
            local_result = await try_local(
                self.hass, user_input.text, honorific, device_id=device_id)

            if local_result and local_result.handled:
                response_text = local_result.text
                _LOGGER.info("Nova local: %s", response_text[:100])
                nova_log("LOCAL", f"handled: {response_text[:80]}")
            else:
                complexity = score_complexity(user_input.text)
                # v5.9.06: Connectivity-aware escalation. If the circuit breaker
                # is OPEN (LLM known-unreachable), skip the doomed network call
                # entirely and attempt an offline salvage pass instead.
                from . import connectivity
                if not connectivity.allow_request():
                    nova_log("OFFLINE", f"LLM down — local salvage: {user_input.text[:60]}")
                    salvage = await try_local(
                        self.hass, user_input.text, honorific, force=True,
                        device_id=device_id,
                    )
                    if salvage and salvage.handled:
                        response_text = salvage.text
                        _LOGGER.info("Nova offline-salvage: %s", response_text[:100])
                    else:
                        response_text = (
                            f"I'm offline at the moment{addr}, so I can't "
                            f"handle that request — it needs my reasoning systems. "
                            f"I can still control your devices, report status, and "
                            f"run scenes. I'll be back to full capability once "
                            f"connectivity returns."
                        )
                else:
                    nova_log("AGENT", f"LLM needed (complexity={complexity}): {user_input.text[:60]}")
                    # Complex request — use LLM agent (Groq/Gemini fallback)
                    from .agent import run_agent
                    from . import nova_config as _jc
                    provider_name = self._rt_opt("llm_provider", "groq")
                    model_val = self._rt_opt(CONF_MODEL, DEFAULT_MODEL)

                    # The reasoning-tier fallback needs the FULL config, not
                    # entry.data|options — those are empty on panel-configured
                    # installs, which left the fallback unable to resolve a
                    # provider and forced the "offline" message even when a
                    # working tier was configured. Use the single source of
                    # truth (nova_config) plus the live panel runtime_config
                    # (non-empty values win), read from NovaRuntime this turn.
                    eff_config = await self.hass.async_add_executor_job(
                        _jc.effective_config_with_runtime, self.entry,
                        self._runtime_config(),
                    )

                    api_key_val = resolve_provider_credential(
                        eff_config, provider_name)
                    base_url_val = resolve_provider_endpoint(
                        eff_config, provider_name)

                    try:
                        response_text = await run_agent(
                            self.hass,
                            messages=history,
                            persona=persona,
                            provider_name=provider_name,
                            api_key=api_key_val,
                            model=model_val,
                            base_url=base_url_val,
                            hass_api=hass_api,
                            user_input=user_input,
                            temperature=0.7,
                            config=eff_config,
                        )
                        # Agent returns a connectivity sentinel string on total
                        # failure; treat that as a breaker failure + salvage.
                        if _is_connectivity_failure(response_text):
                            connectivity.record_failure()
                            salvage = await try_local(
                                self.hass, user_input.text, honorific, force=True,
                                device_id=device_id,
                            )
                            if salvage and salvage.handled:
                                response_text = salvage.text
                        else:
                            connectivity.record_success()
                    except Exception as agent_exc:  # pylint: disable=broad-except
                        _LOGGER.warning("Agent call raised: %s", agent_exc)
                        connectivity.record_failure()
                        salvage = await try_local(
                            self.hass, user_input.text, honorific, force=True,
                            device_id=device_id,
                        )
                        if salvage and salvage.handled:
                            response_text = salvage.text
                        else:
                            response_text = (
                                f"I've lost connection to my reasoning systems"
                                f"{addr}. I can still control devices and "
                                f"report status while I reconnect."
                            )
            from .directive_helper import fill_honorific
            response_text = fill_honorific(response_text, honorific)

            # v5.7.01: Record the winning response so duplicate pipelines
            # arriving slightly later will get caught by dedup
            _record_dedup_response(user_input.text, response_text)

            # v5.8.03: Log command for cognitive core pattern learning
            # v6.29.0: attribute to the resolved person so per-person command
            # patterns are real (falls back to "unknown" when not confident).
            try:
                from . import cognitive_core, voice_recognition
                handler = "local" if local_result and local_result.handled else "agent"
                dev = getattr(user_input, "device_id", None)
                # Phase 2: reuse the identity captured once at the top of the
                # turn — no second identity.resolve() call. Falls back to
                # the same "unknown" value a fresh unresolved identity
                # (or a failed import/resolve) would have produced.
                if ident is not None:
                    who = ident.person
                elif identity_module is not None:
                    who = identity_module.UNKNOWN
                else:
                    who = "unknown"
                cognitive_core.log_command(
                    text=user_input.text,
                    handled_by=handler,
                    person=who,
                )
                # Voice learns over time: if we know who this is from other
                # signals but the voice service doesn't yet, flag an enrollment
                # opportunity so the pending sample can be labelled automatically.
                voice_recognition.maybe_fire_enrollment(self.hass, dev)
            except Exception:
                pass

            # v5.7.03: Route reply to paired Cast speaker. The satellite
            # handles wake/STT, Nova routes TTS output to the real
            # speaker in the room (Google Home, Nest Audio, etc.) for
            # better audio quality. Satellite pairings from the panel
            # Settings determine which speaker each satellite uses.
            try:
                device_id_route = getattr(user_input, 'device_id', None)
                if device_id_route:
                    from .audio_routing import reply_target
                    sat_pairings = self._satellite_pairings()

                    speaker = reply_target(
                        self.hass,
                        device_id=device_id_route,
                        satellite_pairings=sat_pairings,
                    )
                    # A mic-only satellite (e.g. Waveshare with the DAC off), or
                    # one whose room has no real speaker, resolves to itself and
                    # can't play the reply. Fall back to the configured
                    # reply/broadcast speakers — the same ones proactive audio
                    # (the briefing) already uses successfully — so the reply is
                    # heard instead of lost.
                    if not speaker or speaker.startswith("assist_satellite."):
                        for cand in self._speakers(device_id_route):
                            if cand.startswith("assist_satellite."):
                                continue
                            st = self.hass.states.get(cand)
                            if st is not None and st.state not in ("unavailable", "unknown"):
                                _LOGGER.info(
                                    "Nova reply: satellite can't speak, using "
                                    "configured reply speaker %s", cand)
                                speaker = cand
                                break
                    if speaker and not speaker.startswith("assist_satellite."):
                        tts_ent = resolve_tts_entity(
                            self.hass, self._opt("tts_engine", "auto"),
                        )
                        sp = self.hass.states.get(speaker)
                        sp_reachable = sp is not None and sp.state not in (
                            "unavailable", "unknown")
                        if tts_ent and sp_reachable:
                            _LOGGER.info(
                                "Nova reply → Cast: tts=%s speaker=%s",
                                tts_ent, speaker,
                            )
                            # Fire-and-forget to the paired speaker, then silence
                            # the satellite. This is the delivery that worked.
                            # v7.50–v7.51 changed this to await + poll the speaker
                            # for a 'playing' state + fall back to broadcast; that
                            # misfired on idle Cast speakers that play fine but don't
                            # report 'playing' during a short announce, breaking the
                            # working case. Reverted to the original behavior.
                            #
                            # Spoken History (v7.104.0): when this reply is itself
                            # the deterministic "repeat that" command's answer
                            # (local_result.repeat_of_id set — see local_engine.py),
                            # tag it "repeat" with a reference to the original
                            # instead of "reply", so it's recorded as a repeat.
                            # This is the ONLY place a voice-triggered repeat is
                            # ever recorded — only here does Nova get a confirmed
                            # delivery result; an ordinary reply that instead plays
                            # through the pipeline's own TTS on the satellite
                            # (the non-Cast-routed branch below) is never recorded,
                            # because Nova cannot observe whether that TTS ran.
                            repeat_of_id = (
                                local_result.repeat_of_id
                                if local_result and getattr(local_result, "repeat_of_id", None)
                                else None
                            )
                            self.hass.async_create_task(
                                async_announce(
                                    self.hass, response_text,
                                    tts_ent, [speaker],
                                    context="repeat" if repeat_of_id else "reply",
                                    repeat_of_id=repeat_of_id,
                                )
                            )
                            cast_routed = True
                            reopen_speaker = speaker
                            reopen_device = device_id_route
                            try:
                                nova_log("REPLY", f"→ Cast {speaker} via {tts_ent}")
                            except Exception:
                                pass
                        elif not sp_reachable:
                            # The reply speaker is offline — do NOT silence the
                            # satellite, or the reply is lost entirely. Let the
                            # satellite speak instead.
                            _LOGGER.warning(
                                "Nova reply: Cast speaker %s is unavailable — "
                                "the satellite will speak the reply instead", speaker)
                            try:
                                nova_log("REPLY", f"speaker {speaker} unavailable "
                                           "— satellite will speak")
                            except Exception:
                                pass
                        else:
                            _LOGGER.warning("Nova reply: no TTS entity found")
                            try:
                                nova_log("REPLY", "no TTS entity — satellite will speak")
                            except Exception:
                                pass
                    else:
                        _LOGGER.info(
                            "Nova reply: no Cast pairing for device=%s, "
                            "pipeline speaker fallback",
                            device_id_route,
                        )
                        try:
                            nova_log("REPLY", f"no Cast pairing for device="
                                       f"{device_id_route} — satellite plays "
                                       f"(resolved={speaker})")
                        except Exception:
                            pass
            except Exception as exc:
                _LOGGER.warning("Reply routing error: %s", exc)

            # Now safe to do blocking DB operations
            history.append({"role": "assistant", "content": response_text})
            try:
                # Phase 2: same episodic_subject captured at the top of the
                # turn, so subject-scoped transcript fallback (memory_thread)
                # can find both halves of a past exchange, not just the
                # user's side. This row keeps its own independent primary
                # key — it is never itself a turn_id, only ever tagged with
                # the triggering user row's turn_id in semantic memory below.
                await self.hass.async_add_executor_job(
                    save_message, "assistant", response_text, cid, episodic_subject,
                )
            except Exception:
                pass
            _LOGGER.debug("Nova → %s", response_text[:120])

            # Store assistant response in long-term memory. Phase 2: threads
            # the SAME turn_id/episodic_subject captured for this turn's user
            # message, so the two halves pair on retrieval. If this turn
            # never reached this point (offer short-circuit, an exception
            # above), this block simply never runs — the user's row/semantic
            # record stay a legitimately unpaired turn, and a later,
            # unrelated assistant reply carries its OWN fresh turn_id, so it
            # can never accidentally pair with this one.
            try:
                from .memory import store_memory
                await self.hass.async_add_executor_job(
                    lambda: store_memory(response_text, role="assistant",
                        device_id=user_input.device_id or "", conversation_id=cid,
                        subject=episodic_subject, turn_id=turn_id)
                )
            except Exception:
                pass

        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.error(
                "Nova API error (%s): %s | model=%s",
                type(exc).__name__, exc, self._model(),
            )
            from .directive_helper import fill_honorific
            response_text = fill_honorific(
                _FALLBACKS[self._fallback_idx % len(_FALLBACKS)], honorific
            )
            self._fallback_idx += 1
            if history and history[-1]["role"] == "user":
                history.pop()

        ir = intent.IntentResponse(language=user_input.language)
        # When Cast routing succeeded, suppress the pipeline's TTS to the
        # satellite speaker — only the Cast device should talk. If Cast
        # routing failed or wasn't attempted, let the pipeline play through
        # the satellite as fallback.
        if cast_routed:
            ir.async_set_speech("")  # silence satellite — Cast has it
        else:
            ir.async_set_speech(response_text)  # fallback: satellite speaks
        result = conversation.ConversationResult(response=ir, conversation_id=cid)
        # v6.88.0: keep listening for a follow-up when Nova invited one, so
        # natural turn-taking works without a new wake word. Set defensively —
        # older HA cores may not carry the field.
        try:
            from . import continued_conversation as _cc
            if _cc.enabled() and _cc.should_continue(response_text):
                sat_ent = (_cc.satellite_for_device(self.hass, reopen_device)
                           if reopen_device else None)
                if (cast_routed and reopen_speaker and sat_ent
                        and _cc.speaker_reopen_enabled()):
                    # The reply is playing on a SEPARATE speaker, not the mic-only
                    # satellite. HA's continue_conversation would reopen the mic
                    # based on the satellite's own (instant) playback — before the
                    # speaker finishes — so the mic would capture Nova's own
                    # reply. Instead Nova watches the speaker and reopens the mic
                    # only once it goes idle.
                    _cc.schedule_reopen(
                        self.hass, sat_ent, reopen_speaker, response_text)
                else:
                    # Satellite plays the reply itself (or no separate speaker) —
                    # HA's built-in reopen timing is correct here.
                    result.continue_conversation = True
        except Exception:
            pass
        return result


def _ha_kwargs(cls, **kwargs):
    """Keep only the kwargs that cls's constructor accepts. Home Assistant's LLM
    API changed fields across versions — 2026.8 moved the context out of
    ToolInput into LLMContext and dropped user_prompt — so we pass the
    intersection and stay compatible with old and new HA (v7.21.1). Never raises."""
    try:
        import inspect
        allowed = inspect.signature(cls).parameters
        return {k: v for k, v in kwargs.items() if k in allowed}
    except Exception:
        return kwargs
