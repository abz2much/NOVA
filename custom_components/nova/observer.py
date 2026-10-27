"""
Nova — the observer.

Listens to every state change in Home Assistant and decides which ones are
worth a word. Most are dropped for free: noisy entity ids, domains that are
never interesting, startup and recovery transitions, flapping sensors,
repeats inside a debounce window, bursts of numbered siblings, and motion
while the household is home. The rest go to the classifier model, then to
the reasoning step, and anything it wants said is handed to delivery as one
request (delivery applies the output gate, quiet hours, sleep and speaker
choice).

Two things sit above the shortcuts. A critical hazard turning on (smoke,
gas, carbon monoxide, a leak) skips every debounce and limit; the cognitive
core announces it when it is running, so the observer only handles it when
the core is not. And the local cognition layer sees every event and can
lift one the filter would drop, but never holds one back.

Settings read live from the owning entry's runtime config, then the config
the observer was started with: classifier_rate_limit (calls per hour, 0 for
no limit), cognition_enabled, cognition_threshold, observer_group_debounce.
"""
from __future__ import annotations

import contextlib
import logging
import re
import time
from collections import deque
from typing import Optional

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from . import audio_routing, classifier, output_gate, reasoning_loop
from . import sleep_detection  # noqa: F401  (read through world.py; tests patch it here)
from .cognitive import evaluators as cognitive_rules
from .llm_provider import create_tier_provider

_LOGGER = logging.getLogger(__name__)


# ─── What the pre filter drops ──────────────────────────────────────────────
# Dropping costs nothing; every event let through costs a classifier call,
# so the filter leans hard towards dropping.

IGNORED_DOMAINS = {
    # changes people make themselves, or that never need comment
    "light", "switch", "media_player", "scene", "script", "automation", "button",
    # locations and household helpers
    "person", "device_tracker", "zone", "sun", "weather", "calendar", "group",
    "input_text", "input_number", "number", "select", "text", "timer", "counter",
    # voice and system plumbing
    "conversation", "stt", "tts", "assist_satellite", "assist_pipeline", "update",
    # sensors are let back in only for the classes below
    "sensor",
}

# Sensor classes with discrete, safety relevant states. Measurements (power,
# energy, temperature, humidity and the like) change all day and stay out.
INTERESTING_SENSOR_CLASSES = {
    "smoke", "gas", "carbon_monoxide", "moisture",
    "problem", "safety", "tamper", "running",
}

# Classes that fire constantly; they get the long debounce.
HIGH_FREQ_BINARY_CLASSES = {"motion", "occupancy", "presence", "moving"}

WATCHED_DOMAINS = {"binary_sensor", "lock", "cover", "alarm_control_panel", "vacuum"}

# Pieces of an entity id that mean "measurement", for entities with no
# device class. "_w" only counts at the very end, so a window or water
# sensor is not mistaken for watts.
ENTITY_ID_NOISE_SUBSTRINGS = (
    "_w", "_wh", "_kwh", "_power", "_energy",
    "_volt", "_volts", "_voltage", "_amp", "_amps", "_amperage", "_current",
    "_batt", "_battery",
    "_rssi", "_lqi", "_linkquality", "_link_quality", "_signal",
    "_wifi", "_wlan", "_ble", "_bluetooth",
    "_cpu", "_ram", "_memory", "_disk", "_uptime",
    "_temp_", "_temperature", "_humid", "_humidity",
    "_pressure", "_dewpoint", "_lux", "_illuminance",
    "_freq", "_frequency", "_ping",
    "_cache", "_count", "_counter", "_runtime", "_duration",
)

# No longer used for filtering (media players are ignored); kept as a name.
INTERESTING_MEDIA_STATES = {"off", "idle", "playing"}

DEBOUNCE_DEFAULT_S = 60.0
DEBOUNCE_MOTION_S = 300.0
GROUP_DEBOUNCE_S = 90.0

# A previous state held for less than this is flapping, not an event.
MIN_PREVIOUS_STATE_HOLD_S = 10.0

GLOBAL_CLASSIFIER_RATE_LIMIT_PER_HOUR = 30


