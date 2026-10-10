"""Local intent routing for Nova.

``LocalIntentRouter`` matches a spoken/typed phrase against a small table of
local command patterns (no cloud round-trip), resolves pronoun context ("turn it
off") to the active entity in a room, executes the change, and supports a short
interactive feedback window so an actionable announcement can be confirmed by
voice without a fresh wake word.

Module-level code is stdlib-only so the pure matching helpers
(``match_intent`` / ``is_affirmative``) load and test without Home Assistant.
Anything touching hass (entity-area lookup, service calls, the feedback timer)
is imported lazily inside the methods that need it.
"""
from __future__ import annotations

import asyncio
import logging
import re

_LOGGER = logging.getLogger(__name__)

# Fired when an actionable announcement opens a confirmation window; the voice
# satellite layer listens for this to start a short, wake-word-free capture.
EVENT_FEEDBACK_WINDOW = "nova_feedback_window"
FEEDBACK_TIMEOUT_S = 10.0

# Intent table — ordered most-specific first so "turn off the lights" matches
# the light intent rather than the pronoun ("it") intent.
_INTENT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Arming is not a local command (8.7.20): it used to fall into secure_area,
    # which closed covers and locked locks, never armed anything, and reported
    # success. It is matched here only so it can be refused plainly.
    ("arm_alarm", (
        r"\barm\b.*\b(garage|house|home|alarm)\b",
    )),
    ("secure_area", (
        r"\bsecure\b",
        r"\bclose\b.*\bgarage\b",
        r"\block\s+(up|down|the|it)\b",
    )),
    ("lights_off", (
        r"\b(turn|switch|shut)\s+off\b.*\blight",
        r"\blights?\s+off\b",
        r"\bkill\s+the\s+lights?\b",
    )),
    ("lights_on", (
        r"\b(turn|switch)\s+on\b.*\blight",
        r"\blights?\s+on\b",
    )),
    ("context_off", (
        r"\bturn\s+it\s+off\b",
        r"\bswitch\s+it\s+off\b",
        r"\bshut\s+it\s+off\b",
        r"\bturn\s+that\s+off\b",
        r"\bpause\s+it\b",
    )),
    ("context_close", (
        r"\bclose\s+it\b",
        r"\bshut\s+it\b",
    )),
)

_AFFIRMATIVE_PATTERNS: tuple[str, ...] = (
    r"\b(yes|yeah|yep|yup|sure|ok|okay|affirmative|confirm(ed)?|proceed|go ahead|do it)\b",
    r"\b(close|shut|secure)\s+it\b",
)

# A phrase with any of these is never a yes and never a command (8.7.20):
# "no, don't do it" used to confirm, and "don't secure the garage" used to
# secure it. Failing toward doing nothing is the safe direction.
_NEGATION = re.compile(r"\b(no|not|never|cancel|stop|don[\u2019']?t|do\s+not)\b")

# A question asks; it never acts (8.7.20): "is the garage secure?" used to
# close the garage. A trailing "?" or a leading question word marks one.
_QUESTION_START = re.compile(
    r"^(is|are|was|were|am|do|does|did|has|have|had|what|what's|whats|why|how|"
    r"when|where|which|who|whose)\b")

_ACTIVE_MEDIA_STATES = {"playing"}
_ACTIVE_LIGHT_STATES = {"on"}
_ACTIVE_GENERIC_STATES = {"on", "open"}


def match_intent(phrase: str) -> dict | None:
    """Return {"intent": name, "raw": phrase} for the first matching pattern, or
    None if the phrase doesn't map to a known local command. Pure function."""
    if not phrase:
        return None
    text = phrase.lower().strip()
    if _NEGATION.search(text) or text.endswith("?") or _QUESTION_START.match(text):
        return None
    for name, patterns in _INTENT_PATTERNS:
        if any(re.search(pat, text) for pat in patterns):
            return {"intent": name, "raw": phrase}
    return None


def is_affirmative(phrase: str) -> bool:
    """True if the phrase reads as a yes/confirmation. Pure function."""
    if not phrase:
        return False
    text = phrase.lower().strip()
    if _NEGATION.search(text):
        return False
    return any(re.search(pat, text) for pat in _AFFIRMATIVE_PATTERNS)


