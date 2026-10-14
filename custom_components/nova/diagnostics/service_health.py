"""Health of the services Nova itself relies on (diagnostics package).

This is not the infrastructure triage in monitor.py, which looks at physical
kit and speaks up on its own. Here each row answers one narrow question about
a piece of software Nova calls, and the roll-up says whether anything is
genuinely broken:

  LLM, embeddings, TTS, STT, Voice ID, cameras, routine attribution, the
  conversation store, the scheduler and host health.

A row is a dict with at least name, key, status and detail. The rule that
shapes every check: a synthetic poke that misses is never alarming. Only a
failure during real use (reported through record_usage) makes a row DOWN;
otherwise the worst a missed poke can give is IDLE.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Row states. Only DOWN is alarming; IDLE is shown calmly.
_OK = "ok"          # verified: a probe answered, or real use just worked
_IDLE = "idle"      # reachable but quiet, or a poke missed: not a fault
_WARN = "warn"      # worth a look (breaker open, a camera offline, ...)
_DOWN = "down"      # real use failed: the one state that should alarm
_OFF = "off"        # not set up or switched off

# Outcomes reported by the code paths that really use each service, by key
# (llm, embeddings, tts, stt, voice_id). Each record may hold ok_ts, fail_ts
# and fail_detail.
_USAGE: dict = {}
_REAL_FAIL_TTL = 900.0      # how long a real failure keeps a row DOWN
_RECENT_USE_TTL = 900.0     # how long a real success counts as "recently used"

_ASLEEP = ("unavailable", "unknown")


def record_usage(key: str, ok: bool, detail: str = "") -> None:
    """Note the outcome of a real call to service `key`. A success wipes any
    earlier failure; a failure is what can turn the row DOWN. Never raises."""
    try:
        entry = _USAGE.setdefault(key, {})
        stamp = time.time()
        if not ok:
            entry["fail_ts"] = stamp
            entry["fail_detail"] = detail or "a real request failed"
            return
        entry["ok_ts"] = stamp
        for stale in ("fail_ts", "fail_detail"):
            entry.pop(stale, None)
    except Exception:
        pass


def _recent_real_failure(key: str):
    """The detail of a real failure still inside its window and not followed
    by a success, or None."""
    entry = _USAGE.get(key) or {}
    failed_at = entry.get("fail_ts")
    if not failed_at or time.time() - failed_at > _REAL_FAIL_TTL:
        return None
    succeeded_at = entry.get("ok_ts")
    if succeeded_at and succeeded_at >= failed_at:
        return None
    return entry.get("fail_detail", "a real request failed")


def _recently_used_ok(key: str) -> bool:
    """Whether a real call to `key` succeeded inside the recent-use window."""
    succeeded_at = (_USAGE.get(key) or {}).get("ok_ts")
    if not succeeded_at:
        return False
    return time.time() - succeeded_at <= _RECENT_USE_TTL


async def _retry_async(coro_fn, attempts: int = 3, delay: float = 1.2):
    """Await `coro_fn()` up to `attempts` times so a cold model that misses
    once is not reported. Returns the first result whose "ok" is truthy,
    otherwise the last result; an exception counts as a failed result."""
    result = None
    for attempt in range(1, attempts + 1):
        try:
            result = await coro_fn()
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        if isinstance(result, dict) and result.get("ok"):
            break
        if attempt < attempts:
            await asyncio.sleep(delay)
    return result


def _cfg(key: str, default=None):
    """A Nova setting, or `default` when it is unset, None or unreadable."""
    try:
        from .. import nova_config
        value = nova_config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _row(name: str, key: str, status: str = _OFF, detail: str = "") -> dict[str, Any]:
    return {"name": name, "key": key, "status": status, "detail": detail}


def _redact(url: str) -> str:
    """The URL without any user:password@ part."""
    try:
        scheme, sep, rest = url.partition("//")
        if sep and "@" in rest:
            return f"{scheme}//{rest.split('@', 1)[1]}"
    except Exception:
        pass
    return url


def _label(state) -> str:
    """'Friendly name (entity_id)', or just the id when there is no separate
    name. The id is kept because this is a diagnostic."""
    try:
        friendly = (state.attributes or {}).get("friendly_name")
    except Exception:
        friendly = None
    friendly = str(friendly).strip() if friendly else ""
    if friendly and friendly != state.entity_id:
        return f"{friendly} ({state.entity_id})"
    return str(state.entity_id)


# ── LLM ─────────────────────────────────────────────────────────────────────

def _self_hosted_endpoint(provider: str):
    """The configured endpoint for an Ollama or custom provider, or None."""
    urls = ("ollama_base_url", "custom_base_url", "llm_base_url")
    try:
        from ..llm_provider import resolve_provider_endpoint
        return resolve_provider_endpoint({f: _cfg(f, "") for f in urls}, provider)
    except (ImportError, ValueError):
        return None


def _breaker_status():
    try:
        from .. import connectivity
        return connectivity.status()
    except Exception:
        return None


async def _check_llm(hass) -> dict:
    row = _row("LLM", "llm")
    provider = str(_cfg("llm_provider", "") or _cfg("provider", "") or "").strip()
    self_hosted = provider in ("ollama", "custom")
    base = _self_hosted_endpoint(provider) if self_hosted else None
    breaker = _breaker_status()

    if self_hosted and not base:
        row["detail"] = f"no {provider} endpoint configured"
        return row

    failure = _recent_real_failure("llm")
    if failure:
        row["status"] = _DOWN
        row["detail"] = f"a real agent call failed: {failure}"
    elif provider == "ollama" and base:
        reachable, detail = await _ping_ollama(hass, base)
        if not reachable:
            # One more go: a single blip should not change the row.
            await asyncio.sleep(1.0)
            reachable, detail = await _ping_ollama(hass, base)
        if reachable:
            row["status"] = _OK
        elif _recently_used_ok("llm"):
            row["status"] = _OK
            detail = "working (recent agent call succeeded; ping missed)"
        else:
            row["status"] = _IDLE
        row["detail"] = detail
        row["base"] = _redact(base)
    else:
        who = provider or "cloud"
        if breaker and breaker.get("state") == "OPEN":
            row["status"] = _WARN
            row["detail"] = f"{who} provider — breaker OPEN (recent failures)"
        else:
            row["status"] = _OK
            row["detail"] = f"{who} provider — no recent failures"
        if base:
            row["base"] = _redact(base)
    if breaker:
        row["breaker"] = breaker.get("state")
    return row


async def _ping_ollama(hass, base: str) -> tuple[bool, str]:
    """Ask Ollama for its model list; (reachable, detail)."""
    root = base.rstrip("/").removesuffix("/v1")
    try:
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)
        limit = aiohttp.ClientTimeout(total=8)
        async with session.get(f"{root}/api/tags", timeout=limit) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status} from {root}"
            try:
                body = await resp.json(content_type=None)
                count = len(body.get("models", []) or [])
            except Exception:
                return True, "reachable"
            return True, f"reachable — {count} model(s) available"
    except Exception as exc:
        return False, f"unreachable: {exc}"


# ── embeddings ──────────────────────────────────────────────────────────────

async def _check_embeddings(hass) -> dict:
    row = _row("Embeddings", "embeddings")
    try:
        from .. import embeddings
        if not embeddings.is_enabled():
            row["detail"] = "semantic search disabled (keyword search active)"
            return row
        failure = _recent_real_failure("embeddings")
        if failure:
            row["status"] = _DOWN
            row["detail"] = f"a real embedding request failed: {failure}"
            return row
        probe = await _retry_async(lambda: embeddings.probe(hass))
        if probe and probe.get("ok"):
            row["status"] = _OK
            row["detail"] = f"Ollama {probe.get('model')} — {probe.get('dim')}-dim vectors"
        elif _recently_used_ok("embeddings"):
            # The model is probably unloaded between uses; real use is fine.
            row["status"] = _OK
            row["detail"] = ("working (recent embedding succeeded; "
                             "model idle for the health poke)")
        else:
            row["status"] = _IDLE
            row["detail"] = ("not exercised recently — the embed model may be "
                             "unloaded at idle; it loads on demand when you "
                             "ingest or search documents")
    except Exception as exc:
        # The check broke, not the service.
        row["status"] = _IDLE
        row["detail"] = f"health check could not verify: {exc}"
    return row


# ── speech engines ──────────────────────────────────────────────────────────

def _check_tts(hass) -> dict:
    engine = str(_cfg("tts_engine", "auto") or "auto")
    return _check_speech_entity(hass, domain="tts", name="TTS", configured=engine)


def _check_stt(hass) -> dict:
    engine = str(_cfg("stt_engine", "auto") or "auto")
    return _check_speech_entity(hass, domain="stt", name="STT", configured=engine)


def _check_speech_entity(hass, domain: str, name: str, configured: str) -> dict:
    """A TTS or STT row. Engines often sit unavailable until asked, so that
    alone is IDLE; a real failed request is the only route to DOWN."""
    row = _row(name, domain)
    failure = _recent_real_failure(domain)
    if failure:
        row["status"] = _DOWN
        row["detail"] = f"a real {domain} request failed: {failure}"
        return row
    used = _recently_used_ok(domain)

    try:
        engines = hass.states.async_all(domain)
    except Exception:
        engines = []
    if not engines:
        # Nothing registered is a setup gap, not an outage.
        row["status"] = _OK if used else _IDLE
        row["detail"] = (f"{domain} used successfully recently" if used else
                         f"no {domain} engine registered (install the add-on to enable)")
        return row

    if configured in ("", "auto"):
        _auto_engine(row, engines, domain, used)
        return row

    chosen = next((s for s in engines if configured in s.entity_id), None)
    if chosen is None:
        row["status"] = _WARN
        row["detail"] = f"configured '{configured}' not found; {len(engines)} other(s) present"
        return row
    shown = _label(chosen)
    if chosen.state not in _ASLEEP:
        row["status"], row["detail"] = _OK, f"{shown} available"
    elif used:
        row["status"], row["detail"] = _OK, f"{shown} idle (recent {domain} succeeded)"
    else:
        row["status"], row["detail"] = _IDLE, f"{shown} idle — comes available on demand"
    row["entity"] = chosen.entity_id
    return row


def _auto_engine(row: dict, engines: list, domain: str, used: bool) -> None:
    """Fill `row` for the "auto" setting: any engine that is up will do."""
    awake = [s for s in engines if s.state not in _ASLEEP]
    total = len(engines)
    if awake:
        row["status"] = _OK
        row["detail"] = f"{len(awake)} engine(s) available (auto)"
        row["entity"] = awake[0].entity_id
    elif used:
        row["status"] = _OK
        row["detail"] = f"{total} engine(s) present, idle (recent {domain} succeeded)"
    else:
        row["status"] = _IDLE
        row["detail"] = (f"{total} engine(s) present, idle — they come "
                         f"available on demand when {domain} is used")


# ── optional features and housekeeping ──────────────────────────────────────

def _check_cameras(hass) -> dict:
    """Cameras are a feature, not the core: at worst WARN, and OFF when the
    home has none."""
    row = _row("Cameras", "cameras")
    try:
        cameras = hass.states.async_all("camera")
    except Exception:
        cameras = []
    if not cameras:
        row["detail"] = "no cameras configured (optional)"
        return row
    total = len(cameras)
    offline = sum(1 for s in cameras
                  if str(getattr(s, "state", "")).lower()
                  in ("unavailable", "unknown", "none", ""))
    if offline == 0:
        row["status"] = _OK
        row["detail"] = f"{total} camera(s) available"
    elif offline == total:
        row["status"] = _WARN
        row["detail"] = f"all {total} camera(s) unavailable"
    else:
        row["status"] = _WARN
        row["detail"] = f"{total - offline}/{total} available, {offline} unavailable"
    return row


def _check_database(hass) -> dict:
    """The conversation store. A store that does not exist yet is OFF; one
    that exists but fails its health probe is DOWN."""
    row = _row("Conversation store", "database")
    try:
        from .. import database
    except Exception:
        row["detail"] = "database module unavailable"
        return row
    try:
        present = database._db_path().exists()
    except Exception:
        present = False
    if not present:
        row["detail"] = "not yet created"
        return row
    try:
        report = database.health()
    except Exception as exc:
        row["status"] = _DOWN
        row["detail"] = f"health probe error: {exc}"
        return row
    if report.get("ok"):
        row["status"] = _OK
        row["detail"] = "conversation store OK"
    else:
        row["status"] = _DOWN
        row["detail"] = f"schema/migration error: {report.get('error') or 'unknown'}"
    return row


def _check_voice_id(hass) -> dict:
    """The Voice ID app. OFF until an address is set; a real request that
    failed is DOWN; a missed check is IDLE because the fallback still works."""
    row = _row("Voice ID", "voice_id", detail="no app address set")
    from ..runtime import domain_runtime
    runtime = domain_runtime(hass)
    app = getattr(runtime, "voice_id", None) if runtime is not None else None
    info = app.status() if app is not None else None
    if not info or info.get("state") == "not_set":
        return row
    failure = _recent_real_failure("voice_id")
    if failure:
        row["status"] = _DOWN
        row["detail"] = f"a real request failed: {failure}"
        return row
    state = info.get("state")
    if state == "connected" or _recently_used_ok("voice_id"):
        version = info.get("version") or ""
        row["status"] = _OK
        # Concatenated on purpose: a non-text version raises, as it always has.
        row["detail"] = "app connected" + (" (v" + version + ")" if version else "")
    else:
        row["status"] = _IDLE
        row["detail"] = ("app not reachable at the last check; the fallback is used"
                         if state == "unreachable" else "checking the app")
    return row


def _check_scheduler(hass) -> dict:
    """The entry's periodic task scheduler: WARN when a task keeps failing,
    OFF before it has started."""
    row = _row("Scheduler", "scheduler")
    from ..runtime import domain_runtime
    runtime = domain_runtime(hass)
    scheduler = runtime.scheduler if runtime is not None else None
    if scheduler is None:
        row["detail"] = "not running"
        return row
    try:
        tasks = scheduler.status()
    except Exception as exc:
        row["status"] = _WARN
        row["detail"] = f"status error: {exc}"
        return row
    if not tasks:
        row["detail"] = "no periodic tasks"
        return row
    stuck = [str(t.get("name")) for t in tasks if t.get("consecutive_errors", 0) >= 3]
    if stuck:
        row["status"] = _WARN
        row["detail"] = f"{len(stuck)} task(s) failing: {', '.join(stuck)}"
    else:
        row["status"] = _OK
        row["detail"] = f"{len(tasks)} periodic task(s) scheduled"
    return row


def _check_host_health(hass) -> dict:
    """A summary of host_health.snapshot() (its snapshot rides along). Host
    load is never a household emergency, so the worst here is WARN."""
    row = _row("Host health", "host_health")
    try:
        from .. import host_health, nova_config
        snap = host_health.snapshot(hass, nova_config.get_all())
        row["snapshot"] = snap
        if not snap.get("enabled"):
            row["detail"] = "not enabled (Settings → Host Health)"
            return row
        overall = snap.get("overall")
        if overall == "problem":
            labels = ", ".join(p["label"] for p in snap.get("persistent_problems", []))
            row["status"], row["detail"] = _WARN, f"persistent problem: {labels}"
        elif overall == "watch":
            row["status"], row["detail"] = _OK, "elevated reading(s), not yet persistent"
        elif overall == "partial":
            live = len(snap.get("available", []))
            gaps = len(snap.get("missing_or_stale", []))
            row["status"] = _OK
            row["detail"] = f"{live} reading(s) active, {gaps} missing/stale"
        elif overall == "ok":
            live = len(snap.get("available", []))
            row["status"], row["detail"] = _OK, f"{live} reading(s) healthy"
        else:
            row["status"], row["detail"] = _IDLE, "no readings yet"
    except Exception as exc:
        row["status"] = _IDLE
        row["detail"] = f"host health check could not run: {str(exc)[:120]}"
    return row


def _check_routines(hass) -> dict:
    """A setting check: an identity-confidence bar set very high means almost
    no observation is ever attributed to a person, starving their routines."""
    row = _row("Routines", "routines", status=_OK)
    try:
        bar = float(_cfg("identity_min_confidence", 0.45))
    except Exception as exc:
        row["status"] = _IDLE
        row["detail"] = str(exc)[:80]
        return row
    if bar >= 0.85:
        row["status"] = _WARN
        row["detail"] = (f"identity confidence {bar:.2f} is very high — per-person "
                         "routines rarely attribute; lower to ~0.5–0.6")
    else:
        row["detail"] = f"identity confidence {bar:.2f} — attribution active"
    return row


# ── roll-up ─────────────────────────────────────────────────────────────────

# (row name, key, check function name). Looked up at run time so each check
# can be swapped out on its own.
_CHECKS = (
    ("LLM", "llm", "_check_llm"),
    ("Embeddings", "embeddings", "_check_embeddings"),
    ("TTS", "tts", "_check_tts"),
    ("STT", "stt", "_check_stt"),
    ("Voice ID", "voice_id", "_check_voice_id"),
    ("Cameras", "cameras", "_check_cameras"),
    ("Routines", "routines", "_check_routines"),
    ("Conversation store", "database", "_check_database"),
    ("Scheduler", "scheduler", "_check_scheduler"),
    ("Host health", "host_health", "_check_host_health"),
)
_AWAITED = frozenset({"llm", "embeddings"})


def _summarise(rows: list[dict[str, Any]]) -> tuple[str, str]:
    """(overall, summary) for the rows that are switched on."""
    live = [r for r in rows if r.get("status") != _OFF]
    if not live:
        return _OFF, "no core services active"
    tally = {state: sum(1 for r in live if r["status"] == state)
             for state in (_OK, _IDLE, _WARN, _DOWN)}
    if tally[_DOWN]:
        overall = _DOWN
    elif tally[_WARN]:
        overall = _WARN
    else:
        overall = _OK      # OK and IDLE are both fine
    healthy = f"{tally[_OK]}/{len(live)} healthy"
    if tally[_DOWN]:
        return overall, f"{tally[_DOWN]} service(s) failing — {healthy}"
    if tally[_IDLE]:
        return overall, f"{healthy}, {tally[_IDLE]} idle (not recently used)"
    return overall, f"all {len(live)} core services healthy"


async def run_service_health(hass) -> dict:
    """Run every check and roll them up into
    {"overall", "services", "summary"}. A check that raises becomes a DOWN
    row carrying the error text. Never raises."""
    rows: list[dict[str, Any]] = []
    for name, key, fn_name in _CHECKS:
        try:
            outcome: Any = globals()[fn_name](hass)
            if key in _AWAITED:
                outcome = await outcome
            rows.append(outcome)
        except Exception as exc:
            rows.append(_row(name, key, _DOWN, str(exc)))
    overall, summary = _summarise(rows)
    return {"overall": overall, "services": rows, "summary": summary}
