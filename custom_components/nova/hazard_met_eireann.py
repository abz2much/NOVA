"""Met Éireann weather warnings for Irish homes (8.8.0).

Source, per Met Éireann's "Warning RSS/CAP/JSON Description" (revised June
2020):
  - the RSS index https://www.met.ie/warningsxml/rss.xml, whose items link to
    one CAP XML file per warning. "This RSS/Atom feed always contains a list
    of valid warnings. If an unexpired warning exists but is not in the feed,
    it is deemed to be cancelled." This is the primary source.
  - the JSON file https://www.met.ie/Open_Data/json/warning_IRELAND.json,
    used only when the RSS index cannot be read. (When checked on
    2026-10-06 it answered 404 while the RSS index was live.) Its capId is
    the CAP identifier, so a warning keeps its id whichever source saw it.

Licence (Creative Commons 4.0, per the same document): "The headline and
description must not be altered", and Met Éireann is credited. Nova shows
both as published (the description's HTML is shown as plain text, every word
kept) and never shortens or rewords them.

Counties are the 26 FIPS codes in the document's region table. Northern
Ireland counties are not in it, nor in the example files. Sea areas (EI805
to EI825, EMMA_ID) are not handled in this release; a warning for sea areas
only never matches a county.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Optional

from . import hazard_cap

_LOGGER = logging.getLogger(__name__)

RSS_URL = "https://www.met.ie/warningsxml/rss.xml"
JSON_URL = "https://www.met.ie/Open_Data/json/warning_IRELAND.json"
SOURCE = "met_eireann"
SOURCE_LABEL = "Met Éireann"

# FIPS code → (name, approximate centre latitude, longitude). Codes and
# names from Met Éireann's region table (and the 26 codes of the "Weather
# Advisory for Ireland" example). Centres are approximate geographic
# centres, accurate to some kilometres; they only pick the default county,
# which the panel shows so it can be confirmed or changed.
COUNTIES: dict[str, tuple[str, float, float]] = {
    "EI01": ("Carlow", 52.72, -6.82),
    "EI02": ("Cavan", 53.99, -7.36),
    "EI03": ("Clare", 52.86, -8.98),
    "EI04": ("Cork", 51.95, -8.75),
    "EI06": ("Donegal", 54.92, -7.95),
    "EI07": ("Dublin", 53.38, -6.27),
    "EI10": ("Galway", 53.35, -8.75),
    "EI11": ("Kerry", 52.13, -9.62),
    "EI12": ("Kildare", 53.20, -6.80),
    "EI13": ("Kilkenny", 52.58, -7.22),
    "EI14": ("Leitrim", 54.12, -8.00),
    "EI15": ("Laois", 52.98, -7.35),
    "EI16": ("Limerick", 52.52, -8.75),
    "EI18": ("Longford", 53.73, -7.72),
    "EI19": ("Louth", 53.92, -6.48),
    "EI20": ("Mayo", 53.88, -9.35),
    "EI21": ("Meath", 53.62, -6.65),
    "EI22": ("Monaghan", 54.13, -6.92),
    "EI23": ("Offaly", 53.22, -7.70),
    "EI24": ("Roscommon", 53.75, -8.25),
    "EI25": ("Sligo", 54.15, -8.60),
    "EI26": ("Tipperary", 52.60, -7.85),
    "EI27": ("Waterford", 52.20, -7.60),
    "EI29": ("Westmeath", 53.53, -7.45),
    "EI30": ("Wexford", 52.45, -6.58),
    "EI31": ("Wicklow", 52.98, -6.40),
}

# awareness_type numbers that are warnings (per the document; 7, 8, 9, 12
# and 13 are listed but "not used by Met Eireann"). Advisory (22) and blight
# (21, seen in the CAP examples) are not warnings and are ignored.
WARNING_TYPE_NUMBERS = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13}
_NOT_WARNING_TYPES = {"advisory", "blight"}

# The island of Ireland, generously (it includes Northern Ireland, which
# Met Éireann does not cover; see in_ireland).
_IRELAND_BOX = (51.3, 55.5, -10.7, -5.9)


def county_name(code: str) -> str:
    entry = COUNTIES.get(str(code).upper())
    return entry[0] if entry else str(code)


def nearest_county(lat: float, lon: float) -> str:
    """The county whose centre is nearest the point."""
    def dist(code: str) -> float:
        _n, clat, clon = COUNTIES[code]
        x = math.radians(clon - lon) * math.cos(math.radians((clat + lat) / 2))
        y = math.radians(clat - lat)
        return x * x + y * y
    return min(COUNTIES, key=dist)


def in_ireland(country: Optional[str], home: Optional[tuple[float, float]]) -> bool:
    """Whether the home is an Irish one for the region defaults: Home
    Assistant's country is IE, or (with no country set) the home point is on
    the island. A home with another country set (GB in Northern Ireland,
    say) is not, even inside the box."""
    c = str(country or "").strip().upper()
    if c:
        return c == "IE"
    if home is None:
        return False
    lat, lon = home
    return _IRELAND_BOX[0] <= lat <= _IRELAND_BOX[1] and _IRELAND_BOX[2] <= lon <= _IRELAND_BOX[3]


def parse_county_list(value) -> list[str]:
    """hazard_counties as stored (a JSON list string, or a list) → valid
    codes, de-duplicated. Anything unreadable is an empty list (auto)."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else []
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(c).upper() for c in value if str(c).upper() in COUNTIES))


