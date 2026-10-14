"""
Nova — the nova.briefing service.

Collects what is going on in the home (the date and time, weather, who is in
and out, recent sentinel events, anything open or unlocked, today's calendar,
heavy power use, nearby hazards and camera sightings), asks the language
model to turn it into a short spoken briefing, and delivers it. When the
model says nothing, or says something the collected facts do not support,
Nova reads the facts out plainly instead.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from homeassistant.core import HomeAssistant, ServiceCall

from . import paths
from .const import BRIEFING_GROUNDING_RULE
from .database import save_message
from .directive_helper import build_system_prompt
from .locale_format import format_date
from .persistence import sqlite as _store
from .presence import get_presence_summary

_LOGGER = logging.getLogger(__name__)

BRIEFING_MODEL = "openai/gpt-oss-120b"

# Tests point this at a scratch file; None means the normal location from
# paths.py. Only the sentinel_events table is read here.
CONVERSATIONS_DB: Optional[str] = None

_POWER_WATCH_LEVEL = 500     # a reading above this is worth mentioning
_EVENT_LIMIT = 10
_OPENING_CLASSES = frozenset({"door", "window", "garage_door"})
_SERIOUS_LEVELS = ("orange", "red")


def _conversations_db() -> str:
    return CONVERSATIONS_DB or paths.conversations_db()


def _time_greeting() -> str:
    hour = datetime.now().hour
    for last_hour, words in ((11, "Good morning"), (16, "Good afternoon"),
                             (21, "Good evening")):
        if hour <= last_hour:
            return words
    return "Still awake"


def _label(state) -> str:
    return state.attributes.get("friendly_name", state.entity_id)


def _gather_weather(hass: HomeAssistant) -> str:
    """Condition, current temperature and today's range from the first
    weather entity, joined into one line."""
    states = hass.states.async_all("weather")
    if not states:
        return ""
    first = states[0]
    info = first.attributes
    unit = info.get("temperature_unit", "°")
    bits = [f"{first.state}"]
    if info.get("temperature") is not None:
        bits.append(f"currently {info.get('temperature')}{unit}")
    days = info.get("forecast") or []
    today = days[0] if days else {}
    top, bottom = today.get("temperature"), today.get("templow")
    if top is not None and bottom is not None:
        bits.append(f"high {top}{unit}, low {bottom}{unit}")
    return ", ".join(bits)


def _gather_open_things(hass: HomeAssistant) -> list[str]:
    """Open doors, windows and garage doors, then unlocked locks. Locks the
    lockdown setting exempts (a thermostat keypad lock, say) are left out."""
    found = [f"{_label(s)} is open"
             for s in hass.states.async_all("binary_sensor")
             if s.state == "on" and s.attributes.get("device_class") in _OPENING_CLASSES]
    from .cognitive_core import _lockdown_exempt_locks
    skip = _lockdown_exempt_locks()
    found.extend(f"{_label(s)} is unlocked"
                 for s in hass.states.async_all("lock")
                 if s.state == "unlocked" and s.entity_id not in skip)
    return found


def _gather_calendar(hass: HomeAssistant) -> list[str]:
    """The current event of each calendar that is on, with its start clock."""
    found = []
    for cal in hass.states.async_all("calendar"):
        title = cal.attributes.get("message")
        begins = cal.attributes.get("start_time")
        if cal.state == "on" and title and begins:
            found.append(f"{title} at {begins[11:16]}")
    return found


def _gather_energy_anomalies(hass: HomeAssistant) -> list[str]:
    """Power sensors reading above the watch level."""
    found = []
    for sensor in hass.states.async_all("sensor"):
        if sensor.attributes.get("device_class") != "power":
            continue
        try:
            reading = float(sensor.state)
        except (TypeError, ValueError):
            continue
        if reading <= _POWER_WATCH_LEVEL:
            continue
        unit = sensor.attributes.get("unit_of_measurement", "W")
        found.append(f"{_label(sensor)} is drawing {reading:.0f}{unit}")
    return found


def _local_clock(stamp: str) -> str:
    """HH:MM in local time for a naive UTC timestamp. Text that does not
    parse keeps its own clock characters."""
    try:
        moment = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return stamp[11:16]
    return moment.astimezone().strftime("%H:%M")


def _gather_overnight_events(hass: HomeAssistant, hours: int = 12) -> list[str]:
    """Up to ten sentinel events from the last `hours`, newest first, each
    as "HH:MM: detail" in local time."""
    db_file = Path(_conversations_db())
    try:
        if not db_file.exists():
            return []
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
        query = ("SELECT timestamp, detail FROM sentinel_events WHERE timestamp > ? "
                 f"ORDER BY timestamp DESC LIMIT {_EVENT_LIMIT}")
        with _store.connect(str(db_file), row_factory=False, mkdir=False,
                            busy_timeout_ms=None) as conn:
            found = conn.execute(query, (cutoff.isoformat(),)).fetchall()
        return [f"{_local_clock(stamp)}: {detail}" for stamp, detail in found]
    except Exception as exc:
        _LOGGER.debug("Nova briefing: could not read sentinel events: %s", exc)
        return []


def _plain_briefing(greeting: str, honorific: str, context_lines: list[str]) -> str:
    """The collected facts read out as they are, opened by the greeting.
    With no honorific (nobody specific to address) the greeting stands
    alone rather than ending in a stray comma."""
    opener = f"{greeting}, {honorific}." if honorific else f"{greeting}."
    facts = [line.strip() for line in context_lines or [] if line and line.strip()]
    body = " ".join(facts) if facts else "Nothing notable to report."
    return f"{opener} {body}"


# Kinds of device the briefing may only mention when the collected facts
# mention that kind too: (label, pattern in the briefing, pattern in the
# facts). The model has borrowed devices from prompt examples before.
_GROUNDED_DEVICE_WORDS = (
    ("garage", re.compile(r"\bgarages?\b"), re.compile(r"garage")),
    ("gate", re.compile(r"\bgates?\b"), re.compile(r"\bgate")),
    ("door", re.compile(r"\bdoors?\b"), re.compile(r"door")),
    ("window", re.compile(r"\bwindows?\b"), re.compile(r"window")),
    ("lock", re.compile(r"\b(?:un)?lock(?:ed|s)?\b"), re.compile(r"lock")),
    ("thermostat", re.compile(r"\b(?:thermostat|heating|setpoint)s?\b"),
     re.compile(r"thermostat|heating|setpoint|climate")),
)
_CLOCK_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _clock_times(text: str) -> set[tuple[int, int]]:
    """Each valid H:MM in the text as (hour mod 12, minute), so a 24 hour
    time and its 12 hour reading match."""
    found = set()
    for hh, mm in _CLOCK_TIME.findall(text):
        hour, minute = int(hh), int(mm)
        if hour < 24 and minute < 60:
            found.add((hour % 12, minute))
    return found


def ungrounded_briefing_fact(text: str, context: str) -> str:
    """The first thing the briefing says that the facts do not support, or ""
    when it all checks out. A device kind is checked first, then any clock
    time missing from the facts (reported as "time H:MM", 12 hour). The
    caller swaps a failing briefing for the plain facts; it is never patched."""
    spoken = (text or "").lower()
    facts = (context or "").lower()
    for label, said_pattern, fact_pattern in _GROUNDED_DEVICE_WORDS:
        if said_pattern.search(spoken) and not fact_pattern.search(facts):
            return label
    unknown = sorted(_clock_times(spoken) - _clock_times(facts))
    if not unknown:
        return ""
    hour, minute = unknown[0]
    return f"time {hour or 12}:{minute:02d}"


def _hazard_feed_bits(hz: dict) -> list[str]:
    bits = []
    for quake in hz.get("earthquakes") or []:
        size = quake.get("mag")
        what = (f"magnitude {size:.1f}" if isinstance(size, (int, float))
                else "an earthquake")
        bits.append(f"{what} quake {quake.get('dist_km')} km away ({quake.get('place')})")
    for alert in hz.get("weather") or []:
        where = f" for {alert.get('area')}" if alert.get("area") else ""
        bits.append(f"{alert.get('severity')} weather alert: {alert.get('event')}{where}")
    for item in hz.get("disasters") or []:
        bits.append(f"{item.get('category')}: {item.get('title')} "
                    f"{item.get('dist_km')} km away")
    return bits


def _warning_line(w: dict) -> str:
    where = ", ".join(w.get("counties") or []) or "your area"
    start, end = w.get("from"), w.get("to")
    span = f" from {start} to {end}" if start and end else ""
    level = str(w.get("level")).capitalize()
    return (f"{w.get('source_label')} {level} {w.get('type')} warning for {where}{span}. "
            f"Headline (quote exactly): {w.get('headline_text')}")


def _hazard_context(hz: dict) -> list[str]:
    """Context lines from a hazard_monitor scan: one line for the quake,
    weather alert and disaster feeds, and one for Orange or Red weather
    warnings with their headline passed on word for word. Nothing when the
    scan is quiet."""
    out: list[str] = []
    bits = _hazard_feed_bits(hz) if hz.get("ok") else []
    if bits:
        out.append("Active hazards nearby: " + "; ".join(bits) + ".")
    serious = [w for w in hz.get("warnings") or [] if w.get("level") in _SERIOUS_LEVELS]
    if serious:
        out.append("Weather warnings in force:\n- "
                   + "\n- ".join(_warning_line(w) for w in serious))
    return out


def _presence_lines(hass: HomeAssistant) -> list[str]:
    people = get_presence_summary(hass)["people"]
    lines = []
    home = [p["name"] for p in people if p["state"] == "home"]
    away = [p["name"] for p in people if p["state"] != "home"]
    if home:
        lines.append(f"At home: {', '.join(home)}.")
    if away:
        lines.append(f"Away: {', '.join(away)}.")
    return lines


async def _collect_context(hass: HomeAssistant, wants, hours: int) -> list[str]:
    """The fact lines for the briefing, in the order they are read."""
    now = datetime.now()
    lines = [f"It is {format_date(now, hass)}, {now.strftime('%-I:%M %p')}."]
    if wants("include_weather"):
        sky = _gather_weather(hass)
        if sky:
            lines.append(f"Weather: {sky}.")
    if wants("include_presence"):
        lines.extend(_presence_lines(hass))
    if wants("include_events"):
        recent = _gather_overnight_events(hass, hours)
        if recent:
            lines.append(f"Events in the last {hours} hours:\n- " + "\n- ".join(recent))
    open_now = _gather_open_things(hass)
    if open_now:
        lines.append(f"Currently open/unlocked: {', '.join(open_now)}.")
    if wants("include_calendar"):
        agenda = _gather_calendar(hass)
        if agenda:
            lines.append(f"Calendar: {', '.join(agenda)}.")
    if wants("include_energy"):
        heavy = _gather_energy_anomalies(hass)
        if heavy:
            lines.append(f"High power draw: {', '.join(heavy)}.")
    if wants("include_hazards"):
        try:
            from . import hazard_monitor
            lines.extend(_hazard_context(await hazard_monitor.scan_now(hass)))
        except Exception:
            pass
    try:
        from .proactive_briefing import get_snapshot_summary
        sightings = get_snapshot_summary(hours=hours)
        if sightings:
            lines.append(sightings)
    except Exception:
        pass
    return lines


def _briefing_task(greeting: str, honorific: str) -> str:
    if honorific:
        audience, opening = f"to {honorific}", f"{greeting}, {honorific}."
    else:
        audience, opening = "to the household", f"{greeting}."
    return (
        f"You are delivering a spoken briefing {audience}. Begin with '{opening}' "
        "Then concisely cover the important items. Under 120 words. "
        "Be efficient — do not list trivia. If nothing is noteworthy, say so briefly. "
        "Your prime directive should inform what you surface — protect, steward, "
        "anticipate. Lead with anything that affects their safety or wellbeing. "
        f"{BRIEFING_GROUNDING_RULE}"
    )


async def _compose(hass, groq_client, system: str, context: str) -> str:
    try:
        from .providers.activity import execute_chat
        reply = await execute_chat(
            hass, groq_client,
            [{"role": "system", "content": system},
             {"role": "user", "content": f"Context:\n{context}"}],
            role="briefing", data_category="text",
            max_tokens=1500,
            temperature=0.2,
        )
    except Exception as exc:
        _LOGGER.error("Nova briefing error: %s", exc)
        return ""
    return (reply.text or "").strip()


async def async_briefing(
    hass: HomeAssistant, call: ServiceCall, groq_client,
    honorific: str, tts_entity: str | None, speakers: list[str],
) -> dict:
    """nova.briefing: collect the facts, have the model voice them, keep a
    copy in the conversation history and, when asked to announce, hand it
    to delivery as a request the person made."""
    options = call.data

    def wants(key: str) -> bool:
        return options.get(key, True)

    hours = int(options.get("hours", 12))
    lines = await _collect_context(hass, wants, hours)
    context = "\n".join(lines)

    greeting = _time_greeting()
    system = build_system_prompt(hass, honorific, _briefing_task(greeting, honorific))
    from . import output_language
    system = output_language.with_language(hass, system)   # spoken, so in Nova's language
    text = await _compose(hass, groq_client, system, context)

    if not text:
        _LOGGER.warning("Nova briefing: empty model output — reading gathered facts instead")
        text = _plain_briefing(greeting, honorific, lines)
    else:
        unsupported = ungrounded_briefing_fact(text, context)
        if unsupported:
            _LOGGER.warning(
                "Nova briefing rejected — %s is not in the gathered facts; "
                "reading the facts instead: %s", unsupported, text[:200])
            text = _plain_briefing(greeting, honorific, lines)

    await hass.async_add_executor_job(save_message, "assistant", f"[Briefing] {text}",
                                      "briefing")

    announced = False
    if options.get("announce", True):
        announced = await _announce(hass, text, tts_entity, speakers)
    return {"success": True, "briefing": text, "context": context,
            "announced": announced}


async def _announce(hass, text: str, tts_entity, speakers) -> bool:
    _LOGGER.info("Nova briefing announce: tts=%s, speakers=%s, text_len=%d, first_50=%s",
                  tts_entity, speakers, len(text), text[:50])
    if not tts_entity:
        _LOGGER.warning("Nova briefing: no TTS entity resolved")
        return False
    if not speakers:
        _LOGGER.warning("Nova briefing: no announcement speakers configured — choose "
                        "them in Settings → Announcement Speakers")
    # The person asked for this briefing, so shush, the Announcements switch
    # and quiet hours do not hold it back. Delivery sends it to phones when
    # the house is asleep, "Notifications only" is on, or there is no speaker.
    from . import delivery
    request = delivery.DeliveryRequest(
        source="briefing", message=text, category="briefing_service",
        requested_by_user=True, quiet_ok=True, speakers=tuple(speakers or ()),
        tts_entity=tts_entity, tts_context="briefing", title="Nova — Briefing")
    outcome = await delivery.async_deliver(hass, request)
    return outcome.spoken