def _entity_id_looks_noisy(entity_id: str) -> bool:
    lowered = entity_id.lower()
    if lowered.endswith("_w"):
        return True
    return any(piece in lowered for piece in ENTITY_ID_NOISE_SUBSTRINGS if piece != "_w")


# ─── Shared state ───────────────────────────────────────────────────────────

class _ObserverState:
    """Everything the running observer holds. The tier clients are borrowed
    from a ProviderManager (the entry's, or one the observer owns only when
    it runs without an entry); `entry` is the config entry whose live
    settings the pipeline reads."""

    def __init__(self) -> None:
        self.hass = None
        self.config: dict = {}
        # entity -> when it was last let through (the per entity debounce)
        self.last_seen: dict[str, float] = {}
        # the context buffer behind get_recent_context()
        self.recent_events: deque[dict] = deque(maxlen=50)
        # one stamp per classifier call, for the hourly limit
        self.classifier_timestamps: deque[float] = deque(maxlen=5000)
        self._set_stopped()

    def _set_stopped(self) -> None:
        """The fields that describe a running observer, put back to idle."""
        self.running = False
        self.unsub = None
        self.entry = None
        self.providers = None
        self.owns_providers = False
        self.classifier_provider = self.reasoning_provider = None
        self.rate_limit_warn_logged = False

    def reset(self) -> None:
        """Back to stopped. hass and config are left for late readers, and
        the buffers are emptied where they are."""
        self._set_stopped()
        for buffer in (self.last_seen, self.recent_events, self.classifier_timestamps):
            buffer.clear()


_STATE = _ObserverState()


def _live_runtime_config() -> dict:
    """The owning entry's live panel settings ({} with no owning entry or an
    unloaded one). A loaded entry that lost its runtime raises rather than
    let the observer run on defaults."""
    if _STATE.entry is None:
        return {}
    from .runtime import lifecycle_runtime_config
    return lifecycle_runtime_config(_STATE.entry)


def _live_honorific(hass: HomeAssistant) -> str:
    """How to address whoever is home, worked out for each event."""
    try:
        from . import honorific as honorific_mod
        return honorific_mod.effective_honorific(hass)
    except Exception:
        return "sir"


def _setting(key: str, default, convert):
    """A setting from the live runtime config, else the start up config,
    else the default; a value that will not convert falls through."""
    live = _live_runtime_config()
    if key in live:
        try:
            return convert(live[key])
        except (TypeError, ValueError):
            pass
    try:
        return convert((_STATE.config or {}).get(key, default))
    except (TypeError, ValueError):
        return default


# ─── Reading an event ───────────────────────────────────────────────────────

def _states(event: Event):
    return event.data.get("old_state"), event.data.get("new_state")


def _is_person_home_transition(event: Event) -> bool:
    """A person entity arriving home or leaving it. People are otherwise
    ignored (zone names and GPS change all the time), but the classifier
    has its own arrival and departure handling for exactly this."""
    if not event.data.get("entity_id", "").startswith("person."):
        return False
    before, after = event.data.get("old_state"), event.data.get("new_state")
    if before is None or after is None:
        return False
    return (before.state == "home") != (after.state == "home")


def _critical_hazard(event: Event) -> bool:
    """A smoke, gas, carbon monoxide or leak sensor (and the like) turning on."""
    try:
        before, after = _states(event)
        if before is None or after is None:
            return False
        return cognitive_rules.is_critical_hazard_activation(
            after.attributes.get("device_class") or "", before.state, after.state)
    except Exception:
        return False


def _core_running() -> bool:
    """Whether the cognitive core (which announces hazards) is running."""
    try:
        from .core_state import _CORE
        return bool(_CORE.running)
    except Exception:
        return False


def _entry_state_recovery(event: Event) -> bool:
    """A door, window, garage door or lock reading again after unknown or
    unavailable: a restart or reconnection, not something anyone did."""
    before, after = _states(event)
    if after is None:
        return False
    return cognitive_rules.is_entry_state_recovery(
        event.data.get("entity_id", ""), after.attributes.get("device_class") or "",
        "unknown" if before is None else before.state, after.state)


