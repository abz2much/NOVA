"""Nova sentinel: watches doors, windows, garages and locks and speaks up when
one is left in an alerting state for too long."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from . import nova_config
from .const import CONF_TTS_ENGINE, DEFAULT_TTS_ENGINE
from .database import save_message, save_sentinel_event
from .directive_helper import build_system_prompt
from .tts_helper import resolve_tts_entity

_LOGGER = logging.getLogger(__name__)

SENTINEL_MODEL = "openai/gpt-oss-120b"

_OPEN_TEXT = "{honorific}, {friendly_name} has been open for {minutes} minutes."
_UNLOCKED_TEXT = "{honorific}, {friendly_name} has been unlocked for {minutes} minutes."

# The rules sentinel runs when the config names none. Night motion rules were
# dropped long ago (every trip to the bathroom set them off); anyone who wants
# them adds them under sentinel_rules. A mild day (above 10 C) silences the
# door and window rules, since an open door is no heat loss worry then.
DEFAULT_RULES = [
    {"id": "door_left_open", "domain": "binary_sensor", "device_class": "door",
     "state": "on", "for_minutes": 10, "skip_if_outdoor_above_c": 10,
     "message": _OPEN_TEXT},
    {"id": "window_left_open", "domain": "binary_sensor", "device_class": "window",
     "state": "on", "for_minutes": 30, "skip_if_outdoor_above_c": 10,
     "message": _OPEN_TEXT},
    # A garage door may be a contact sensor or a cover, so the rule has a cover
    # variant as well.
    {"id": "garage_left_open", "domain": "binary_sensor", "device_class": "garage_door",
     "state": "on", "also": [{"domain": "cover", "device_class": "garage", "state": "open"}],
     "for_minutes": 15, "message": _OPEN_TEXT},
    # The lock domain also holds config locks (a thermostat keypad lock, for
    # one), so this rule only takes locks _is_security_relevant_lock accepts.
    {"id": "lock_unlocked", "domain": "lock", "state": "unlocked", "for_minutes": 20,
     "requires_security_lock": True, "message": _UNLOCKED_TEXT},
]

# Asking Nova to ignore something quiets its announcements only. These checks
# drop an entity solely through the user's exclusion list (entity_filter).


def _variants(rule: dict) -> list:
    """A rule followed by each dict in its `also` list."""
    extra = [alt for alt in rule.get("also") or [] if isinstance(alt, dict)]
    return [rule, *extra]


def _watched_states(entity_id: str, rule: dict) -> set:
    """Every state the rule alerts on for this entity's domain."""
    states = set()
    for variant in _variants(rule):
        domain = variant.get("domain")
        if domain and not entity_id.startswith(f"{domain}."):
            continue
        states.add(variant.get("state"))
    return states


def _never_excluded(_hass, _entity_id) -> bool:
    return False


def _exclusion_check():
    """entity_filter.is_excluded, or a check that excludes nothing when the
    filter cannot be loaded."""
    try:
        from .entity_filter import is_excluded
    except Exception:
        return _never_excluded
    return is_excluded


def _excluded_now(hass, entity_id) -> bool:
    """is_excluded, treating any failure as not excluded."""
    try:
        return bool(_exclusion_check()(hass, entity_id))
    except Exception:
        return False


def _is_security_relevant_lock(hass: HomeAssistant, entity_id: str) -> bool:
    """True when a lock entity is a real door or access lock.

    Locks carry no device class that marks a keypad or child lock, and a name
    proves nothing, so only registry facts can say no: an entity category of
    any kind, or a device that also owns a climate entity (that lock belongs to
    a thermostat). The category alone is not enough, as some integrations
    never set it. Anything unknown, or any lookup error, counts as a real lock.
    """
    try:
        registry = er.async_get(hass)
        entry = registry.async_get(entity_id)
        if entry is None:
            return True
        if entry.entity_category is not None:
            return False
        if entry.device_id:
            on_device = er.async_entries_for_device(
                registry, entry.device_id, include_disabled_entities=True)
            if any(other.entity_id.startswith("climate.") for other in on_device):
                return False
    except Exception as exc:
        _LOGGER.debug("Sentinel: lock check for %s failed: %s", entity_id, exc)
    return True


def _to_celsius(value: float, unit) -> float:
    if unit and "F" in unit.upper():
        return (value - 32.0) * 5.0 / 9.0
    return value


