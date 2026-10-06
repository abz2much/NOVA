"""Pin what the rest of websocket.py's panel commands do today (8.7.22, tests only).

nova/update_config has its own file (test_pin_ws_update_config.py). This one
covers nova/get_panel_data, nova/get_activity_log, nova/reload_appliances,
nova/get_debug_log, nova/diagnostics, nova/get_setup_health,
nova/get_provider_activity, nova/semantic_search, nova/documents and
nova/get_area_sparklines. The decorators are stubbed to pass through, so the
handler bodies run with a recording connection. Tests named
test_current_behaviour_* pin behaviour that looks wrong; each says why.
"""
from __future__ import annotations

import sys
import types

import pytest

from test_pin_ws_update_config import _Conn, _entry, _hass, _stub_ws_api


@pytest.fixture
def ws(load, monkeypatch):
    _stub_ws_api(monkeypatch)
    sys.modules.pop("jc.websocket", None)
    mod = load("websocket")
    # knowledge.stats() opens knowledge.db; in a full run its path is not
    # always redirected away from /config, and it is a read for display
    # only, so get_panel_data gets a fixed answer.
    monkeypatch.setattr(mod, "_get_knowledge_stats", lambda: {"facts": 0})
    yield mod
    sys.modules.pop("jc.websocket", None)


async def _call(handler, hass, **msg):
    conn = _Conn()
    await handler(hass, conn, {"id": 7, **msg})
    return conn


def _boom(text="token=abc123 /config/nova/x"):
    def f(*a, **k):
        raise RuntimeError(text)
    return f


async def _aboom(*a, **k):
    raise RuntimeError("token=abc123 /config/nova/x")


def _safe(conn, code):
    (msg_id, got_code, message), = conn.errors
    assert (msg_id, got_code) == (7, code)
    assert message == "RuntimeError (details are in the Home Assistant log)"


# ── nova/get_panel_data ─────────────────────────────────────────────────────

async def test_panel_data_has_every_section(ws, load):
    conn = await _call(ws.ws_get_panel_data, _hass(_entry(load)))
    assert conn.errors == []
    res = conn.results[0][1]
    assert set(res) == {"status", "version", "meta", "dominant", "areas", "sleep_reason",
                        "doorbell_training", "doors", "lockdown", "intrusion", "knowledge",
                        "suggestions", "suggestions_filtered", "goals", "config"}
    assert res["version"] == ws._INTEGRATION_VERSION
    assert set(res["status"]) == {"observer", "sleep", "gemini", "broadcast", "notify",
                                  "satellites"}


async def test_panel_data_with_nobody_home_shows_away(ws, load):
    res = (await _call(ws.ws_get_panel_data, _hass(_entry(load)))).results[0][1]
    assert res["dominant"]["area_id"] is None and res["dominant"]["name"] == "AWAY"
    assert res["status"]["observer"] == {"state": "DISABLED", "level": "off"}


async def test_panel_data_reads_live_runtime_values(ws, load):
    entry = _entry(load, runtime_config={"announcements_enabled": True, "banter_level": 2,
                                         "observer_enabled": True})
    res = (await _call(ws.ws_get_panel_data, _hass(entry))).results[0][1]
    assert res["config"]["announcements_enabled"] is True
    assert res["config"]["banter_level"] == 2
    assert res["status"]["observer"] == {"state": "READY", "level": "warn"}


@pytest.mark.parametrize("key", ["lockdown_auto_on_arm", "intrusion_requires_confinement",
                                 "face_stand_down"])
async def test_panel_data_shows_a_safety_opt_in_on_only_for_a_real_true(ws, load, key):
    for stored, shown in ((True, True), ("true", False), (1, False)):
        entry = _entry(load, runtime_config={key: stored})
        res = (await _call(ws.ws_get_panel_data, _hass(entry))).results[0][1]
        assert res["config"][key] is shown


