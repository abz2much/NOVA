"""CAP (Common Alerting Protocol) fetching, parsing and area matching (8.8.0).

Used two ways: by hazard_met_eireann.py for Met Éireann's own CAP files, and
on its own for a custom CAP feed the user sets (hazard_cap_url), which is how
the monitor can work outside Ireland. A feed URL may be one CAP XML document
or an Atom or RSS index that links to CAP documents.

Safety rules, all on the fetch and parse side:
  - https only, and every hop (redirects included) passes the same
    destination checks the AI endpoints use: no link-local or cloud metadata
    address, no user:pass@ in the URL;
  - at most 50 linked documents, 2 MB each, 20 seconds each;
  - XML containing a DOCTYPE or an ENTITY declaration is refused outright,
    so no entity can be expanded (internal or external). Standard library
    parser only.

A fetch or parse failure is never "no alerts": collect() reports whether
the list it returns is complete, and only a complete list may cancel
anything (hazard_warnings.reconcile).
"""
from __future__ import annotations

import asyncio
import logging
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

_LOGGER = logging.getLogger(__name__)

MAX_LINKS = 50
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 20
MAX_REDIRECTS = 3
SOURCE = "cap"
SOURCE_LABEL = "Custom CAP feed"

# CAP severity → Nova level. Minor (and Unknown) are ignored.
SEVERITY_LEVEL = {"moderate": "yellow", "severe": "orange", "extreme": "red"}
_AWARENESS_LEVELS = {"yellow", "orange", "red"}


class FetchError(Exception):
    """A feed could not be fetched safely. Never means "no alerts"."""


# ── fetching ─────────────────────────────────────────────────────────────────

def _check_destination(url: str) -> None:
    """The AI endpoints' destination policy (resolving the host), plus https
    only. Raises FetchError. Runs in the executor (DNS)."""
    from .providers.destinations import check_url
    from .providers.errors import ProviderError
    if urlparse(url).scheme != "https":
        raise FetchError("only https feeds are allowed")
    try:
        check_url(url, resolve=True)
    except ProviderError as exc:
        raise FetchError("the feed address is not an allowed destination") from exc