def _fill(template: str, honorific: str, friendly_name: str, minutes: int) -> str:
    """Render a rule message. The usual "{honorific}, " opening is dropped when
    nobody is addressed, and the rest then starts with a capital."""
    opening = "{honorific}, "
    if not template.startswith(opening):
        return template.format(friendly_name=friendly_name, minutes=minutes,
                               honorific=honorific)
    rest = template[len(opening):].format(friendly_name=friendly_name, minutes=minutes)
    if honorific:
        return f"{honorific}, {rest}"
    return rest[:1].upper() + rest[1:]


def _rule_ids(raw) -> list:
    """disabled_sentinel_rules arrives as a JSON string or a list."""
    if isinstance(raw, str):
        return json.loads(raw)
    return raw if isinstance(raw, list) else []


class NovaSentinel:
    """Follows the watched entities and announces any rule that fires."""

    def __init__(self, hass: HomeAssistant, groq_client, honorific: str,
                 rules: list[dict] | None = None, entry: ConfigEntry | None = None) -> None:
        self.hass, self._entry, self._groq = hass, entry, groq_client
        self._rules = rules or DEFAULT_RULES
        # Only a fallback: the honorific is looked up afresh for every alert.
        self._honorific = honorific
        self._active = False
        self._entity_cache: list[str] = []
        self._unsubs: list = []
        # Keyed "entity_id:rule_id": when the rule began to match, and when it
        # was last announced.
        self._state_start: dict[str, datetime] = {}
        self._last_announced: dict[str, datetime] = {}

    def _setting(self, key, default):
        return nova_config.runtime_get(self.hass, self._entry, key, default)

    def _tts_entity(self) -> str | None:
        if not self._entry:
            return None
        return resolve_tts_entity(self.hass, self._setting(CONF_TTS_ENGINE, DEFAULT_TTS_ENGINE))

    def _speakers(self) -> list[str]:
        """The panel's announcement speakers, else the broadcast group. With
        neither set, sentinel has no speakers."""
        if not self._entry:
            return []
        from .audio_routing import broadcast_target
        group = self._setting("broadcast_group", "") or None
        chosen = self._setting("announcement_speakers", None)
        return broadcast_target(self.hass, broadcast_group=group, announcement_speakers=chosen)

    async def async_start(self) -> None:
        """Start watching: state changes, a minute timer for durations and a
        five minute rescan that picks up sensors paired after start up."""
        if self._active:
            return
        self._active = True
        self._entity_cache = self._collect_entity_ids()
        self._refresh_subscription()
        for handler, every in ((self._check_durations, timedelta(seconds=60)),
                               (self._refresh_entities, timedelta(minutes=5))):
            self._unsubs.append(async_track_time_interval(self.hass, handler, every))
        _LOGGER.info("Nova Sentinel active, watching %d entities (auto-enroll enabled)",
                     len(self._entity_cache))

    def _refresh_subscription(self) -> None:
        """Listen for state changes on the current entity list.

        Preserved quirk: an earlier listener is never taken down here. It is
        kept in _unsubs and only removed by async_stop, so after a rescan that
        changed the list the old listeners still fire alongside the new one.
        """
        if not self._entity_cache:
            return
        self._unsubs.append(async_track_state_change_event(
            self.hass, self._entity_cache, self._handle_state_change))

    @callback
    def _refresh_entities(self, now) -> None:  # noqa: ARG002
        """Rescan, so newly paired sensors join without a restart."""
        fresh = self._collect_entity_ids()
        joined = set(fresh).difference(self._entity_cache)
        left = set(self._entity_cache).difference(fresh)
        if not (joined or left):
            return
        self._entity_cache = fresh
        self._refresh_subscription()
        if joined:
            _LOGGER.info("Nova Sentinel: enrolled %d new entities: %s",
                         len(joined), ", ".join(list(joined)[:5]))
        if left:
            _LOGGER.info("Nova Sentinel: de-enrolled %d entities", len(left))

    async def async_stop(self) -> None:
        """Remove every listener."""
        listeners, self._unsubs[:] = list(self._unsubs), []
        for remove in listeners:
            try:
                remove()
            except Exception:  # pylint: disable=broad-except
                continue
        self._active = False
        _LOGGER.info("Nova Sentinel stopped.")

    @callback
    def _handle_state_change(self, event) -> None:
        """Runs in the event loop: start or clear each rule's clock, and fire
        instant time window rules."""
        entity_id: str = event.data["entity_id"]
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        value = new_state.state
        now = dt_util.utcnow().replace(tzinfo=None)
        # An entry sensor returning from unknown or unavailable was not opened
        # by anyone: it fires nothing instant, though duration rules still
        # count its state.
        from .cognitive.evaluators import is_entry_state_recovery
        old_state = event.data.get("old_state")
        attributes = getattr(new_state, "attributes", None) or {}
        recovery = is_entry_state_recovery(
            entity_id, attributes.get("device_class") or "",
            "unknown" if old_state is None else old_state.state, value)
        for rule in self._rules:
            key = f"{entity_id}:{rule['id']}"
            alerting = (self._entity_matches_rule(entity_id, rule)
                        and value in _watched_states(entity_id, rule))
            if not alerting:
                self._state_start.pop(key, None)
                self._last_announced.pop(key, None)
                continue
            self._state_start.setdefault(key, now)
            instant = "time_window" in rule and "for_minutes" not in rule
            if instant and not recovery and self._in_time_window(rule["time_window"]):
                self.hass.async_create_task(self._announce_rule(entity_id, rule, minutes=0))

    @callback
    def _check_durations(self, now) -> None:  # noqa: ARG002
        """Every minute: announce each entity held past its rule's limit, and
        again every further limit while it stays that way.

        The reported minutes always run from the first moment the rule matched;
        only _last_announced moves on each announcement. A rule with
        skip_if_outdoor_above_c rests while it is milder than that outside,
        and the temperature is read at most once a tick.
        """
        excluded = _exclusion_check()
        clock = dt_util.utcnow().replace(tzinfo=None)
        outdoor = []
        for rule in self._rules:
            limit = rule.get("for_minutes")
            if not limit:
                continue
            mild_above = rule.get("skip_if_outdoor_above_c")
            if mild_above is not None:
                if not outdoor:
                    outdoor.append(self._outdoor_temp_c())
                if outdoor[0] is not None and outdoor[0] > mild_above:
                    continue
            for entity_id in self._entity_cache:
                if excluded(self.hass, entity_id):
                    continue
                key = f"{entity_id}:{rule['id']}"
                began = self._state_start.get(key)
                if began is None:
                    continue
                held = (clock - began).total_seconds() / 60
                if held < limit:
                    continue
                previous = self._last_announced.get(key)
                gap = held if not previous else (clock - previous).total_seconds() / 60
                if gap < limit:
                    continue
                self.hass.async_create_task(
                    self._announce_rule(entity_id, rule, minutes=int(held)))
                self._last_announced[key] = clock

    def _outdoor_temp_c(self) -> float | None:
        """The outside temperature in Celsius from cognitive_core's discovery,
        or None when there is no source."""
        try:
            from .cognitive_core import discover_outdoor_temp
            reading = discover_outdoor_temp(self.hass)
        except Exception:
            return None
        if reading is None:
            return None
        return _to_celsius(*reading)

    def _live_honorific(self) -> str:
        """Who is home right now decides the honorific, so it is read at alert
        time; the start up value is used only when that lookup fails."""
        try:
            from . import honorific as honorific_mod
            return honorific_mod.effective_honorific(self.hass)
        except Exception:
            return self._honorific

    def _switched_off(self, rule: dict, entity_id: str) -> bool:
        """The panel toggles, read live: announcements, sentinel, then the
        rule's own entry in disabled_sentinel_rules."""
        for toggle in ("announcements_enabled", "sentinel_enabled"):
            if not bool(self._setting(toggle, True)):
                _LOGGER.debug("Sentinel: %s is off, skipping rule %s for %s",
                              toggle, rule.get("id"), entity_id)
                return True
        try:
            off = rule.get("id") in _rule_ids(self._setting("disabled_sentinel_rules", "[]"))
        except Exception:
            return False
        if off:
            _LOGGER.debug("Sentinel: rule %s is individually disabled", rule.get("id"))
        return off

    def _household_asleep(self) -> bool:
        """Quiet hours by code, never by a model or a remembered fact. A failed
        check means speak."""
        try:
            from . import sleep_detection
            asleep, _why = sleep_detection.is_sleeping(
                self.hass,
                bedroom_area_ids=self._setting("bedroom_areas", []) or [],
                quiet_start=self._setting("observer_quiet_start", "22:00"),
                quiet_end=self._setting("observer_quiet_end", "07:00"))
        except Exception as exc:
            _LOGGER.debug("Sentinel: quiet hours check failed, speaking anyway: %s", exc)
            return False
        return asleep

    async def _announce_rule(self, entity_id: str, rule: dict, minutes: int) -> None:
        # An excluded entity is silent at once, even with a clock running.
        if _excluded_now(self.hass, entity_id):
            return
        if self._entry and self._switched_off(rule, entity_id):
            return
        # Only a rule marked urgent_security (no default rule is) may speak
        # while the house sleeps. The phone push goes out regardless.
        urgent = bool(rule.get("urgent_security", False))
        asleep = False if urgent else self._household_asleep()

        state = self.hass.states.get(entity_id)
        name = state.attributes.get("friendly_name", entity_id) if state else entity_id
        honorific = self._live_honorific()
        if "message" in rule:
            text = _fill(rule["message"], honorific, name, minutes)
        else:
            text = await self._groq_line(entity_id, name, rule, minutes)

        save_sentinel_event(entity_id, rule["id"], text)
        # Kept in the chat history only when it will be spoken, so a held back
        # overnight alert never reads later as something Nova already said.
        if not asleep:
            save_message("assistant", f"[Sentinel] {text}", device_id="sentinel")
        # delivery.py decides whether it is spoken (mute, shush, quiet hours,
        # sleep, notifications only); it always reaches the phones, and only
        # the phones when sentinel has no speaker.
        from . import delivery
        speakers = tuple(self._speakers() or ())
        request = delivery.DeliveryRequest(
            source="sentinel", message=text, entity_id=entity_id,
            category=rule.get("id", "sentinel"), speakers=speakers,
            phone_only=not speakers, tts_entity=self._tts_entity() or "",
            tts_context="sentinel", push=delivery.PUSH_ALWAYS, title="Nova",
            quiet_ok=urgent, sleep_ok=urgent, record_kind="left_open",
            record_facts={"rule": rule.get("id"), "minutes": minutes},
            record_assessment="left open past its limit")
        await delivery.async_deliver(self.hass, request)

    async def _groq_line(self, entity_id: str, friendly_name: str, rule: dict,
                         minutes: int) -> str:
        """Have the model word an alert for a rule without a message."""
        honorific = self._live_honorific()
        who = honorific or "the household"
        held_state = rule.get("state", "unknown")
        prompt = ("Generate one concise Nova alert (under 25 words) telling "
                  f"{who} that '{friendly_name}' has been '{held_state}' for "
                  f"{minutes} minutes. Speak as Nova — calm, precise, one dry "
                  "remark if warranted.")
        task = ("You are generating a proactive sentinel alert. Your prime directive "
                "guides what deserves flagging — protect, steward, anticipate. "
                "Deliver the alert directly, no preamble.")
        try:
            system = build_system_prompt(self.hass, honorific, task)
            from . import output_language
            system = output_language.with_language(self.hass, system)
            from .providers.activity import execute_chat
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": prompt}]
            reply = await execute_chat(self.hass, self._groq, messages, role="sentinel",
                                       data_category="text", max_tokens=60, temperature=0.7)
            return reply.text.strip()
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning("Nova Sentinel LLM error: %s", exc)
        lead = f"{honorific}, " if honorific else ""
        return f"{lead}{friendly_name} requires your attention."

    def _collect_entity_ids(self) -> list[str]:
        """Every entity a rule covers: its named entity, or each state whose
        domain and device class fit one of its variants."""
        is_excluded = _exclusion_check()
        found: set[str] = set()
        for rule in self._rules:
            named = rule.get("entity_id")
            if named:
                if not is_excluded(self.hass, named):
                    found.add(named)
                continue
            locks_only = rule.get("requires_security_lock", False)
            for variant in _variants(rule):
                for state in self.hass.states.async_all():
                    eid = state.entity_id
                    if not self._variant_fits(variant, eid, state):
                        continue
                    if locks_only and not _is_security_relevant_lock(self.hass, eid):
                        continue
                    if not is_excluded(self.hass, eid):
                        found.add(eid)
        return list(found)

    def _variant_fits(self, variant: dict, entity_id: str, state) -> bool:
        """Domain and device class test for one rule variant; a missing state
        passes the device class test."""
        domain = variant.get("domain")
        if domain and not entity_id.startswith(f"{domain}."):
            return False
        wanted = variant.get("device_class")
        if wanted and state is not None:
            return state.attributes.get("device_class") == wanted
        return True

    def _entity_matches_rule(self, entity_id: str, rule: dict) -> bool:
        """Whether the entity fits the rule or any of its `also` variants."""
        named = rule.get("entity_id")
        if named and named != entity_id:
            return False
        if rule.get("requires_security_lock") and not _is_security_relevant_lock(
                self.hass, entity_id):
            return False
        state = self.hass.states.get(entity_id)
        return any(self._variant_fits(v, entity_id, state) for v in _variants(rule))

    def _in_time_window(self, window: dict) -> bool:
        """Local clock inside the window; a start after the end crosses midnight."""
        clock = datetime.now().strftime("%H:%M")
        start, end = window["start"], window["end"]
        if start > end:
            return clock >= start or clock <= end
        return start <= clock <= end
