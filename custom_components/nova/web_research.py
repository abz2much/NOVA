"""The agent's web_research tool: a short, bounded web lookup.

Off until a backend is chosen in Nova's settings. Backends are the DuckDuckGo
Instant Answer API (no key) or the person's own SearXNG. One attempt with a
10 s timeout; failures come back as an error dict, never an exception.
"""
from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

_MAX_ABSTRACT = 1200
_MAX_RELATED = 5
_TIMEOUT = 10
_MAX_RELATED_TEXT = 160

_DDG_ENDPOINT = "https://api.duckduckgo.com/"

_BACKENDS = ("duckduckgo", "searxng")


def _cfg(key: str, default: Any) -> Any:
    try:
        from . import nova_config
        value = nova_config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _squash(text: Any) -> str:
    return " ".join(str(text or "").split())


def _clip(text: Any, limit: int) -> str:
    s = _squash(text)
    if len(s) <= limit:
        return s
    return s[:limit].rsplit(" ", 1)[0] + "…"


def _error(q: str, message: str) -> dict:
    return {"query": q, "error": message}


def _success(q: str, answer: str, source: str, source_name: str,
             related: list[str], backend: str) -> dict:
    return {"query": q, "answer": answer, "source": source,
            "source_name": source_name, "related": related, "backend": backend}


def _shape_ddg(q: str, data: dict) -> dict:
    data = data or {}
    abstract = (data.get("AbstractText") or data.get("Abstract") or "").strip()
    best = (abstract or (data.get("Answer") or "").strip()
            or (data.get("Definition") or "").strip())

    related: list[str] = []
    for topic in (data.get("RelatedTopics") or [])[:2 * _MAX_RELATED]:
        if isinstance(topic, dict) and topic.get("Text"):
            related.append(_clip(topic["Text"], _MAX_RELATED_TEXT))
            if len(related) >= _MAX_RELATED:
                break

    if not best and not related:
        return _error(q, "no results — try rephrasing, or this may need a full web search")
    return _success(
        q,
        _clip(best, _MAX_ABSTRACT) if best else "",
        (data.get("AbstractURL") or data.get("DefinitionURL") or "").strip(),
        (data.get("AbstractSource") or data.get("DefinitionSource") or "").strip(),
        related,
        "duckduckgo",
    )


def _shape_searxng(q: str, data: dict) -> dict:
    data = data or {}
    results = data.get("results") or []
    if not results:
        answers = data.get("answers") or []
        if answers:
            return _success(q, _clip(answers[0], _MAX_ABSTRACT), "", "", [], "searxng")
        return _error(q, "no results")

    top = results[0]
    related = [_clip(r["title"], _MAX_RELATED_TEXT)
               for r in results[1:1 + _MAX_RELATED] if r.get("title")]
    return _success(
        q,
        _clip(top.get("content") or top.get("title") or "", _MAX_ABSTRACT),
        top.get("url", ""),
        top.get("engine", ""),
        related,
        "searxng",
    )


async def _duckduckgo(hass: Any, q: str) -> dict:
    import aiohttp
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    params = {"q": q, "format": "json", "no_html": "1",
              "skip_disambig": "1", "t": "nova_home"}
    async with session.get(_DDG_ENDPOINT, params=params,
                           timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as resp:
        if resp.status != 200:
            return _error(q, f"search returned HTTP {resp.status}")
        return _shape_ddg(q, await resp.json(content_type=None))


async def _searxng(hass: Any, q: str) -> dict:
    base = str(_cfg("searxng_url", "")).rstrip("/")
    if not base:
        return _error(q, "searxng_url not configured")

    import aiohttp
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    async with session.get(f"{base}/search", params={"q": q, "format": "json"},
                           timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as resp:
        if resp.status != 200:
            return _error(q, f"searxng returned HTTP {resp.status}")
        return _shape_searxng(q, await resp.json(content_type=None))


async def research(hass: Any, query: str) -> dict:
    """Look ``query`` up with the configured backend."""
    q = _squash(query)
    if not q:
        return _error("", "empty query")

    backend = str(_cfg("search_backend", "off") or "off").lower()
    if backend not in _BACKENDS:
        return _error(q, "web research is not set up — choose a backend in Nova's settings")

    try:
        if backend == "searxng":
            return await _searxng(hass, q)
        return await _duckduckgo(hass, q)
    except Exception as exc:
        _LOGGER.debug("Web research for %r failed: %s", q, exc)
        return _error(q, f"lookup failed: {exc}")