async def fetch_bytes(hass, url: str, *, user_agent: str,
                      max_bytes: int = MAX_BYTES, timeout: float = TIMEOUT_S) -> bytes:
    """GET ``url`` with every redirect re-checked, a size cap and a time
    limit. Raises FetchError on anything but a complete 200 body."""
    try:
        return await asyncio.wait_for(
            _fetch(hass, url, user_agent=user_agent, max_bytes=max_bytes, timeout=timeout),
            timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise FetchError("the feed took too long") from exc


async def _fetch(hass, url: str, *, user_agent: str, max_bytes: int, timeout: float) -> bytes:
    import aiohttp
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    session = async_get_clientsession(hass)
    headers = {"User-Agent": user_agent,
               "Accept": "application/cap+xml, application/xml, text/xml, application/json"}
    for _hop in range(MAX_REDIRECTS + 1):
        await hass.async_add_executor_job(_check_destination, url)
        try:
            async with session.get(url, headers=headers, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise FetchError("a redirect with no location")
                    url = urljoin(url, location)
                    continue
                if resp.status != 200:
                    raise FetchError(f"HTTP {resp.status}")
                data = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    data += chunk
                    if len(data) > max_bytes:
                        raise FetchError("the feed is too large")
                return bytes(data)
        except FetchError:
            raise
        except asyncio.TimeoutError:
            raise
        except Exception as exc:
            raise FetchError(type(exc).__name__) from exc
    raise FetchError("too many redirects")


# ── XML ──────────────────────────────────────────────────────────────────────

def parse_xml(raw: bytes) -> ET.Element:
    """Parse XML, refusing any DOCTYPE or ENTITY declaration. Raises
    ValueError for refused or malformed XML."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("not bytes")
    lowered = bytes(raw).lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("XML with a DOCTYPE or ENTITY declaration is refused")
    try:
        return ET.fromstring(bytes(raw).lstrip(b"\xef\xbb\xbf").lstrip())
    except ET.ParseError as exc:
        raise ValueError("malformed XML") from exc


def _local(tag) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _child(el, name: str):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el, name: str) -> list:
    return [c for c in el if _local(c.tag) == name]


def _text(el, name: str) -> str:
    c = _child(el, name) if el is not None else None
    return (c.text or "").strip() if c is not None and c.text else ""


def parse_index(root) -> Optional[list[str]]:
    """The CAP links of an RSS or Atom index, or None when ``root`` is not
    an index. Links are kept in feed order, duplicates removed."""
    kind = _local(root.tag)
    links: list[str] = []
    if kind == "rss":
        channel = _child(root, "channel")
        for item in _children(channel, "item") if channel is not None else []:
            link = _text(item, "link") or _text(item, "guid")
            if link:
                links.append(link)
    elif kind == "feed":
        for entry in _children(root, "entry"):
            chosen = ""
            for ln in _children(entry, "link"):
                href = (ln.get("href") or "").strip()
                if not href:
                    continue
                typ = (ln.get("type") or "").lower()
                if "cap" in typ or "xml" in typ:
                    chosen = href
                    break
                chosen = chosen or href
            if chosen:
                links.append(chosen)
    else:
        return None
    return list(dict.fromkeys(links))


def _references(text: str) -> list[str]:
    """CAP references: space separated "sender,identifier,sent" triples."""
    out = []
    for triple in (text or "").split():
        parts = triple.split(",")
        if len(parts) >= 2 and parts[1]:
            out.append(parts[1])
    return out


def _parse_area(area) -> dict:
    polygons, circles, geocodes = [], [], []
    for poly in _children(area, "polygon"):
        pts = []
        for pair in (poly.text or "").split():
            try:
                la, lo = pair.split(",")[:2]
                pts.append((float(la), float(lo)))
            except ValueError:
                pts = []
                break
        if len(pts) >= 3:
            polygons.append(pts)
    for circ in _children(area, "circle"):
        try:
            centre, radius = (circ.text or "").split()
            la, lo = centre.split(",")[:2]
            circles.append((float(la), float(lo), float(radius)))
        except ValueError:
            continue
    for gc in _children(area, "geocode"):
        value = _text(gc, "value")
        if value:
            geocodes.append((_text(gc, "valueName"), value))
    return {"desc": _text(area, "areaDesc"), "polygons": polygons,
            "circles": circles, "geocodes": geocodes}


def _pick_info(infos: list, lang: str):
    """The info block in ``lang`` (exact, then by language prefix), else the
    first one."""
    if not infos:
        return None
    want = (lang or "").lower()
    if want:
        for info in infos:
            if _text(info, "language").lower() == want:
                return info
        prefix = want.split("-")[0]
        for info in infos:
            if _text(info, "language").lower().split("-")[0] == prefix:
                return info
    return infos[0]


def parse_alert(root, lang: str = "") -> Optional[dict]:
    """A CAP <alert> as a plain dict, or None when ``root`` is not one."""
    if _local(root.tag) != "alert":
        return None
    info = _pick_info(_children(root, "info"), lang)
    params: dict[str, str] = {}
    areas: list[dict] = []
    if info is not None:
        for prm in _children(info, "parameter"):
            name = _text(prm, "valueName")
            if name:
                params[name] = _text(prm, "value")
        areas = [_parse_area(a) for a in _children(info, "area")]
    return {
        "identifier": _text(root, "identifier"),
        "sender": _text(root, "sender"),
        "sent": _text(root, "sent"),
        "status": _text(root, "status"),
        "msg_type": _text(root, "msgType"),
        "references": _references(_text(root, "references")),
        "event": _text(info, "event") if info is not None else "",
        "severity": _text(info, "severity") if info is not None else "",
        "onset": (_text(info, "onset") or _text(info, "effective")) if info is not None else "",
        "expires": _text(info, "expires") if info is not None else "",
        "headline": _text(info, "headline") if info is not None else "",
        "description": _text(info, "description") if info is not None else "",
        "params": params,
        "areas": areas,
    }


def level_of(alert: dict) -> Optional[str]:
    """yellow, orange or red from the awareness_level parameter when present
    (Meteoalarm style "3; orange; Severe"), else from CAP severity. None for
    Minor, green or unknown."""
    awareness = alert.get("params", {}).get("awareness_level", "")
    if awareness:
        parts = [p.strip().lower() for p in awareness.split(";")]
        for p in parts:
            if p in _AWARENESS_LEVELS:
                return p
        if "green" in parts or "minor" in parts:
            return None
    return SEVERITY_LEVEL.get(str(alert.get("severity", "")).strip().lower())


# ── location matching ────────────────────────────────────────────────────────

def point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray casting on (lat, lon) pairs. Points on an edge may go either way."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]
        yj, xj = polygon[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def matched_areas(alert: dict, home: Optional[tuple[float, float]],
                  area_codes: list[str], area_names: list[str]) -> list[str]:
    """The areas of ``alert`` that cover the home, checked in this order: a
    polygon containing the home point, a circle containing it, a geocode
    value in the user's area codes, an areaDesc equal to one of the user's
    area names. Empty means the alert is not for this home."""
    codes = {str(c).strip().lower() for c in area_codes if str(c).strip()}
    names = {str(n).strip().lower() for n in area_names if str(n).strip()}
    out = []
    for area in alert.get("areas", []):
        hit = False
        if home is not None:
            hit = any(point_in_polygon(home[0], home[1], p) for p in area["polygons"])
            hit = hit or any(_km(home[0], home[1], c[0], c[1]) <= c[2] for c in area["circles"])
        hit = hit or any(v.strip().lower() in codes for _n, v in area["geocodes"])
        hit = hit or (area["desc"].strip().lower() in names if area["desc"] else False)
        if hit:
            out.append(area["desc"] or "your area")
    return out


# ── collecting a feed ────────────────────────────────────────────────────────

async def collect(hass, url: str, *, lang: str, user_agent: str) -> tuple[bool, list[dict]]:
    """Fetch ``url`` (one CAP document, or an RSS or Atom index of them) and
    return (complete, alerts). complete is False when the feed or any linked
    document could not be fetched or parsed, or there were more than
    MAX_LINKS links: such a list may announce, but never cancel."""
    try:
        root = parse_xml(await fetch_bytes(hass, url, user_agent=user_agent))
    except (FetchError, ValueError) as exc:
        _LOGGER.debug("hazard CAP: feed unusable: %s", exc)
        return False, []
    single = parse_alert(root, lang)
    if single is not None:
        return True, [single]
    links = parse_index(root)
    if links is None:
        _LOGGER.debug("hazard CAP: the feed is neither a CAP alert nor an index")
        return False, []
    complete = len(links) <= MAX_LINKS
    alerts = []
    for link in links[:MAX_LINKS]:
        try:
            alert = parse_alert(parse_xml(await fetch_bytes(
                hass, urljoin(url, link), user_agent=user_agent)), lang)
        except (FetchError, ValueError) as exc:
            _LOGGER.debug("hazard CAP: a linked alert is unusable: %s", exc)
            complete = False
            continue
        if alert is None:
            complete = False
            continue
        alerts.append(alert)
    return complete, alerts


def parse_time(text: str) -> Optional[datetime]:
    """An ISO 8601 / CAP time as an aware UTC datetime, or None."""
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_warnings(alerts: list[dict], *, home, area_codes: list[str],
                area_names: list[str]) -> list[dict]:
    """Custom CAP alerts as warnings: status Actual only, level yellow or
    above, an area that matches the home. A Cancel is passed on (for its
    references) whatever its area."""
    out = []
    for a in alerts:
        if a.get("status") != "Actual":
            continue
        msg_type = a.get("msg_type") or "Alert"
        if msg_type == "Cancel":
            out.append({"id": a["identifier"], "msg_type": "Cancel",
                        "refs": a["references"], "source": SOURCE})
            continue
        level = level_of(a)
        if level is None:
            continue
        areas = matched_areas(a, home, area_codes, area_names)
        if not areas:
            continue
        out.append({
            "id": a["identifier"], "msg_type": msg_type, "refs": a["references"],
            "source": SOURCE, "source_label": SOURCE_LABEL,
            "type": a.get("event") or "warning", "level": level,
            "onset": a.get("onset", ""), "expiry": a.get("expires", ""),
            "headline": a.get("headline", ""), "description": a.get("description", ""),
            "areas": areas, "area_keys": [s.lower() for s in areas],
        })
    return out