async def test_current_behaviour_panel_data_shows_the_string_false_as_on(ws, load):
    # Still pinned after 8.7.23, on purpose: writes now refuse "false" for
    # these keys, but values saved before are never re-checked on read, so
    # an old config keeps loading. Such a stored "false" still shows as on
    # (and is on, since their readers use bool() too).
    entry = _entry(load, runtime_config={"voice_confirm_enabled": "false",
                                         "sentinel_enabled": "false"})
    res = (await _call(ws.ws_get_panel_data, _hass(entry))).results[0][1]
    assert res["config"]["voice_confirm_enabled"] is True
    assert res["config"]["sentinel_enabled"] is True


_URL_KEYS = ("ollama_base_url", "custom_base_url", "llm_base_url", "searxng_url",
             "departure_osrm_url")


async def test_panel_data_masks_a_user_and_password_in_endpoint_urls(ws, load):
    # 8.7.23: was test_current_behaviour_panel_data_returns_endpoint_urls_
    # unmasked. The diagnostics scrubber masks user:pass@; the field stays a
    # string, so the response shape is unchanged. The saved value is not.
    url = "http://nova:hunter2@10.0.0.2:11434"
    entry = _entry(load, runtime_config={k: url for k in _URL_KEYS})
    cfg = (await _call(ws.ws_get_panel_data, _hass(entry))).results[0][1]["config"]
    for key in _URL_KEYS:
        assert cfg[key] == "http://**REDACTED**@10.0.0.2:11434", key
        assert entry.runtime_data.runtime_config[key] == url


async def test_panel_data_leaves_plain_and_empty_urls_as_they_are(ws, load):
    plain = "http://10.0.0.2:11434/v1"
    entry = _entry(load, runtime_config={"ollama_base_url": plain})
    cfg = (await _call(ws.ws_get_panel_data, _hass(entry))).results[0][1]["config"]
    assert cfg["ollama_base_url"] == plain
    assert cfg["custom_base_url"] == "" and cfg["searxng_url"] == ""


def test_masking_still_hides_the_password_if_the_scrubber_is_missing(ws, monkeypatch):
    monkeypatch.setitem(sys.modules, "jc.diagnostics", None)   # import fails
    assert ws._masked_url("http://a:b@h") == "**REDACTED**"
    assert ws._masked_url("http://h:1") == "http://h:1"
    assert ws._masked_url(None) == ""


async def test_panel_data_failure_returns_no_raw_text(ws, load, monkeypatch):
    monkeypatch.setattr(ws, "_get_entry", _boom())
    _safe(await _call(ws.ws_get_panel_data, _hass(_entry(load))), "panel_data_failed")


# ── nova/get_activity_log ───────────────────────────────────────────────────

@pytest.fixture
def activity(load, monkeypatch):
    db = load("database")
    calls = []
    rows = []

    def get_recent_activity(hours, limit):
        calls.append((hours, limit))
        return rows
    monkeypatch.setattr(db, "get_recent_activity", get_recent_activity)
    return calls, rows


async def test_activity_log_rows_are_shaped_for_the_panel(ws, load, activity):
    calls, rows = activity
    rows += [
        {"timestamp": "2026-04-23T05:30:00", "urgency": "high", "entity_id": "lock.front_door",
         "message": "lock.front_door unlocked", "source": "sentinel"},
        {"timestamp": "garbage", "message": "m"},
    ]
    conn = await _call(ws.ws_get_activity_log, _hass(_entry(load)), hours=24, limit=50)
    first, second = conn.results[0][1]["entries"]
    assert first["urgency"] == "high" and first["source"] == "sentinel"
    assert first["tag"] == "FRONT_DOOR" and len(first["ts"]) == 5
    assert second == {"ts": "garba", "urgency": "low", "tag": "", "msg": "m",
                      "source": "observer"}
    assert calls == [(24, 50)]