class LocalIntentRouter:
    """Route phrases to local actions, with room-context pronoun resolution and
    a short voice-confirmation window."""

    def __init__(self, hass, *, ledger=None, mutex=None) -> None:
        self.hass = hass
        self.ledger = ledger  # optional StateLedger (duck-typed); write-ahead for high-stakes
        self.mutex = mutex    # optional EntityLockRegistry (duck-typed); concurrency control
        self._pending_feedback: dict | None = None
        self._feedback_cancel = None
        # Seconds to wait before rereading what a voice reply locked or
        # closed (8.24.0), the same wait lockdown uses.
        self.verify_delay = 25.0

    # ── Area helper (lazy import keeps the module HA-free at import time) ──
    def _area_of(self, entity_id: str):
        from .. import audio_routing  # lazy
        return audio_routing.entity_area(self.hass, entity_id)

    # ── Context resolution ────────────────────────────────────────────────
    def resolve_active_entity(
        self, area_id: str,
        domains: tuple[str, ...] = ("media_player", "light"),
        *, area_of=None,
    ) -> tuple[str | None, str | None]:
        """Find the entity a pronoun ("it") most likely refers to in an area:
        a playing media_player first, then an 'on' light, then any other 'on'/
        'open' entity in the requested domains. Returns (entity_id, domain)."""
        resolve_area = area_of or self._area_of
        for domain in domains:
            for st in self.hass.states.async_all(domain):
                try:
                    if resolve_area(st.entity_id) != area_id:
                        continue
                except Exception:  # noqa: BLE001
                    continue
                state = str(st.state).lower()
                if domain == "media_player" and state in _ACTIVE_MEDIA_STATES:
                    return st.entity_id, domain
                if domain == "light" and state in _ACTIVE_LIGHT_STATES:
                    return st.entity_id, domain
                if domain not in ("media_player", "light") and state in _ACTIVE_GENERIC_STATES:
                    return st.entity_id, domain
        return None, None

    # ── Authority and the action log (8.24.0) ─────────────────────────────
    def _authorized(self, domain: str, service: str, entity_ids: list[str],
                    user_id: str | None = None) -> tuple[list[str], list[str]]:
        """Split entity_ids into (allowed, held) by the one authority check,
        as a voice request. Locking, closing and lights are allowed; anything
        that would need a confirmation is held, never sent. Fails closed."""
        allowed: list[str] = []
        held: list[str] = []
        try:
            from .. import policy  # lazy — keeps module HA-free
        except Exception:  # noqa: BLE001
            return [], list(entity_ids)
        for eid in entity_ids:
            try:
                ok = policy.authorize_now(self.hass, policy.AuthorityRequest(
                    domain, service, eid, source=policy.SOURCE_VOICE,
                    user_id=user_id or "")).allowed
            except Exception:  # noqa: BLE001
                ok = False
            (allowed if ok else held).append(eid)
        return allowed, held

    async def _log_start(self, intent: str, domain: str, service: str,
                         allowed: list[str], held: list[str],
                         user_id: str | None = None) -> dict:
        """One action log request for this voice reply: a row per device,
        the held ones already blocked. Best effort; returns {entity_id: row}."""
        rows: list[dict] = [{"key": e, "domain": domain, "service": service, "entity_id": e}
                            for e in allowed]
        rows += [{"key": e, "domain": domain, "service": service, "entity_id": e,
                  "approval_required": True, "approval_result": "not_requested",
                  "execution_result": "blocked",
                  "reason_code": "confirmation_unavailable_in_voice_reply"}
                 for e in held]
        if not rows:
            return {}
        try:
            from .. import action_log
            request_id = action_log.new_request_id()
            return await self.hass.async_add_executor_job(
                lambda: action_log.start_many(
                    request_id, f"voice_reply:{intent}", "voice", rows,
                    requested_by_user_id=user_id or None)) or {}
        except Exception:  # noqa: BLE001
            return {}

    async def _log_set(self, row_ids: dict, entity_ids: list[str], result: str,
                       reason_code: str | None = None) -> None:
        try:
            from .. import action_log
            for eid in entity_ids:
                rid = row_ids.get(eid)
                if rid is not None:
                    await self.hass.async_add_executor_job(
                        lambda r=rid: action_log.set_execution(
                            r, result, reason_code=reason_code))
        except Exception:  # noqa: BLE001
            pass

    def _schedule_check(self, intent: str, domain: str, targets: list[str],
                        row_ids: dict, user_id: str | None) -> None:
        """Locks and covers a voice reply secured are checked in the
        background (8.24.0). Never raises."""
        if domain not in ("lock", "cover") or not targets:
            return
        try:
            self.hass.async_create_task(
                self._check_secured(intent, domain, list(targets), row_ids, user_id))
        except Exception:  # noqa: BLE001
            pass

    async def _check_secured(self, intent: str, domain: str, targets: list[str],
                             row_ids: dict, user_id: str | None) -> None:
        """After the wait, each lock must read locked and each cover closed.
        The action log rows become verified or unverified (a device that
        cannot be read is unverified), the result goes to the decision log,
        and anything not secured is raised as a high alert. Never raises."""
        try:
            await asyncio.sleep(self.verify_delay)
            from .. import alert_path, entity_verify
            expected = "locked" if domain == "lock" else "closed"
            secured: list[str] = []
            not_secured: list[str] = []
            for eid in targets:
                (secured if entity_verify.check_state_once(self.hass, eid, expected)
                 else not_secured).append(eid)
            await self._log_set(row_ids, secured, "verified")
            await self._log_set(row_ids, not_secured, "unverified",
                                "not_secure_after_check")
            await alert_path.async_record_decision(
                self.hass, "voice_secure", source="voice_reply",
                sit=alert_path.situation(self.hass),
                facts={"intent": intent, "secured": secured,
                       "not_secured_after_check": not_secured},
                assessment="asked by voice to secure the room",
                decision="checked, secured" if not not_secured
                else "checked, some not secured",
                reason="the voice reply's check after acting")
            if not_secured:
                from .. import core_state
                from ..core_bridge import _emit_action
                names = ", ".join(
                    (self.hass.states.get(e).attributes.get("friendly_name", e)
                     if self.hass.states.get(e) else e) for e in not_secured)
                await _emit_action(self.hass, core_state._CORE.config or {}, {
                    "type": "voice_secure_failed", "urgency": "high",
                    "message": f"I tried to secure {names}, but it is not secured yet. "
                               f"Please check it.",
                    "entity_id": not_secured[0], "auto_act": True,
                }, False)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("intent: secure check failed", exc_info=True)

    # ── Execution ─────────────────────────────────────────────────────────
    async def _call_domain_in_area(
        self, domain: str, service: str, area_id: str,
        *, high_stakes: bool = False, desired_state: str | None = None,
        priority: int | None = None, intent: str = "", user_id: str | None = None,
    ) -> list[str]:
        """Call a service on the entities of `domain` in `area_id`. Acquires a
        per-entity concurrency lock first (skipping entities held at equal-or-
        higher priority), writes a recovery-ledger intent before high-stakes
        actions, and releases the locks after. Returns the entity_ids acted on."""
        candidates: list[str] = []
        for st in self.hass.states.async_all(domain):
            try:
                if self._area_of(st.entity_id) != area_id:
                    continue
            except Exception:  # noqa: BLE001
                continue
            candidates.append(st.entity_id)
        if not candidates:
            return []
        candidates, held = self._authorized(domain, service, candidates, user_id)
        row_ids = await self._log_start(intent or f"{domain}.{service}", domain, service,
                                        candidates, held, user_id)
        if not candidates:
            return []

        # Concurrency control: only act on entities we can lock at this priority.
        tokens: dict = {}
        if self.mutex is not None and priority is not None:
            targets: list[str] = []
            for eid in candidates:
                token = self.mutex.try_acquire(eid, priority)
                if token is not None:
                    tokens[eid] = token
                    targets.append(eid)
                else:
                    _LOGGER.info("intent: %s busy (higher-priority lock) — skipping", eid)
            if not targets:
                await self._log_set(row_ids, candidates, "blocked", "entity_busy")
                return []
        else:
            targets = candidates
        busy = [e for e in candidates if e not in targets]
        if busy:
            await self._log_set(row_ids, busy, "blocked", "entity_busy")

        # Write-ahead: durably record intent BEFORE issuing a high-stakes call.
        txns: list[str] = []
        if high_stakes and self.ledger is not None and desired_state is not None:
            for eid in targets:
                try:
                    txns.append(
                        self.ledger.record_intent(
                            eid, desired_state, action=f"{domain}.{service}"
                        )
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("ledger record_intent failed for %s", eid)

        try:
            await self.hass.services.async_call(
                domain, service, {"entity_id": targets}, blocking=True
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("intent: %s.%s failed for %s", domain, service, targets)
            await self._log_set(row_ids, targets, "failed", "service_call_failed")
            self._release_all(tokens)
            return []
        await self._log_set(row_ids, targets, "accepted")
        self._schedule_check(intent or f"{domain}.{service}", domain, targets,
                             row_ids, user_id)

        for txn in txns:
            try:
                self.ledger.mark_complete(txn)
            except Exception:  # noqa: BLE001
                pass
        self._release_all(tokens)
        return targets

    def _release_all(self, tokens: dict) -> None:
        if self.mutex is None:
            return
        for token in tokens.values():
            try:
                self.mutex.release(token)
            except Exception:  # noqa: BLE001
                pass

    async def execute(self, decision: dict, area_id: str, *, user_id: str | None = None) -> dict:
        """Carry out a matched intent in the given area."""
        intent = decision["intent"]

        priority = None
        if self.mutex is not None:
            from ..automation.mutex import Priority  # lazy — keeps module HA-free
            priority = Priority.INTENT

        if intent == "lights_off":
            acted = await self._call_domain_in_area(
                "light", "turn_off", area_id, priority=priority, intent=intent, user_id=user_id
            )
            return {"executed": bool(acted), "intent": intent, "entities": acted}

        if intent == "lights_on":
            acted = await self._call_domain_in_area(
                "light", "turn_on", area_id, priority=priority, intent=intent, user_id=user_id
            )
            return {"executed": bool(acted), "intent": intent, "entities": acted}

        if intent == "arm_alarm":
            return {"executed": False, "intent": intent,
                    "reason": "arming the alarm is not available as a local voice "
                              "command; nothing was done"}

        if intent == "secure_area":
            covers = await self._call_domain_in_area(
                "cover", "close_cover", area_id,
                high_stakes=True, desired_state="closed", priority=priority,
                intent=intent, user_id=user_id,
            )
            locks = await self._call_domain_in_area(
                "lock", "lock", area_id,
                high_stakes=True, desired_state="locked", priority=priority,
                intent=intent, user_id=user_id,
            )
            return {
                "executed": bool(covers or locks),
                "intent": intent,
                "entities": covers + locks,
            }

        if intent in ("context_off", "context_close"):
            # "close it" means an open cover (8.7.20); it used to turn off
            # the playing media or the light and could never close a cover.
            domains = ("cover",) if intent == "context_close" else ("media_player", "light")
            entity_id, domain = self.resolve_active_entity(area_id, domains)
            if entity_id is None:
                return {"executed": False, "intent": intent,
                        "reason": "nothing active to act on in this area"}
            service = "close_cover" if domain == "cover" else "turn_off"
            allowed, held = self._authorized(str(domain), service, [entity_id], user_id)
            row_ids = await self._log_start(intent, str(domain), service, allowed, held, user_id)
            if not allowed:
                return {"executed": False, "intent": intent, "entity_id": entity_id,
                        "reason": "needs a confirmation the voice reply cannot ask for"}
            token = None
            if self.mutex is not None and priority is not None:
                token = self.mutex.try_acquire(entity_id, priority)
                if token is None:
                    await self._log_set(row_ids, [entity_id], "blocked", "entity_busy")
                    return {"executed": False, "intent": intent, "entity_id": entity_id,
                            "reason": "entity busy (higher-priority lock)"}
            try:
                await self.hass.services.async_call(
                    domain, service, {"entity_id": entity_id}, blocking=True
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("intent: context action failed for %s", entity_id)
                await self._log_set(row_ids, [entity_id], "failed", "service_call_failed")
                if token is not None:
                    self.mutex.release(token)
                return {"executed": False, "intent": intent, "entity_id": entity_id}
            await self._log_set(row_ids, [entity_id], "accepted")
            self._schedule_check(intent, str(domain), [entity_id], row_ids, user_id)
            if token is not None:
                self.mutex.release(token)
            return {"executed": True, "intent": intent, "entity_id": entity_id}

        return {"executed": False, "intent": intent, "reason": "unhandled intent"}

    async def route(self, phrase: str, target_area: str, *, user_id: str | None = None) -> dict:
        """Match a phrase and execute it. Returns a result dict; never raises."""
        decision = match_intent(phrase)
        if decision is None:
            _LOGGER.debug("intent: no local match for %r", phrase)
            return {"matched": False, "intent": None, "executed": False}
        result = await self.execute(decision, target_area, user_id=user_id)
        return {"matched": True, **result}

    # ── Interactive feedback window ───────────────────────────────────────
    async def open_feedback_window(
        self, pending_action: dict, *, timeout: float = FEEDBACK_TIMEOUT_S
    ) -> None:
        """Arm a short confirmation window. Fires EVENT_FEEDBACK_WINDOW for the
        voice satellite layer and auto-disarms after `timeout` seconds.

        `pending_action` should contain at least {"intent": ..., "area": ...}.
        """
        from homeassistant.helpers.event import async_call_later  # lazy

        self._cancel_feedback()
        self._pending_feedback = pending_action
        self.hass.bus.async_fire(
            EVENT_FEEDBACK_WINDOW, {"timeout": timeout, "action": pending_action}
        )

        def _expire(_now):
            self._pending_feedback = None
            self._feedback_cancel = None

        self._feedback_cancel = async_call_later(self.hass, timeout, _expire)

    async def handle_voice_response(self, phrase: str) -> dict:
        """Process a phrase captured during an open feedback window. If the
        window is open and the phrase is affirmative, execute the pending
        action."""
        if self._pending_feedback is None:
            return {"handled": False, "reason": "no open window"}
        if not is_affirmative(phrase):
            return {"handled": False, "affirmative": False}
        pending = self._pending_feedback
        self._cancel_feedback()
        result = await self.execute({"intent": pending["intent"]}, pending["area"])
        return {"handled": True, **result}

    def _cancel_feedback(self) -> None:
        if self._feedback_cancel is not None:
            try:
                self._feedback_cancel()
            except Exception:  # noqa: BLE001
                pass
        self._feedback_cancel = None
        self._pending_feedback = None