def chosen_counties(saved, home: Optional[tuple[float, float]]) -> list[str]:
    """The user's counties, else the one nearest home, else none."""
    codes = parse_county_list(saved)
    if codes:
        return codes
    if home is not None:
        return [nearest_county(*home)]
    return []


def _type_from_awareness(text: str) -> tuple[Optional[int], str]:
    """'1; Wind' → (1, 'Wind')."""
    parts = [p.strip() for p in str(text or "").split(";")]
    try:
        number = int(parts[0])
    except (ValueError, IndexError):
        number = None
    return number, (parts[1] if len(parts) > 1 else "")


def _warning(*, wid, refs, msg_type, wtype, level, onset, expiry, headline,
             description, matched: list[str]) -> dict:
    names = [county_name(c) for c in matched]
    return {
        "id": wid, "msg_type": msg_type, "refs": refs,
        "source": SOURCE, "source_label": SOURCE_LABEL,
        "type": wtype, "level": level, "onset": onset, "expiry": expiry,
        "headline": headline, "description": description,
        "areas": names, "area_keys": list(matched),
    }


def from_cap(alerts: list[dict], counties: list[str]) -> list[dict]:
    """Met Éireann CAP alerts → warnings for ``counties``. Only status
    Actual, only warning types, level yellow or above, and a FIPS county in
    the list. A Cancel is passed on for its references."""
    wanted = set(counties)
    out = []
    for a in alerts:
        if a.get("status") != "Actual":
            continue
        msg_type = a.get("msg_type") or "Alert"
        if msg_type == "Cancel":
            out.append({"id": a["identifier"], "msg_type": "Cancel",
                        "refs": a["references"], "source": SOURCE})
            continue
        number, wtype = _type_from_awareness(a.get("params", {}).get("awareness_type", ""))
        wtype = wtype or a.get("event", "")
        if number is not None and number not in WARNING_TYPE_NUMBERS:
            continue
        if wtype.strip().lower() in _NOT_WARNING_TYPES:
            continue
        level = hazard_cap.level_of(a)
        if level is None:
            continue
        fips = [v.upper() for area in a.get("areas", []) for name, v in area["geocodes"]
                if name.upper() == "FIPS"]
        matched = [c for c in dict.fromkeys(fips) if c in wanted]
        if not matched:
            continue
        out.append(_warning(
            wid=a["identifier"], refs=a["references"], msg_type=msg_type,
            wtype=wtype, level=level, onset=a.get("onset", ""), expiry=a.get("expires", ""),
            headline=a.get("headline", ""), description=a.get("description", ""),
            matched=matched))
    return out


def from_json(data, counties: list[str]) -> Optional[list[dict]]:
    """The documented JSON list → warnings for ``counties``, or None when
    ``data`` is not that list. Only status "Warning" (not "Advisory")."""
    if not isinstance(data, list):
        return None
    wanted = set(counties)
    out = []
    for w in data:
        if not isinstance(w, dict) or str(w.get("status", "")) != "Warning":
            continue
        level = str(w.get("level", "")).lower()
        if level not in ("yellow", "orange", "red"):
            continue
        regions = [str(r).upper() for r in (w.get("regions") or []) if isinstance(r, str)]
        matched = [c for c in dict.fromkeys(regions) if c in wanted]
        if not matched or not w.get("capId"):
            continue
        out.append(_warning(
            wid=str(w["capId"]), refs=[], msg_type="Alert", wtype=str(w.get("type", "")),
            level=level, onset=str(w.get("onset", "")), expiry=str(w.get("expiry", "")),
            headline=str(w.get("headline", "")), description=str(w.get("description", "")),
            matched=matched))
    return out


async def fetch(hass, counties: list[str], *, lang: str,
                user_agent: str) -> tuple[bool, list[dict], str]:
    """(complete, warnings, via). The RSS index and its CAP files first; the
    JSON file only when the index cannot be read. A failure of both is
    (False, [], "none"): never an empty list that could cancel."""
    complete, alerts = await hazard_cap.collect(hass, RSS_URL, lang=lang, user_agent=user_agent)
    if complete or alerts:
        return complete, from_cap(alerts, counties), "rss"
    try:
        raw = await hazard_cap.fetch_bytes(hass, JSON_URL, user_agent=user_agent)
        parsed = from_json(json.loads(raw.decode("utf-8-sig")), counties)
    except (hazard_cap.FetchError, ValueError) as exc:
        _LOGGER.debug("hazard Met Éireann: JSON fallback unusable: %s", exc)
        parsed = None
    if parsed is None:
        return False, [], "none"
    return True, parsed, "json"
