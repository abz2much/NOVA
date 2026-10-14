"""Nova's small set of local voice commands.

A phrase such as "lights off" or "secure the garage" is matched against a
fixed table here, with no model involved, and carried out in the room the
request came from. "It" and "that" are taken to mean the active thing in that
room. After an announcement that offers to do something, a short window lets
a plain "yes" carry it out without the wake word.

Everything at module level is standard library only, so match_intent and
is_affirmative can be loaded and tested without Home Assistant. Home
Assistant, policy, the logs and the area lookup are imported where used.
"""
from __future__ import annotations

import asyncio
import logging
import re

_LOGGER = logging.getLogger(__name__)

# The event a voice satellite listens for to open a short capture without the
# wake word, and how long that window stays open.
EVENT_FEEDBACK_WINDOW = "nova_feedback_window"
FEEDBACK_TIMEOUT_S = 10.0


class _Rows(dict):
    """Action log row ids by entity for one voice reply. request_id names the
    action log request the rows belong to (8.25.0)."""
    request_id: str | None = None


def _rx(*patterns: str) -> tuple[str, ...]:
    return patterns


# First match wins, so the specific commands come before the pronoun ones:
# "turn off the lights" is a lights command, not "turn it off". Arming is
# listed only so it can be refused plainly (8.7.20); it once fell into
# secure_area, which locked and closed things and armed nothing.
_INTENT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("arm_alarm", _rx(r"\barm\b.*\b(garage|house|home|alarm)\b")),
    ("secure_area", _rx(r"\bsecure\b", r"\bclose\b.*\bgarage\b",
                        r"\block\s+(up|down|the|it)\b")),
    ("lights_off", _rx(r"\b(turn|switch|shut)\s+off\b.*\blight", r"\blights?\s+off\b",
                       r"\bkill\s+the\s+lights?\b")),
    ("lights_on", _rx(r"\b(turn|switch)\s+on\b.*\blight", r"\blights?\s+on\b")),
    ("context_off", _rx(r"\bturn\s+it\s+off\b", r"\bswitch\s+it\s+off\b",
                        r"\bshut\s+it\s+off\b", r"\bturn\s+that\s+off\b",
                        r"\bpause\s+it\b")),
    ("context_close", _rx(r"\bclose\s+it\b", r"\bshut\s+it\b")),
)

# Besides a plain yes (answers.py holds the one list every yes or no question
# reads, 8.49.0), the window accepts its own action words, since the
# announcement offered to close something.
_ACTION_YES = tuple(prefix + verb + " it" for prefix in ("", "yes ")
                    for verb in ("close", "shut", "secure"))

# Any of these words means no command and no yes (8.7.20). "no, don't do it"
# once confirmed and "don't secure the garage" once secured it. Doing nothing
# is the safe way to fail.
_NEGATION = re.compile(r"\b(no|not|never|cancel|stop|don[’']?t|do\s+not)\b")

# A question never acts (8.7.20): "is the garage secure?" once closed the
# garage. It ends in "?" or starts with a question word.
_QUESTION_WORDS = ("is are was were am do does did has have had what what's whats "
                   "why how when where which who whose")
_QUESTION_START = re.compile(r"^(" + "|".join(_QUESTION_WORDS.split()) + r")\b")

# The state that makes something "it", by domain. Any other domain counts
# when on or open.
_ACTIVE_STATES = {"media_player": {"playing"}, "light": {"on"}}
_ACTIVE_OTHERWISE = {"on", "open"}

# What a held pronoun target is told, by the live check's reason.
_HELD_REASONS = {"already_secure": "it is already closed",
                 "device_unreadable": "it can't be read right now"}
_HELD_DEFAULT = "needs a confirmation the voice reply cannot ask for"


def _is_command_shaped(text: str) -> bool:
    return not (_NEGATION.search(text) or text.endswith("?")
                or _QUESTION_START.match(text))


def match_intent(phrase: str) -> dict | None:
    """{"intent": name, "raw": phrase} for the first command the phrase
    matches, or None. Questions and negated phrases never match."""
    if not phrase:
        return None
    text = phrase.lower().strip()
    if not _is_command_shaped(text):
        return None
    for name, patterns in _INTENT_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, text):
                return {"intent": name, "raw": phrase}
    return None