def _people_and_alarm(hass: HomeAssistant):
    from . import alarm_source
    people = [s.state for s in hass.states.async_all("person")]
    alarms = [s.state for s in alarm_source.states(hass, _STATE.config)]
    return people, alarms


def _household_presence(hass: HomeAssistant) -> str:
    """Presence for ordinary openings: people, the security alarm, and the
    rooms occupied right now."""
    people, alarms = _people_and_alarm(hass)
    return cognitive_rules.household_presence(
        people, alarms, audio_routing.currently_occupied_areas(hass))


def _household_home() -> bool:
    """Home by people or the alarm only. Room occupancy is left out because
    the motion being judged occupies its own room. Errors read as not home,
    so the event takes its normal path."""
    try:
        people, alarms = _people_and_alarm(_STATE.hass)
        return cognitive_rules.household_presence(
            people, alarms, []) == cognitive_rules.PRESENCE_HOME
    except Exception:
        return False


def _alarm_watching() -> bool:
    """The chosen alarm is armed away or on vacation, so it watches the house
    itself and motion is left to it (8.81.0). Errors read as not watching,
    so the event takes its normal path."""
    try:
        _people, alarms = _people_and_alarm(_STATE.hass)
        return any(str(s or "").strip().lower() in cognitive_rules.AWAY_ALARM_STATES
                   for s in alarms)
    except Exception:
        return False


_STARTUP_STATES = ("unknown", "unavailable", "none")


def _user_excluded(entity_id: str) -> bool:
    """The user's own exclusions (entity, domain or label)."""
    if _STATE.hass is None:
        return False
    try:
        from .entity_filter import is_excluded
        return bool(is_excluded(_STATE.hass, entity_id))
    except Exception:
        return False


def _should_pre_filter(event: Event) -> bool:
    """True to drop the event before any model sees it."""
    entity_id = event.data.get("entity_id", "")
    if not entity_id or _user_excluded(entity_id) or _entity_id_looks_noisy(entity_id):
        return True

    domain = entity_id.partition(".")[0]
    if domain == "alarm_control_panel":
        from . import alarm_source
        if not alarm_source.is_selected(_STATE.hass, entity_id, _STATE.config):
            return True
    if domain in IGNORED_DOMAINS:
        if domain == "sensor":
            reading = event.data.get("new_state")
            if reading is None:
                return True
            if reading.attributes.get("device_class") in INTERESTING_SENSOR_CLASSES:
                return False
        return not (domain == "person" and _is_person_home_transition(event))
    if domain not in WATCHED_DOMAINS:
        return True

    before, after = event.data.get("old_state"), event.data.get("new_state")
    if before is None or after is None or before.state == after.state:
        return True
    # Safety first: a hazard turning on, or the chosen alarm triggering,
    # passes even from unknown and however quickly it re-tripped.
    if _critical_hazard(event) or (
            domain == "alarm_control_panel" and after.state == "triggered"):
        return False
    if after.state in _STARTUP_STATES or before.state in _STARTUP_STATES:
        return True
    try:
        held = (after.last_changed - before.last_changed).total_seconds()
    except Exception:
        return False
    return held < MIN_PREVIOUS_STATE_HOLD_S


# ─── Limits and debounces ───────────────────────────────────────────────────

def _effective_rate_limit() -> int:
    """Classifier calls allowed per hour; 0 or less means no limit (for a
    local model or a generous plan)."""
    return _setting("classifier_rate_limit", GLOBAL_CLASSIFIER_RATE_LIMIT_PER_HOUR, int)


def _classifier_rate_limited() -> bool:
    limit = _effective_rate_limit()
    stamps = _STATE.classifier_timestamps
    hour_ago = time.time() - 3600
    while stamps and stamps[0] < hour_ago:
        stamps.popleft()
    return limit > 0 and len(stamps) >= limit


def _record_classifier_call() -> None:
    _STATE.classifier_timestamps.append(time.time())


def _debounced(entity_id: str, min_interval_s: float = 30.0) -> bool:
    """True if this entity was let through within the interval; otherwise
    note it now and let it through."""
    now = time.time()
    if now - _STATE.last_seen.get(entity_id, 0.0) < min_interval_s:
        return True
    _STATE.last_seen[entity_id] = now
    return False