async def test_current_behaviour_activity_log_hours_and_limit_are_not_bounded(ws, load, activity):
    # Looks wrong: unlike nova/list_actions (1 to 100) and nova/list_decisions
    # (1 to 200), hours and limit go to the database as sent, so one call can
    # ask for every row ever logged.
    calls, _ = activity
    await _call(ws.ws_get_activity_log, _hass(_entry(load)), hours=10**6, limit=10**9)
    await _call(ws.ws_get_activity_log, _hass(_entry(load)), hours=-5, limit=-1)
    assert calls == [(10**6, 10**9), (-5, -1)]


async def test_activity_log_failure_returns_no_raw_text(ws, load, monkeypatch):
    monkeypatch.setattr(load("database"), "get_recent_activity", _boom())
    conn = await _call(ws.ws_get_activity_log, _hass(_entry(load)), hours=1, limit=1)
    _safe(conn, "activity_log_failed")


# ── nova/reload_appliances ──────────────────────────────────────────────────

async def test_reload_appliances_restarts_the_monitor(ws, load, monkeypatch):
    am = load("appliance_monitor")
    started = []

    async def start(hass, cfg, entry=None):
        started.append((cfg, entry))
    monkeypatch.setattr(am, "start", start)
    monkeypatch.setattr(ws, "_get_appliance_status", lambda: [{"name": "Washer"}])
    entry = _entry(load)
    conn = await _call(ws.ws_reload_appliances, _hass(entry))
    assert conn.results == [(7, {"ok": True, "appliances": [{"name": "Washer"}]})]
    assert len(started) == 1 and started[0][1] is entry


async def test_reload_appliances_failure_returns_no_raw_text(ws, load, monkeypatch):
    monkeypatch.setattr(load("appliance_monitor"), "start", _aboom)
    _safe(await _call(ws.ws_reload_appliances, _hass(_entry(load))), "reload_failed")


# ── debug log, diagnostics, setup health, provider activity ────────────────

async def test_debug_log_names_entities(ws, load, monkeypatch):
    monkeypatch.setattr(ws, "_DEBUG_LOG", [{"msg": "lock.front_door jammed"}, "raw"])
    monkeypatch.setattr(ws, "_entity_names", lambda hass: {"lock.front_door": "Front Door"})
    conn = await _call(ws.ws_get_debug_log, _hass(_entry(load)))
    entries = conn.results[0][1]["entries"]
    assert "Front Door" in entries[0]["msg"] and entries[1] == "raw"


async def test_debug_log_with_no_names_is_unchanged(ws, load, monkeypatch):
    monkeypatch.setattr(ws, "_DEBUG_LOG", [{"msg": "lock.front_door"}])
    monkeypatch.setattr(ws, "_entity_names", lambda hass: {})
    conn = await _call(ws.ws_get_debug_log, _hass(_entry(load)))
    assert conn.results[0][1]["entries"] == [{"msg": "lock.front_door"}]


async def test_debug_log_answers_unnamed_when_naming_fails(ws, load, monkeypatch):
    # 8.7.23: was test_current_behaviour_debug_log_raises_instead_of_
    # returning_an_error. The pinned contract has no error code for this
    # command, so it answers in its usual shape with the entity_ids as they
    # are; the failure goes to the Home Assistant log.
    monkeypatch.setattr(ws, "_DEBUG_LOG", [{"msg": "lock.front_door jammed"}])
    monkeypatch.setattr(ws, "_entity_names", _boom())
    conn = await _call(ws.ws_get_debug_log, _hass(_entry(load)))
    assert conn.errors == []
    assert conn.results == [(7, {"entries": [{"msg": "lock.front_door jammed"}]})]