def is_affirmative(phrase: str) -> bool:
    """Whether the phrase is a plain yes, or one of the window's action words.
    Anything negated is not."""
    if not phrase:
        return False
    text = phrase.lower().strip()
    if _NEGATION.search(text):
        return False
    from ..answers import yes_or_no
    return yes_or_no(text, extra_yes=_ACTION_YES) == "yes"


def _held_row(entity_id: str, domain: str, service: str, why: dict) -> dict:
    """The action log row for a device the voice reply did not act on. Only
    a device held for confirmation (no reason, or an open door) needed an
    approval that was never asked for."""
    code = why.get(entity_id)
    needs_approval = code is None or code == "door_open"
    return {"key": entity_id, "domain": domain, "service": service,
            "entity_id": entity_id,
            "approval_required": needs_approval,
            "approval_result": "not_requested" if needs_approval else "not_required",
            "execution_result": "blocked",
            "reason_code": code or "confirmation_unavailable_in_voice_reply"}


class LocalIntentRouter:
    """Carries out matched commands in a room, works out what "it" means
    there, and runs the voice confirmation window.

    ledger (a StateLedger) and mutex (an EntityLockRegistry) are optional and
    duck typed: the ledger records a high stakes action before it is sent, and
    the mutex keeps the router off devices a higher priority job holds."""

    def __init__(self, hass, *, ledger=None, mutex=None) -> None:
        self.hass = hass
        self.ledger = ledger
        self.mutex = mutex
        self._pending_feedback: dict | None = None
        self._feedback_cancel = None
        # How long to wait before rereading what a voice reply locked or
        # closed (8.24.0); lockdown waits the same.
        self.verify_delay = 25.0

    def _area_of(self, entity_id: str):
        from .. import audio_routing
        return audio_routing.entity_area(self.hass, entity_id)

    def _in_area(self, domain: str, area_id: str, area_of) -> list:
        """States of this domain whose area is area_id. An entity whose area
        cannot be looked up is left out."""
        found = []
        for state in self.hass.states.async_all(domain):
            try:
                here = area_of(state.entity_id) == area_id
            except Exception:  # noqa: BLE001
                here = False
            if here:
                found.append(state)
        return found

    def resolve_active_entity(
        self, area_id: str,
        domains: tuple[str, ...] = ("media_player", "light"),
        *, area_of=None,
    ) -> tuple[str | None, str | None]:
        """(entity_id, domain) of what "it" most likely means in the area:
        domains are tried in order, a media player counts while playing, a
        light while on, anything else while on or open. (None, None) when
        nothing is active."""
        for domain in domains:
            active = _ACTIVE_STATES.get(domain, _ACTIVE_OTHERWISE)
            for state in self._in_area(domain, area_id, area_of or self._area_of):
                if str(state.state).lower() in active:
                    return state.entity_id, domain
        return None, None

    # ── authority and the action log (8.24.0) ──────────────────────────────

    def _authorized(self, domain: str, service: str, entity_ids: list[str],
                    user_id: str | None = None) -> tuple[list[str], list[str], dict]:
        """(allowed, held, why) from policy.authorize_now, asked as a voice
        request. Locking, closing and lights pass; anything needing a
        confirmation is held and never sent. why holds the live check's
        reason for a held device when it gives one (8.46.0), such as already
        secure, unreadable, or its door open. Any failure holds."""
        try:
            from .. import policy
        except Exception:  # noqa: BLE001
            return [], list(entity_ids), {}
        allowed: list[str] = []
        held: list[str] = []
        why: dict = {}
        for entity_id in entity_ids:
            try:
                verdict = policy.authorize_now(self.hass, policy.AuthorityRequest(
                    domain, service, entity_id, source=policy.SOURCE_VOICE,
                    user_id=user_id or ""))
            except Exception:  # noqa: BLE001
                held.append(entity_id)
                continue
            if verdict.allowed:
                allowed.append(entity_id)
                continue
            held.append(entity_id)
            if verdict.code:
                why[entity_id] = verdict.code
        return allowed, held, why

    async def _log_start(self, intent: str, domain: str, service: str,
                         allowed: list[str], held: list[str],
                         user_id: str | None = None, why: dict | None = None) -> dict:
        """Open one action log request for this voice reply, with a row per
        device. Held devices start blocked, with the live check's reason
        when there is one (8.46.0). Returns the row ids by entity, or an
        empty _Rows when there is nothing to log or the log fails."""
        why = why or {}
        rows = [{"key": e, "domain": domain, "service": service, "entity_id": e}
                for e in allowed]
        rows.extend(_held_row(e, domain, service, why) for e in held)
        if not rows:
            return _Rows()
        try:
            from .. import action_log
            request_id = action_log.new_request_id()

            def start():
                return action_log.start_many(
                    request_id, f"voice_reply:{intent}", "voice", rows,
                    requested_by_user_id=user_id or None)
            row_ids = _Rows(await self.hass.async_add_executor_job(start) or {})
        except Exception:  # noqa: BLE001
            return _Rows()
        row_ids.request_id = request_id
        return row_ids

    async def _log_set(self, row_ids: dict, entity_ids: list[str], result: str,
                       reason_code: str | None = None) -> None:
        """Record one execution result on each listed device's row. Never
        raises."""
        try:
            from .. import action_log
            for entity_id in entity_ids:
                row = row_ids.get(entity_id)
                if row is None:
                    continue
                await self.hass.async_add_executor_job(
                    lambda r=row: action_log.set_execution(r, result,
                                                           reason_code=reason_code))
        except Exception:  # noqa: BLE001
            pass

    # ── checking what was secured (8.24.0) ─────────────────────────────────

    def _schedule_check(self, intent: str, domain: str, targets: list[str],
                        row_ids: dict, user_id: str | None) -> None:
        """Start the background check for locks and covers this reply acted
        on. Nothing else is checked. Never raises."""
        if not targets or domain not in ("lock", "cover"):
            return
        try:
            self.hass.async_create_task(
                self._check_secured(intent, domain, list(targets), row_ids, user_id))
        except Exception:  # noqa: BLE001
            pass

    def _friendly(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return state.attributes.get("friendly_name", entity_id) if state else entity_id

    async def _check_secured(self, intent: str, domain: str, targets: list[str],
                             row_ids: dict, user_id: str | None) -> None:
        """After verify_delay, reread each target: a lock must be locked and
        a cover closed. Rows become verified or unverified (unreadable counts
        as unverified), the outcome goes to the decision log, and anything
        left unsecured is raised as a high alert. Never raises."""
        try:
            await asyncio.sleep(self.verify_delay)
            from .. import alert_path, entity_verify
            expected = "locked" if domain == "lock" else "closed"
            secured = [e for e in targets
                       if entity_verify.check_state_once(self.hass, e, expected)]
            unsecured = [e for e in targets if e not in secured]
            await self._log_set(row_ids, secured, "verified")
            await self._log_set(row_ids, unsecured, "unverified", "not_secure_after_check")
            await alert_path.async_record_decision(
                self.hass, "voice_secure", source="voice_reply",
                sit=alert_path.situation(self.hass),
                facts={"intent": intent, "secured": secured,
                       "not_secured_after_check": unsecured},
                assessment="asked by voice to secure the room",
                decision=("checked, some not secured" if unsecured
                          else "checked, secured"),
                reason="the voice reply's check after acting",
                request_ids=[getattr(row_ids, "request_id", None)])
            if unsecured:
                await self._raise_unsecured(unsecured)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("intent: secure check failed", exc_info=True)

    async def _raise_unsecured(self, unsecured: list[str]) -> None:
        from .. import core_state
        from ..core_bridge import _emit_action
        names = ", ".join(self._friendly(e) for e in unsecured)
        alert = {"type": "voice_secure_failed", "urgency": "high",
                 "message": (f"I tried to secure {names}, but it is not secured yet. "
                             "Please check it."),
                 "entity_id": unsecured[0], "auto_act": True}
        await _emit_action(self.hass, core_state._CORE.config or {}, alert, False)

    # ── acting ──────────────────────────────────────────────────────────────

    def _take_locks(self, entity_ids: list[str], priority) -> tuple[list[str], dict]:
        """(entities this router may act on, their lock tokens). Without a
        mutex or a priority every entity is free and nothing is locked."""
        if self.mutex is None or priority is None:
            return list(entity_ids), {}
        tokens: dict = {}
        for entity_id in entity_ids:
            token = self.mutex.try_acquire(entity_id, priority)
            if token is None:
                _LOGGER.info("intent: %s busy (higher-priority lock) — skipping", entity_id)
            else:
                tokens[entity_id] = token
        return [e for e in entity_ids if e in tokens], tokens

    def _write_ahead(self, targets: list[str], desired_state: str, action: str) -> list:
        """Record each high stakes target in the ledger before the call, so
        an interrupted action can be recovered at boot."""
        txns = []
        for entity_id in targets:
            try:
                txns.append(self.ledger.record_intent(entity_id, desired_state,
                                                      action=action))
            except Exception:  # noqa: BLE001
                _LOGGER.exception("ledger record_intent failed for %s", entity_id)
        return txns

    def _complete(self, txns: list) -> None:
        for txn in txns:
            try:
                self.ledger.mark_complete(txn)
            except Exception:  # noqa: BLE001
                pass

    async def _call_domain_in_area(
        self, domain: str, service: str, area_id: str,
        *, high_stakes: bool = False, desired_state: str | None = None,
        priority: int | None = None, intent: str = "", user_id: str | None = None,
    ) -> list[str]:
        """Call domain.service on every entity of the domain in the area that
        policy allows and no higher priority job holds. High stakes calls are
        written ahead to the ledger. Returns what was acted on, plus devices
        policy found already secure (8.46.0), which are counted without a
        call. A failed call, or every allowed device busy, returns []."""
        label = intent or f"{domain}.{service}"
        in_area = [s.entity_id for s in self._in_area(domain, area_id, self._area_of)]
        if not in_area:
            return []
        allowed, held, why = self._authorized(domain, service, in_area, user_id)
        row_ids = await self._log_start(label, domain, service, allowed, held,
                                        user_id, why)
        already = [e for e in held if why.get(e) == "already_secure"]
        if not allowed:
            return already

        targets, tokens = self._take_locks(allowed, priority)
        if not targets:
            await self._log_set(row_ids, allowed, "blocked", "entity_busy")
            return []
        busy = [e for e in allowed if e not in targets]
        if busy:
            await self._log_set(row_ids, busy, "blocked", "entity_busy")

        txns = []
        if high_stakes and self.ledger is not None and desired_state is not None:
            txns = self._write_ahead(targets, desired_state, f"{domain}.{service}")
        try:
            await self.hass.services.async_call(domain, service, {"entity_id": targets},
                                                blocking=True)
        except Exception:  # noqa: BLE001
            # The ledger entries stay open so boot recovery sees them.
            _LOGGER.exception("intent: %s.%s failed for %s", domain, service, targets)
            await self._log_set(row_ids, targets, "failed", "service_call_failed")
            self._release_all(tokens)
            return []
        await self._log_set(row_ids, targets, "accepted")
        self._schedule_check(label, domain, targets, row_ids, user_id)
        self._complete(txns)
        self._release_all(tokens)
        return targets + already

    def _release_all(self, tokens: dict) -> None:
        """Give back every lock token taken. A failed release is ignored."""
        for token in (tokens.values() if self.mutex is not None else ()):
            try:
                self.mutex.release(token)
            except Exception:  # noqa: BLE001
                pass

    async def _act_on_pronoun(self, intent: str, area_id: str, priority,
                              user_id: str | None) -> dict:
        """"Turn it off" turns off the playing media or the light that is on;
        "close it" closes an open cover (8.7.20, it once turned off the media
        instead)."""
        domains = ("cover",) if intent == "context_close" else ("media_player", "light")
        entity_id, domain = self.resolve_active_entity(area_id, domains)
        if entity_id is None:
            return {"executed": False, "intent": intent,
                    "reason": "nothing active to act on in this area"}
        domain = str(domain)
        service = "close_cover" if domain == "cover" else "turn_off"
        allowed, held, why = self._authorized(domain, service, [entity_id], user_id)
        row_ids = await self._log_start(intent, domain, service, allowed, held, user_id, why)
        result = {"executed": False, "intent": intent, "entity_id": entity_id}
        if not allowed:
            return {**result, "reason": _HELD_REASONS.get(why.get(entity_id, ""),
                                                          _HELD_DEFAULT)}
        targets, tokens = self._take_locks([entity_id], priority)
        if not targets:
            await self._log_set(row_ids, [entity_id], "blocked", "entity_busy")
            return {**result, "reason": "entity busy (higher-priority lock)"}
        try:
            await self.hass.services.async_call(domain, service, {"entity_id": entity_id},
                                                blocking=True)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("intent: context action failed for %s", entity_id)
            await self._log_set(row_ids, [entity_id], "failed", "service_call_failed")
            self._release_all(tokens)
            return result
        await self._log_set(row_ids, [entity_id], "accepted")
        self._schedule_check(intent, domain, [entity_id], row_ids, user_id)
        self._release_all(tokens)
        return {**result, "executed": True}

    async def execute(self, decision: dict, area_id: str, *, user_id: str | None = None) -> dict:
        """Carry out a matched intent in the area and say what happened."""
        intent = decision["intent"]
        priority = None
        if self.mutex is not None:
            from ..automation.mutex import Priority
            priority = Priority.INTENT
        common = {"priority": priority, "intent": intent, "user_id": user_id}

        if intent in ("lights_off", "lights_on"):
            service = "turn_off" if intent == "lights_off" else "turn_on"
            acted = await self._call_domain_in_area("light", service, area_id, **common)
            return {"executed": bool(acted), "intent": intent, "entities": acted}
        if intent == "secure_area":
            covers = await self._call_domain_in_area(
                "cover", "close_cover", area_id, high_stakes=True,
                desired_state="closed", **common)
            locks = await self._call_domain_in_area(
                "lock", "lock", area_id, high_stakes=True, desired_state="locked", **common)
            acted = covers + locks
            return {"executed": bool(acted), "intent": intent, "entities": acted}
        if intent == "arm_alarm":
            return {"executed": False, "intent": intent,
                    "reason": ("arming the alarm is not available as a local voice "
                               "command; nothing was done")}
        if intent in ("context_off", "context_close"):
            return await self._act_on_pronoun(intent, area_id, priority, user_id)
        return {"executed": False, "intent": intent, "reason": "unhandled intent"}

    async def route(self, phrase: str, target_area: str, *, user_id: str | None = None) -> dict:
        """Match the phrase and carry it out. Never raises."""
        decision = match_intent(phrase)
        if decision is None:
            _LOGGER.debug("intent: no local match for %r", phrase)
            return {"matched": False, "intent": None, "executed": False}
        return {"matched": True, **await self.execute(decision, target_area,
                                                      user_id=user_id)}

    # ── the confirmation window ─────────────────────────────────────────────

    async def open_feedback_window(
        self, pending_action: dict, *, timeout: float = FEEDBACK_TIMEOUT_S
    ) -> None:
        """Hold pending_action ({"intent": ..., "area": ...} at least) for
        timeout seconds, replacing any window already open, and tell the
        satellites to listen without the wake word."""
        from homeassistant.helpers.event import async_call_later

        self._cancel_feedback()
        self._pending_feedback = pending_action
        self.hass.bus.async_fire(EVENT_FEEDBACK_WINDOW,
                                 {"timeout": timeout, "action": pending_action})

        def expired(_now) -> None:
            self._pending_feedback = None
            self._feedback_cancel = None

        self._feedback_cancel = async_call_later(self.hass, timeout, expired)

    async def handle_voice_response(self, phrase: str) -> dict:
        """While a window is open, a yes closes it and carries out the
        pending action. Anything else leaves it open."""
        pending = self._pending_feedback
        if pending is None:
            return {"handled": False, "reason": "no open window"}
        if not is_affirmative(phrase):
            return {"handled": False, "affirmative": False}
        self._cancel_feedback()
        return {"handled": True, **await self.execute({"intent": pending["intent"]},
                                                      pending["area"])}

    def _cancel_feedback(self) -> None:
        cancel, self._feedback_cancel = self._feedback_cancel, None
        self._pending_feedback = None
        if cancel is None:
            return
        try:
            cancel()
        except Exception:  # noqa: BLE001
            pass