def _cognition_enabled() -> bool:
    return _setting("cognition_enabled", True, bool)


def _cognition_threshold() -> float:
    """How unusual an event must be for cognition to lift it."""
    return _setting("cognition_threshold", 0.6, float)


# An alarm panel can flip a whole bank of numbered zones at once. Each is
# its own entity, so the per entity debounce lets all of them through; this
# lets one per bank through per window. Intrusion checks run elsewhere and
# are not affected.
_GROUP_LAST: dict[str, float] = {}
_SEQ_SUFFIX_RE = re.compile(r"^(.*?)_(\d+)$")


def _group_key(entity_id: str) -> Optional[str]:
    """"domain.base" for an object id ending in _<number> with a base of at
    least four characters, else None."""
    domain, dot, object_id = entity_id.partition(".")
    if not dot:
        return None
    numbered = _SEQ_SUFFIX_RE.match(object_id)
    if numbered is None or len(numbered.group(1)) < 4:
        return None
    return f"{domain}.{numbered.group(1)}"


def _group_debounce_s() -> float:
    """The sibling window in seconds; 0 or less turns coalescing off."""
    return _setting("observer_group_debounce", GROUP_DEBOUNCE_S, float)


def _group_debounced(entity_id: str, interval: float) -> bool:
    key = _group_key(entity_id) if interval > 0 else None
    if key is None:
        return False
    now = time.time()
    sibling_recent = now - _GROUP_LAST.get(key, 0.0) < interval
    if not sibling_recent:
        _GROUP_LAST[key] = now
    return sibling_recent


# ─── Recent events ──────────────────────────────────────────────────────────

def _record_for_context(event: Event) -> None:
    entity_id = event.data.get("entity_id", "")
    before, after = _states(event)
    if after is None:
        return
    _STATE.recent_events.append({
        "ts": time.time(), "entity_id": entity_id,
        "old": before.state if before else None, "new": after.state,
        "fname": after.attributes.get("friendly_name", entity_id),
        "area": audio_routing.entity_area(_STATE.hass, entity_id),
    })


def _context_line(item: dict, now: float) -> str:
    seconds = int(now - item["ts"])
    age = f"{seconds}s ago" if seconds < 60 else f"{seconds // 60}m ago"
    if item.get("kind") == "camera":
        mark = "⚠ " if item.get("notable") else ""
        return f"  [{age}] {mark}Camera {item.get('camera', '')}: {item.get('summary', '')}"
    where = f" [{item['area']}]" if item.get("area") else ""
    return f"  [{age}]{where} {item['fname']}: {item['old']} → {item['new']}"


def get_recent_context(seconds: float = 600) -> str:
    """The last fifteen state changes and camera reviews within `seconds`,
    one per line, for prompts, briefings and the situation summary."""
    now = time.time()
    lines = [_context_line(item, now) for item in list(_STATE.recent_events)
             if item["ts"] >= now - seconds]
    return "\n".join(lines[-15:]) if lines else "quiet — no notable recent activity"


def record_camera_event(
    camera_name: str, summary: str, category: str = "other", notable: bool = False,
) -> None:
    """Add a camera review (from camera.py) to the recent events, so
    briefings and questions about what happened outside include it."""
    with contextlib.suppress(Exception):
        _STATE.recent_events.append({
            "ts": time.time(), "kind": "camera", "camera": camera_name,
            "summary": summary, "category": category, "notable": bool(notable),
            "area": None,
        })


# ─── The state change handler ───────────────────────────────────────────────

def _cognition_escalates(event: Event) -> tuple[bool, str]:
    """Run local cognition on the event: (lift it?, why)."""
    try:
        from . import cognition
        verdict = cognition.process(event, _cognition_threshold())
        return verdict.escalate, verdict.reason
    except Exception:
        return False, ""


def _log_escalation(entity_id: str, why: str) -> None:
    with contextlib.suppress(Exception):  # the panel log is best effort
        from .websocket import nova_log
        nova_log("CLASSIFY", f"cognition escalated {entity_id}: {why}")