@pytest.mark.parametrize("handler,module,fn,code", [
    ("ws_diagnostics", "diagnostics", "run_service_health", "diagnostics_failed"),
    ("ws_get_setup_health", "setup_health", "run_setup_health", "get_setup_health_failed"),
])
async def test_health_reports_pass_through_and_fail_safely(ws, load, monkeypatch,
                                                           handler, module, fn, code):
    # diagnostics is a package the test loader does not build, so the
    # handler's `from . import diagnostics` gets a stand-in module.
    mod = types.ModuleType(f"jc.{module}")
    monkeypatch.setitem(sys.modules, f"jc.{module}", mod)
    monkeypatch.setattr(sys.modules["jc"], module, mod, raising=False)

    async def ok(hass):
        return {"llm": "ok"}
    monkeypatch.setattr(mod, fn, ok, raising=False)
    conn = await _call(getattr(ws, handler), _hass(_entry(load)))
    assert conn.results == [(7, {"llm": "ok"})]
    monkeypatch.setattr(mod, fn, _aboom)
    _safe(await _call(getattr(ws, handler), _hass(_entry(load))), code)


@pytest.mark.parametrize("asked,used", [(7, 7), (0, 1), (-3, 1), (365, 90)])
async def test_provider_activity_days_are_held_to_1_to_90(ws, load, monkeypatch, asked, used):
    pa = load("provider_activity")
    seen = []
    monkeypatch.setattr(pa, "db_path_for", lambda hass: "db")
    monkeypatch.setattr(pa, "list_days", lambda days, db_path=None: seen.append(days) or [])
    conn = await _call(ws.ws_get_provider_activity, _hass(_entry(load)), days=asked)
    assert seen == [used] and conn.results == [(7, {"days": []})]


async def test_provider_activity_failure_returns_no_raw_text(ws, load, monkeypatch):
    monkeypatch.setattr(load("provider_activity"), "db_path_for", _boom())
    _safe(await _call(ws.ws_get_provider_activity, _hass(_entry(load)), days=7),
          "get_provider_activity_failed")


# ── nova/semantic_search ────────────────────────────────────────────────────