def _ignored_by_core(entity_id: str) -> bool:
    try:
        from . import cognitive_core
        return bool(cognitive_core.is_ignored(entity_id))
    except Exception:
        return False


def _over_rate_limit(event: Event) -> bool:
    """Whether the hourly limit holds this event back (arrivals and
    departures never are), warning once per stretch at the limit."""
    if not _classifier_rate_limited() or _is_person_home_transition(event):
        _STATE.rate_limit_warn_logged = False
        return False
    if not _STATE.rate_limit_warn_logged:
        _LOGGER.warning(
            "Nova Observer: hit rate limit of %d classifier calls/hour. "
            "Dropping further events until the window clears. "
            "(Raise or disable via classifier_rate_limit; 0 = unlimited.)",
            _effective_rate_limit())
        _STATE.rate_limit_warn_logged = True
    return True


@callback
def _on_state_changed(event: Event) -> None:
    """Every state change comes through here, on the event loop. Events are
    still classified and logged with announcements off; whether anything is
    spoken is decided later."""
    if not _STATE.running:
        return

    lifted, why = False, ""
    if _cognition_enabled():
        lifted, why = _cognition_escalates(event)
    else:
        # Cognition is off, so this event is a gap in what it has seen.
        with contextlib.suppress(Exception):
            from . import cognition
            cognition.mark_unobserved()

    if _should_pre_filter(event):
        if not lifted:
            return
        _log_escalation(event.data.get("entity_id", ""), why)

    entity_id = event.data.get("entity_id", "")
    critical = _critical_hazard(event)
    if critical and _core_running():
        return          # the cognitive core announces hazards
    if not critical:
        if _entry_state_recovery(event) or _ignored_by_core(entity_id):
            return
        if _group_debounced(entity_id, _group_debounce_s()):
            return

    after = event.data.get("new_state")
    device_class = after.attributes.get("device_class") if after else None
    frequent = device_class in HIGH_FREQ_BINARY_CLASSES
    window = DEBOUNCE_MOTION_S if frequent else DEBOUNCE_DEFAULT_S
    if not critical and _debounced(entity_id, window):
        return

    # Motion and presence with the household home is everyday life: keep it
    # as context, do not spend a classifier call on it. Armed away the alarm
    # watches the house, so even unusual motion is left to it.
    if (not critical and frequent and entity_id.startswith("binary_sensor.")
            and ((not lifted and _household_home()) or _alarm_watching())):
        _record_for_context(event)
        return

    if not critical and _over_rate_limit(event):
        return
    _STATE.rate_limit_warn_logged = False
    _record_classifier_call()
    _record_for_context(event)
    _STATE.hass.async_create_task(_process_event(event))


# ─── The pipeline ───────────────────────────────────────────────────────────

def _event_at(event: Event) -> str:
    try:
        return dt_util.as_local(event.time_fired).isoformat(timespec="seconds")
    except Exception:
        return dt_util.now().isoformat(timespec="seconds")


async def _audit(event: Event, *, result: str, category: str, classifier_urgency: str,
                 sit=None, trace: Optional[dict] = None, reason: str = "",
                 urgency: str = "", message: str = "", out=None) -> None:
    """One Decision Record entry for an alert decision: the event and its
    time, the reading it was judged on, the rule and reason, the result
    (alerted, suppressed or ignored) and what delivery did. Never raises."""
    try:
        from . import alert_path
        before, after = _states(event)
        facts = {
            "event": {"type": category or "unclassified",
                      "from": getattr(before, "state", None),
                      "to": getattr(after, "state", None),
                      "at": _event_at(event)},
            "classifier_urgency": classifier_urgency,
            "rule": dict(trace or {}) or {"origin": "classifier",
                                          "rule": "not_worth_considering"},
        }
        if urgency:
            facts["urgency"] = urgency
        plan = None
        delivery_note = ""
        if out is not None:
            plan = alert_path.Plan(alert=out.decision.delivered, speak=out.spoken,
                                   push=bool(out.pushed), reason=out.decision.reason)
            if out.decision.reason:
                delivery_note = out.decision.reason
            elif out.spoken:
                delivery_note = "spoken"
            else:
                delivery_note = "pushed" if out.pushed else "not delivered"
        reading = sit if sit is not None else alert_path.situation(_STATE.hass, _STATE.config)
        await alert_path.async_record_decision(
            _STATE.hass, "observer", source="observer",
            entity_id=event.data.get("entity_id") or None, message=message, sit=reading,
            plan=plan, facts=facts, decision=result, reason=reason,
            request_ids=None if out is None else [out.request_id],
            delivery=delivery_note)
    except Exception as exc:
        _LOGGER.debug("Observer audit entry failed: %s", exc)


def _save_activity(entity_id: str, category: str, urgency: str, message: str) -> None:
    with contextlib.suppress(Exception):  # the activity log never blocks an alert
        from .database import save_activity
        save_activity(entity_id=entity_id, category=category, urgency=urgency,
                      message=message, was_spoken=False, source="observer")


async def _process_event(event: Event) -> None:
    """Classify one event, reason about it, and hand anything worth saying
    to delivery."""
    try:
        entity_id = event.data.get("entity_id", "")
        before, after = _states(event)
        if before is None or after is None:
            return
        name = after.attributes.get("friendly_name", entity_id)
        change = f"{name}: {before.state} → {after.state}"
        recent = get_recent_context(300)
        area = audio_routing.entity_area(_STATE.hass, entity_id)

        verdict = await classifier.classify(
            _STATE.hass, _STATE.classifier_provider,
            entity_id=entity_id, old_state=before.state, new_state=after.state,
            friendly_name=name, device_class=after.attributes.get("device_class"),
            now_hhmm=dt_util.now().strftime("%H:%M"), recent_activity_summary=recent)

        if not verdict.get("worth_considering"):
            if _critical_hazard(event):
                await _audit(event, result="ignored", category=verdict.get("category", ""),
                             classifier_urgency=verdict.get("urgency", ""),
                             reason="the classifier judged it not worth considering")
            _save_activity(entity_id, "classified", "low",
                           f"{change} — not worth considering")
            return

        hint, category = verdict["urgency"], verdict["category"]
        if (category == "doors_windows" and hint != "critical"
                and _household_presence(_STATE.hass) == cognitive_rules.PRESENCE_HOME):
            _save_activity(entity_id, category, "low",
                           f"{change} — household present, not announced")
            return
        _save_activity(entity_id, category, hint, f"{change} — flagged for reasoning")
        _LOGGER.info("Observer flagged %s [%s]: %s → %s (urgency=%s, cat=%s)",
                     entity_id, area or "no-area", before.state, after.state, hint, category)

        # One reading of sleep, quiet hours and who is home, from the same
        # world.py resolvers delivery uses. Quiet hours go by the clock, not
        # by anyone being in a bedroom.
        from . import alert_path, world
        asleep = world.asleep(_STATE.hass)
        quiet = world.quiet_hours(_STATE.hass)
        rooms = audio_routing.currently_occupied_areas(_STATE.hass)
        if asleep.value is True:
            presence_note = f"user appears to be sleeping ({asleep.source})"
        else:
            presence_note = f"awake; occupied areas: {rooms or 'none detected'}"
        sit = alert_path.situation(
            _STATE.hass, _STATE.config, sleeping=asleep, quiet=quiet,
            house=world.presence(_STATE.hass, _STATE.config).value)

        trace: dict = {}
        where = f" in {area}" if area else ""
        decision = await reasoning_loop.decide(
            _STATE.hass, _STATE.reasoning_provider,
            honorific=_live_honorific(_STATE.hass),
            event_summary=(f"{name} ({entity_id}){where} "
                           f"changed from {before.state} to {after.state}"),
            home_state_summary=recent, classifier_urgency=hint,
            classifier_category=category,
            recent_announcements=output_gate.recent_announcements(5),
            presence_context=presence_note, anyone_home=sit.anyone_home,
            entity_id=entity_id, device_class=after.attributes.get("device_class") or "",
            from_state=before.state, to_state=after.state, friendly_name=name,
            trace=trace)
        reason = str(decision.get("reason") or "")

        if not decision.get("speak"):
            _LOGGER.debug("Observer silent for %s: %s", entity_id, decision.get("reason"))
            await _audit(event, result="ignored", category=category,
                         classifier_urgency=hint, sit=sit, trace=trace, reason=reason,
                         urgency=str(decision.get("urgency") or ""))
            return

        message = decision["message"]
        # The reasoning model over-calls critical; it stands only when the
        # classifier said so too, so "critical bypasses sleep" stays for
        # real hazards.
        urgency = cognitive_rules.cap_reasoned_urgency(decision["urgency"], hint)
        if urgency != decision["urgency"]:
            _LOGGER.info("Observer: downgrading urgency critical→high for %s "
                         "(reasoning said critical but classifier said %s)",
                         entity_id, hint)
        if quiet.value is True and urgency != "critical":
            with contextlib.suppress(Exception):
                from .websocket import nova_log
                nova_log("GATE", f"quiet hours — holding {urgency} announcement (not critical)")

        from . import delivery
        out = await delivery.async_deliver(_STATE.hass, delivery.DeliveryRequest(
            source="observer", message=message, urgency=urgency, entity_id=entity_id,
            category=category, push=delivery.PUSH_HIGH,
            title="⚠ Nova URGENT" if urgency == "critical" else "Nova",
            tts_context="sentinel", reading=sit))
        if not out.spoken:
            _LOGGER.info("Observer not spoken '%s' — %s", message, out.decision.reason)
        await _audit(event, result="alerted" if (out.spoken or out.pushed) else "suppressed",
                     category=category, classifier_urgency=hint, sit=sit, trace=trace,
                     reason=reason, urgency=urgency, message=message, out=out)
    except Exception as exc:
        _LOGGER.exception("Observer pipeline error: %s", exc)