@pytest.fixture
def emb(load, monkeypatch):
    e = load("embeddings")
    nc = load("nova_config")
    store: dict = {}
    calls = []
    monkeypatch.setattr(nc, "set", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr(nc, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(e, "init_store", lambda: calls.append("init") or True)
    monkeypatch.setattr(e, "vector_count", lambda: 12)
    monkeypatch.setattr(e, "_model", lambda: "nomic-embed-text")
    result = {"ok": True, "model": "nomic-embed-text", "dim": 768}

    async def probe(hass):
        calls.append("probe")
        return dict(result)
    monkeypatch.setattr(e, "probe", probe)
    logged = []
    monkeypatch.setattr(sys.modules["jc.websocket"], "nova_log",
                        lambda cat, msg: logged.append((cat, msg)))
    return store, calls, result, logged, e


async def test_semantic_enable_saves_then_probes(ws, load, emb):
    store, calls, _, logged, _ = emb
    conn = await _call(ws.ws_semantic_search, _hass(_entry(load)), action="enable")
    assert store == {"semantic_search": True} and calls == ["init", "probe"]
    assert conn.results[0][1]["enabled"] is True and conn.results[0][1]["ok"] is True
    assert "semantic search enabled (Ollama" in logged[0][1]


async def test_semantic_enable_with_ollama_down_still_saves_and_says_so(ws, load, emb):
    store, _, result, logged, _ = emb
    result.update(ok=False, error="connection refused")
    conn = await _call(ws.ws_semantic_search, _hass(_entry(load)), action="enable")
    assert store == {"semantic_search": True}
    assert conn.results[0][1]["ok"] is False
    assert "not ready: connection refused" in logged[0][1]


async def test_semantic_disable_and_test(ws, load, emb):
    store, calls, _, _, _ = emb
    conn = await _call(ws.ws_semantic_search, _hass(_entry(load)), action="disable")
    assert conn.results == [(7, {"enabled": False, "ok": True})] and store == {"semantic_search": False}
    conn = await _call(ws.ws_semantic_search, _hass(_entry(load)), action="test")
    assert conn.results[0][1]["ok"] is True and calls == ["probe"]


async def test_semantic_status_masks_a_user_and_password_in_the_endpoint(ws, load, emb,
                                                                         monkeypatch):
    # 8.7.23: was test_current_behaviour_semantic_status_returns_the_
    # endpoint_unmasked. Same keys, "base" masked.
    _, _, _, _, e = emb
    monkeypatch.setattr(e, "_ollama_base", lambda: "http://nova:hunter2@10.0.0.2:11434")
    conn = await _call(ws.ws_semantic_search, _hass(_entry(load)), action="status")
    assert conn.results == [(7, {"enabled": False, "ollama_configured": True,
                                 "base": "http://**REDACTED**@10.0.0.2:11434",
                                 "model": "nomic-embed-text", "vector_count": 12})]


async def test_semantic_status_with_no_endpoint_is_an_empty_base(ws, load, emb, monkeypatch):
    monkeypatch.setattr(emb[4], "_ollama_base", lambda: None)
    res = (await _call(ws.ws_semantic_search, _hass(_entry(load)), action="status")).results[0][1]
    assert res["base"] == "" and res["ollama_configured"] is False


async def test_semantic_failure_returns_no_raw_text(ws, load, emb, monkeypatch):
    monkeypatch.setattr(emb[4], "probe", _aboom)
    _safe(await _call(ws.ws_semantic_search, _hass(_entry(load)), action="test"),
          "semantic_search_failed")


# ── nova/documents ──────────────────────────────────────────────────────────

@pytest.fixture
def docs(load, monkeypatch):
    d = load("documents")
    seen = []
    logged = []
    monkeypatch.setattr(sys.modules["jc.websocket"], "nova_log",
                        lambda cat, msg: logged.append((cat, msg)))
    monkeypatch.setattr(d, "library_status", lambda: {"chunk_count": 3})

    async def ingest(hass):
        return {"ok": True, "files_ingested": 2, "total_chunks": 9, "semantic": True,
                "embedded_chunks": 9}

    async def upload(hass, filename, content):
        seen.append(("upload", filename, content))
        return {"ok": True, "filename": filename, "chunks": 4}

    async def scan(hass):
        return {"ok": True, "new_files": 1}

    async def search(hass, query, k):
        seen.append(("search", query, k))
        return [{"source": "manual.pdf"}]
    monkeypatch.setattr(d, "ingest_directory_async", ingest)
    monkeypatch.setattr(d, "save_and_ingest_upload", upload)
    monkeypatch.setattr(d, "scan_watch_folders", scan)
    monkeypatch.setattr(d, "search_documents_async", search)
    return d, seen, logged


async def test_documents_each_action_reaches_its_function(ws, load, docs):
    d, seen, logged = docs
    hass = _hass(_entry(load))
    assert (await _call(ws.ws_documents, hass, action="status")).results == [(7, {"chunk_count": 3})]
    res = (await _call(ws.ws_documents, hass, action="ingest")).results[0][1]
    assert res["files_ingested"] == 2
    res = (await _call(ws.ws_documents, hass, action="upload", filename="a.pdf",
                       content="QQ==")).results[0][1]
    assert res == {"ok": True, "filename": "a.pdf", "chunks": 4}
    res = (await _call(ws.ws_documents, hass, action="scan_watch")).results[0][1]
    assert res["new_files"] == 1
    res = (await _call(ws.ws_documents, hass, action="search", query="boiler")).results[0][1]
    assert res == {"results": [{"source": "manual.pdf"}]}
    assert ("search", "boiler", 5) in seen and ("upload", "a.pdf", "QQ==") in seen
    assert [m for _, m in logged] == [
        "documents ingested via panel: 2 files, 9 chunks, 9 embedded",
        "document uploaded: a.pdf (4 chunks)",
        "watch-folder scan: 1 new document(s) ingested",
    ]


async def test_documents_delete_strips_a_path_from_the_name(ws, load, docs, tmp_path,
                                                            monkeypatch):
    d, _, _ = docs
    monkeypatch.setattr(d, "_documents_dir", lambda: str(tmp_path))
    monkeypatch.setattr(d, "_forget_source", lambda s: None)
    outside = tmp_path.parent / "keep.txt"
    outside.write_text("x")
    conn = await _call(ws.ws_documents, _hass(_entry(load)), action="delete",
                       filename="../keep.txt")
    assert conn.results == [(7, {"ok": True, "filename": "keep.txt"})]
    assert outside.exists()


async def test_a_failed_document_delete_returns_no_raw_exception_text(
        ws, load, docs, tmp_path, monkeypatch, caplog):
    # 8.7.23: was test_current_behaviour_a_failed_document_delete_returns_
    # raw_exception_text. The panel gets the error type; the OS error with
    # its full /config path goes to the Home Assistant log only.
    d, _, _ = docs
    monkeypatch.setattr(d, "_documents_dir", lambda: str(tmp_path))
    monkeypatch.setattr(d, "_forget_source", lambda s: None)
    (tmp_path / "manual.txt").mkdir()   # a directory: unlink() fails
    conn = await _call(ws.ws_documents, _hass(_entry(load)), action="delete",
                       filename="manual.txt")
    res = conn.results[0][1]
    assert res["ok"] is False and res["filename"] == "manual.txt"
    assert res["error"].startswith("file remove failed: ")
    assert res["error"].endswith(" (details are in the Home Assistant log)")
    assert str(tmp_path) not in res["error"]
    assert "document delete failed" in caplog.text


async def test_documents_failure_returns_no_raw_text(ws, load, docs, monkeypatch):
    monkeypatch.setattr(docs[0], "library_status", _boom())
    _safe(await _call(ws.ws_documents, _hass(_entry(load)), action="status"), "documents_failed")


# ── nova/get_area_sparklines ────────────────────────────────────────────────

async def test_sparklines_only_ask_for_areas_with_a_sensor(ws, load, monkeypatch):
    monkeypatch.setattr(ws, "_all_areas_with_anything", lambda hass: ["kitchen", "hall"])
    monkeypatch.setattr(ws, "_area_temp_humidity_entities",
                        lambda hass, a: ("sensor.k_t", None) if a == "kitchen" else (None, None))
    asked = []

    async def spark(hass, entity_map):
        asked.append(entity_map)
        return {"kitchen": {"temp": [20, 21]}}
    monkeypatch.setattr(ws, "_get_area_sparklines", spark)
    conn = await _call(ws.ws_get_area_sparklines, _hass(_entry(load)))
    assert asked == [{"kitchen": {"temp": "sensor.k_t", "humidity": None}}]
    assert conn.results == [(7, {"sparklines": {"kitchen": {"temp": [20, 21]}}})]


async def test_sparklines_failure_returns_no_raw_text(ws, load, monkeypatch):
    monkeypatch.setattr(ws, "_all_areas_with_anything", _boom())
    _safe(await _call(ws.ws_get_area_sparklines, _hass(_entry(load))), "sparklines_failed")


async def test_a_masked_url_shown_by_the_panel_cannot_be_saved_back(ws, load, monkeypatch):
    # 8.7.24: the round trip. What get_panel_data shows for a URL with a
    # password, sent straight back through nova/update_config, is refused,
    # and the saved value is untouched.
    nc = load("nova_config")
    written = []
    monkeypatch.setattr(nc, "set", lambda k, v: written.append((k, v)) or True)
    real = "http://nova:hunter2@searx.lan:8080"
    entry = _entry(load, runtime_config={"searxng_url": real, "departure_osrm_url": real})
    hass = _hass(entry)
    shown = (await _call(ws.ws_get_panel_data, hass)).results[0][1]["config"]
    for key in ("searxng_url", "departure_osrm_url"):
        conn = await _call(ws.ws_update_config, hass, key=key, value=shown[key])
        assert conn.errors == [(7, "invalid_value", "This field shows a hidden password. "
                                                   "Type the full address to change it.")]
        assert entry.runtime_data.runtime_config[key] == real
    assert written == []