# ─── Lifecycle ──────────────────────────────────────────────────────────────

_CLASSIFIER_BINDING = "observer_classifier"
_REASONING_BINDING = "observer_reasoning"


def _tier_manager(hass: HomeAssistant, entry):
    """(manager, owned): the entry's ProviderManager, else the loaded entry's,
    else a new one the observer owns and closes in stop()."""
    from .providers.manager import ProviderManager, entry_manager, runtime_manager
    shared = entry_manager(entry) if entry is not None else None
    if shared is None:
        shared = runtime_manager(hass)
    if shared is not None:
        return shared, False
    return ProviderManager(hass), True


def _tier_requests(config: dict) -> dict:
    from .providers.routing import tier_spec
    return {
        binding: (tier_spec(config, tier),
                  lambda tier=tier: create_tier_provider(config, tier))
        for binding, tier in ((_CLASSIFIER_BINDING, "classifier"),
                              (_REASONING_BINDING, "reasoning"))
    }


def _with_ai_keys(config: dict, snapshot: dict) -> dict:
    """The start up config with the panel's model choices laid over it (the
    entry's own data predates anything saved from the panel)."""
    chosen = {key: value for key, value in snapshot.items()
              if key == "llm_base_url" or key.endswith(("_provider", "_model", "_base_url"))}
    return {**config, **chosen}


async def _release_tier_providers() -> None:
    """Give the tier clients back: release the bindings in a shared
    manager, or close the observer's own."""
    manager, owned = _STATE.providers, _STATE.owns_providers
    _STATE.providers = None
    _STATE.owns_providers = False
    _STATE.classifier_provider = None
    _STATE.reasoning_provider = None
    if manager is None:
        return
    try:
        if owned:
            await manager.async_close()
            return
        for binding in (_CLASSIFIER_BINDING, _REASONING_BINDING):
            await manager.async_release(binding)
    except Exception as exc:
        _LOGGER.debug("Observer: releasing tier providers failed: %s", exc)


async def start(hass: HomeAssistant, config: dict, entry=None) -> None:
    """Start observing (restarting if already running). With an entry, its
    runtime supplies the live settings and the ownership check happens
    before anything changes."""
    if entry is not None:
        from .runtime import current_runtime
        current_runtime(entry)
    if _STATE.running:
        await stop()

    _STATE.hass, _STATE.entry = hass, entry
    if entry is not None:
        from .runtime import runtime_config_snapshot
        config = _with_ai_keys(config, runtime_config_snapshot(entry))
    _STATE.config = config

    try:
        # Built off the event loop (client construction reads certificates).
        manager, owned = _tier_manager(hass, entry)
        _STATE.providers, _STATE.owns_providers = manager, owned
        clients = await manager.async_acquire_all(_tier_requests(config))
        _STATE.classifier_provider = clients[_CLASSIFIER_BINDING]
        _STATE.reasoning_provider = clients[_REASONING_BINDING]
    except Exception as exc:
        _LOGGER.error("Observer: failed to create tier providers: %s", exc)
        await _release_tier_providers()
        return

    _STATE.running = True
    _STATE.unsub = hass.bus.async_listen("state_changed", _on_state_changed)
    _LOGGER.info("Nova Observer started (classifier=%s, reasoning=%s, bedrooms=%s)",
                 config.get("classifier_model", "default"),
                 config.get("reasoning_model", "default"),
                 config.get("bedroom_areas", []))

    # The appliance monitor and proactive briefings run alongside. The
    # cognitive core is started by setup on every install, not here.
    async def appliances() -> None:
        from . import appliance_monitor
        await appliance_monitor.start(hass, config, entry=entry)

    async def briefings() -> None:
        from . import proactive_briefing
        await proactive_briefing.start(hass, config)

    for label, begin in (("Appliance monitor", appliances),
                         ("Proactive briefing", briefings)):
        try:
            await begin()
        except Exception as exc:
            _LOGGER.warning("%s start failed (non-fatal): %s", label, exc)


async def refresh_tier_providers(hass: HomeAssistant, updates: Optional[dict] = None) -> None:
    """Swap in classifier and reasoning clients built from the current
    settings without restarting anything else, so a model change applies
    on the next event. Does nothing while stopped, or when `updates` names
    no tier provider or model. Both clients change together or not at all;
    on failure the old ones stay."""
    if not _STATE.running:
        return
    tier_keys = {"classifier_provider", "classifier_model",
                 "reasoning_provider", "reasoning_model"}
    if updates is not None and not tier_keys & set(updates):
        return

    config = dict(_STATE.config or {})
    if _STATE.entry is not None:
        from .runtime import runtime_config_snapshot
        config = _with_ai_keys(config, runtime_config_snapshot(_STATE.entry))

    manager = _STATE.providers
    if manager is None or manager.closed:
        manager, owned = _tier_manager(hass, _STATE.entry)
        _STATE.providers, _STATE.owns_providers = manager, owned
    try:
        clients = await manager.async_acquire_all(_tier_requests(config))
    except Exception as exc:
        _LOGGER.warning("Observer: tier provider refresh failed (keeping the "
                        "previous provider live): %s", exc)
        return

    _STATE.config = config
    _STATE.classifier_provider = clients[_CLASSIFIER_BINDING]
    _STATE.reasoning_provider = clients[_REASONING_BINDING]
    _LOGGER.info(
        "Nova Observer: tier providers refreshed live (classifier=%s/%s, reasoning=%s/%s)",
        getattr(_STATE.classifier_provider, "name", "?"),
        config.get("classifier_model", "default"),
        getattr(_STATE.reasoning_provider, "name", "?"),
        config.get("reasoning_model", "default"))


async def stop() -> None:
    """Stop listening, stop the companions, give back the clients and mark
    the gap in observation for cognition."""
    async def appliances() -> None:
        from . import appliance_monitor
        await appliance_monitor.stop()

    async def briefings() -> None:
        from . import proactive_briefing
        await proactive_briefing.stop()

    # Each step is independent: one failing must not keep the rest from
    # running, or leave the observer marked as running.
    unsubscribe = _STATE.unsub
    if unsubscribe is not None:
        with contextlib.suppress(Exception):
            unsubscribe()
    for halt in (appliances, briefings):
        with contextlib.suppress(Exception):
            await halt()
    await _release_tier_providers()
    with contextlib.suppress(Exception):
        from . import cognition
        cognition.mark_unobserved()
    _STATE.reset()
    _LOGGER.info("Nova Observer stopped")


def is_running() -> bool:
    """Whether the observer is listening to state changes."""
    return _STATE.running
