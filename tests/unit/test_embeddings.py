"""Fresh behaviour tests for embeddings.py, written from the spec.

The Ollama server is a scripted fake session; the vector store is a temp
SQLite file."""
import logging
import math
import os
import sys
import types

import pytest

BASE = "http://ollama.test:11434"


class _Timeout:
    def __init__(self, total=None):
        self.total = total


@pytest.fixture(autouse=True)
def fake_aiohttp(monkeypatch):
    """embeddings imports aiohttp at call time; give it a deterministic stand-in."""
    mod = types.ModuleType("aiohttp")
    mod.ClientTimeout = _Timeout
    mod.ClientSession = object
    monkeypatch.setitem(sys.modules, "aiohttp", mod)
    return mod


@pytest.fixture
def emb(load, monkeypatch, tmp_path):
    m = load("embeddings")
    monkeypatch.setattr(m, "_DB_PATH", str(tmp_path / "nova.db"))
    saved = m._EMBED_LAST_ERROR[0]
    m._EMBED_LAST_ERROR[0] = ""
    yield m
    m._EMBED_LAST_ERROR[0] = saved


@pytest.fixture
def cfg(emb, monkeypatch):
    values = {}
    monkeypatch.setattr(emb, "_cfg", lambda k, d: values.get(k, d))
    return values


@pytest.fixture
def health(load, monkeypatch):
    sh = load("diagnostics.service_health")
    seen = []
    monkeypatch.setattr(sh, "record_usage", lambda name, ok, detail="": seen.append((name, ok, detail)))
    return seen


class Resp:
    def __init__(self, status=200, body=None, text=None, text_exc=None):
        self.status, self._body, self._text, self._text_exc = status, body, text, text_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type="application/json"):
        assert content_type is None
        return self._body

    async def text(self):
        if self._text_exc:
            raise self._text_exc
        return self._text if self._text is not None else str(self._body)


class Session:
    """Answers each POST with route(url, payload) and records the calls."""

    def __init__(self, route):
        self.route, self.calls = route, []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        out = self.route(url, json)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def ollama(emb, monkeypatch):
    """Point the module at BASE and a fake session driven by `route`."""
    import homeassistant.helpers.aiohttp_client as ac

    def _install(route, model="nomic-embed-text"):
        session = Session(route)
        monkeypatch.setattr(emb, "_ollama_base", lambda: BASE)
        monkeypatch.setattr(emb, "_model", lambda: model)
        monkeypatch.setattr(ac, "async_get_clientsession", lambda hass: session)
        return session
    return _install


# ── config ───────────────────────────────────────────────────────────────────

def test_module_constants(emb):
    assert emb._DEFAULT_MODEL == "nomic-embed-text"
    assert (emb._TIMEOUT, emb._EMBED_BATCH) == (60, 32)
    assert isinstance(emb._EMBED_LAST_ERROR, list) and len(emb._EMBED_LAST_ERROR) == 1


def test_cfg_reads_nova_config(emb, load, monkeypatch):
    nc = load("nova_config")
    monkeypatch.setattr(nc, "get", lambda k, d=None: {"a": 1, "n": None}.get(k, d))
    assert emb._cfg("a", 0) == 1
    assert emb._cfg("n", "fallback") == "fallback"
    assert emb._cfg("missing", "dflt") == "dflt"

    def boom(k, d=None):
        raise RuntimeError("config broken")
    monkeypatch.setattr(nc, "get", boom)
    assert emb._cfg("a", "safe") == "safe"


def test_model_default_and_override(emb, cfg):
    assert emb._model() == "nomic-embed-text"
    cfg["embed_model"] = "mxbai-embed-large"
    assert emb._model() == "mxbai-embed-large"
    cfg["embed_model"] = ""
    assert emb._model() == "nomic-embed-text"


@pytest.mark.parametrize("values, expected", [
    ({}, None),
    ({"llm_base_url": "   "}, None),
    ({"llm_base_url": "http://gpu.lan:11434"}, "http://gpu.lan:11434"),
    ({"llm_base_url": " http://gpu.lan:11434/v1/ "}, "http://gpu.lan:11434"),
    ({"ollama_base_url": "http://o.lan//", "llm_base_url": "http://l.lan"}, "http://o.lan"),
    ({"embed_base_url": "http://e.lan", "ollama_base_url": "http://o.lan"}, "http://e.lan"),
    ({"embed_base_url": "", "llm_base_url": "http://l.lan/v1/v1"}, "http://l.lan/v1"),
])
def test_ollama_base(emb, cfg, values, expected):
    cfg.update(values)
    assert emb._ollama_base() == expected


def test_is_enabled_needs_flag_and_base(emb, cfg):
    assert emb.is_enabled() is False
    cfg["semantic_search"] = True
    assert emb.is_enabled() is False
    cfg["ollama_base_url"] = "http://o.lan"
    assert emb.is_enabled() is True
    cfg["semantic_search"] = 0
    assert emb.is_enabled() is False


def test_db_path_override_and_default(emb, load, monkeypatch, tmp_path):
    assert emb._db_path() == str(tmp_path / "nova.db")
    paths = load("paths")
    monkeypatch.setattr(paths, "_root", str(tmp_path / "cfg"))
    monkeypatch.setattr(emb, "_DB_PATH", None)
    assert emb._db_path() == os.path.join(str(tmp_path / "cfg"), "nova.db")


# ── vector math ──────────────────────────────────────────────────────────────

def test_pack_is_little_endian_float32(emb):
    assert emb._pack([1.0]) == b"\x00\x00\x80\x3f"
    assert emb._pack([]) == b"" and emb._unpack(b"") == []
    out = emb._unpack(emb._pack([0.25, -2.5, 1e-3]))
    assert out[:2] == [0.25, -2.5] and abs(out[2] - 1e-3) < 1e-7


@pytest.mark.parametrize("a, b, expected", [
    ([2.0, 0.0], [5.0, 0.0], 1.0),
    ([1.0, 0.0], [0.0, 3.0], 0.0),
    ([1.0, 1.0], [-1.0, -1.0], -1.0),
    ([1.0, 2.0, 2.0], [2.0, 1.0, 2.0], 8.0 / 9.0),
])
def test_cosine_values(emb, a, b, expected):
    assert math.isclose(emb._cosine(a, b), expected, abs_tol=1e-9)


@pytest.mark.parametrize("a, b", [([], [1.0]), ([1.0], []), ([1.0, 2.0], [1.0]),
                                  ([0.0, 0.0], [1.0, 1.0]), ([1.0, 1.0], [0.0, 0.0])])
def test_cosine_degenerate_is_minus_one(emb, a, b):
    assert emb._cosine(a, b) == -1.0


# ── store ────────────────────────────────────────────────────────────────────

def test_init_store_creates_table(emb):
    assert emb.init_store() is True
    assert emb.init_store() is True          # idempotent
    assert emb.vector_count() == 0


def test_init_store_missing_directory_warns(emb, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(emb, "_DB_PATH", str(tmp_path / "absent" / "nova.db"))
    with caplog.at_level(logging.WARNING):
        assert emb.init_store() is False
    assert any("doc_vectors init failed" in r.getMessage() for r in caplog.records)
    assert not (tmp_path / "absent").exists()


def test_vector_count_without_table_is_zero(emb):
    assert emb.vector_count() == 0


def test_store_vectors_writes_rows(emb, cfg, tmp_path):
    import sqlite3
    cfg["embed_model"] = "tiny-embed"
    emb.init_store()
    n = emb.store_vectors("manual.pdf", ["filter size", "reset steps"],
                          [[1.0, 0.0, 0.0], [0.0, 1.0]], "2026-01-02T03:04:05")
    assert n == 2 and emb.vector_count() == 2
    with sqlite3.connect(str(tmp_path / "nova.db")) as c:
        rows = c.execute("SELECT source, chunk, content, dim, vec, model, ingested "
                         "FROM doc_vectors ORDER BY id").fetchall()
    assert rows[0] == ("manual.pdf", 0, "filter size", 3, emb._pack([1.0, 0.0, 0.0]),
                       "tiny-embed", "2026-01-02T03:04:05")
    assert rows[1][1:4] == (1, "reset steps", 2)


@pytest.mark.parametrize("chunks, vectors", [([], [[1.0]]), (["a"], []), (["a", "b"], [[1.0]])])
def test_store_vectors_rejects_bad_input(emb, chunks, vectors):
    emb.init_store()
    assert emb.store_vectors("s", chunks, vectors, "t") == 0
    assert emb.vector_count() == 0


def test_store_vectors_without_table_returns_zero(emb, caplog):
    with caplog.at_level(logging.DEBUG):
        assert emb.store_vectors("s", ["a"], [[1.0]], "t") == 0
    assert any("doc_vectors store failed" in r.getMessage() for r in caplog.records)


def test_store_appends_and_forget_removes_one_source(emb):
    emb.init_store()
    emb.store_vectors("a.txt", ["x"], [[1.0, 0.0]], "t")
    emb.store_vectors("a.txt", ["x again"], [[1.0, 0.0]], "t")
    emb.store_vectors("b.txt", ["y"], [[0.0, 1.0]], "t")
    assert emb.vector_count() == 3
    assert emb.forget_source("a.txt") is None
    assert emb.vector_count() == 1
    emb.forget_source("never-stored")
    assert emb.vector_count() == 1


def test_forget_without_table_is_quiet(emb):
    assert emb.forget_source("x") is None


# ── search ───────────────────────────────────────────────────────────────────

@pytest.fixture
def library(emb):
    emb.init_store()
    emb.store_vectors("boiler.pdf", ["pressure", "bleeding radiators", "pilot light"],
                      [[1.0, 0.0, 0.0], [0.8, 0.6, 0.0], [0.0, 0.0, 1.0]], "t")
    emb.store_vectors("old.pdf", ["two-dim"], [[1.0, 0.0]], "t")
    emb.store_vectors("anti.pdf", ["opposite"], [[-1.0, 0.0, 0.0]], "t")
    return emb


def test_search_ranks_and_shapes_hits(library):
    hits = library.search_vectors([1.0, 0.1, 0.0], k=2)
    assert [h["text"] for h in hits] == ["pressure", "bleeding radiators"]
    assert hits[0] == {"text": "pressure", "source": "boiler.pdf", "chunk": 0,
                       "score": round(1.0 / math.sqrt(1.01), 3)}
    assert hits[1]["chunk"] == 1


def test_search_default_k_is_four_and_drops_non_positive(library):
    hits = library.search_vectors([1.0, 0.0, 0.0])
    # pressure 1.0, radiators 0.8, pilot 0.0 (dropped), two-dim -1 (dropped)
    assert [h["text"] for h in hits] == ["pressure", "bleeding radiators"]


def test_search_takes_top_k_before_dropping(library):
    hits = library.search_vectors([0.0, 0.0, 1.0], k=1)
    assert [h["text"] for h in hits] == ["pilot light"]


def test_search_ties_keep_table_order(emb):
    emb.init_store()
    emb.store_vectors("s", ["first", "second"], [[1.0, 0.0], [2.0, 0.0]], "t")
    assert [h["text"] for h in emb.search_vectors([1.0, 0.0], k=2)] == ["first", "second"]


def test_search_k_edge_cases(library):
    assert library.search_vectors([1.0, 0.0, 0.0], k=0) == []
    # five rows; scores for [1, .2, .3]: pressure, radiators, pilot > 0; two-dim
    # and opposite are negative. k=-1 slices off only the last (lowest) row.
    assert [h["text"] for h in library.search_vectors([1.0, 0.2, 0.3], k=-1)] == [
        "pressure", "bleeding radiators", "pilot light"]
    assert [h["text"] for h in library.search_vectors([1.0, 0.2, 0.3], k=-3)] == [
        "pressure", "bleeding radiators"]


def test_search_empty_query_or_store(emb, caplog):
    assert emb.search_vectors([]) == []
    with caplog.at_level(logging.DEBUG):
        assert emb.search_vectors([1.0]) == []          # no table yet
    assert any("doc_vectors read failed" in r.getMessage() for r in caplog.records)
    emb.init_store()
    assert emb.search_vectors([1.0]) == []


# ── embed_texts / _embed_batch ───────────────────────────────────────────────

async def test_batch_endpoint_in_chunks_of_32(emb, ollama, fake_hass, health):
    session = ollama(lambda url, body: Resp(200, {
        "embeddings": [[float(len(t))] for t in body["input"]]}))
    texts = [f"t{i}" for i in range(70)]
    out = await emb.embed_texts(fake_hass, texts)
    assert out == [[float(len(t))] for t in texts]
    assert [len(c[1]["input"]) for c in session.calls] == [32, 32, 6]
    url, body, timeout = session.calls[0]
    assert url == BASE + "/api/embed"
    assert body == {"model": "nomic-embed-text", "input": texts[:32]}
    assert timeout.total == 60
    assert health == [("embeddings", True, "")]
    assert emb._EMBED_LAST_ERROR[0] == ""


async def test_no_base_or_no_texts_is_none_without_calls(emb, monkeypatch, fake_hass, health):
    monkeypatch.setattr(emb, "_ollama_base", lambda: None)
    assert await emb.embed_texts(fake_hass, ["x"]) is None
    monkeypatch.setattr(emb, "_ollama_base", lambda: BASE)
    assert await emb.embed_texts(fake_hass, []) is None
    assert health == []


async def test_success_clears_old_error(emb, ollama, fake_hass, health):
    ollama(lambda url, body: Resp(200, {"embeddings": [[0.5]]}))
    emb._EMBED_LAST_ERROR[0] = "stale"
    assert await emb.embed_texts(fake_hass, ["x"]) == [[0.5]]
    assert emb._EMBED_LAST_ERROR[0] == ""


async def test_wrong_count_falls_back_to_legacy(emb, ollama, fake_hass, health):
    def route(url, body):
        if url.endswith("/api/embed"):
            return Resp(200, {"embeddings": [[1.0]]})      # one for two inputs
        return Resp(200, {"embedding": [len(body["prompt"]) * 1.0]})
    session = ollama(route)
    assert await emb.embed_texts(fake_hass, ["ab", "abc"]) == [[2.0], [3.0]]
    assert [c[0] for c in session.calls] == [
        BASE + "/api/embed", BASE + "/api/embeddings", BASE + "/api/embeddings"]
    assert session.calls[1][1] == {"model": "nomic-embed-text", "prompt": "ab"}
    assert session.calls[1][2].total == 60


async def test_404_everywhere_reports_pull_hint(emb, ollama, fake_hass, health):
    ollama(lambda url, body: Resp(404, {}), model="tiny-embed")
    assert await emb.embed_texts(fake_hass, ["x"]) is None
    reason = "model 'tiny-embed' not found on Ollama (404) — run: ollama pull tiny-embed"
    assert emb._EMBED_LAST_ERROR[0] == reason
    assert health == [("embeddings", False, reason)]


async def test_empty_200_without_error_text(emb, ollama, fake_hass, health):
    ollama(lambda url, body: Resp(200, {"embeddings": []}) if url.endswith("/api/embed")
           else Resp(500, {}), model="tiny-embed")
    assert await emb.embed_texts(fake_hass, ["x"]) is None
    assert emb._EMBED_LAST_ERROR[0] == (
        "model 'tiny-embed' returned no embeddings — is 'tiny-embed' pulled? "
        "run: ollama pull tiny-embed")


async def test_empty_200_with_error_text(emb, ollama, fake_hass, health):
    ollama(lambda url, body: Resp(200, {"error": "model is loading"}) if url.endswith("/api/embed")
           else Resp(200, {"embedding": []}), model="tiny-embed")
    assert await emb.embed_texts(fake_hass, ["x"]) is None
    assert emb._EMBED_LAST_ERROR[0] == "model 'tiny-embed' returned no embeddings (model is loading)"


async def test_null_json_body_counts_as_unreachable(emb, ollama, fake_hass, health):
    ollama(lambda url, body: Resp(200, None) if url.endswith("/api/embed") else Resp(404, {}))
    assert await emb.embed_texts(fake_hass, ["x"]) is None
    assert emb._EMBED_LAST_ERROR[0] == (
        "Ollama /api/embed unreachable: 'NoneType' object has no attribute 'get'")


async def test_other_status_includes_trimmed_body(emb, ollama, fake_hass, health):
    ollama(lambda url, body: Resp(503, text="Z" * 300) if url.endswith("/api/embed")
           else Resp(404, {}))
    assert await emb.embed_texts(fake_hass, ["x"]) is None
    assert emb._EMBED_LAST_ERROR[0] == "Ollama /api/embed HTTP 503: " + "Z" * 120


async def test_other_status_body_read_failure(emb, ollama, fake_hass, health):
    ollama(lambda url, body: Resp(502, text_exc=RuntimeError("closed"))
           if url.endswith("/api/embed") else Resp(404, {}))
    assert await emb.embed_texts(fake_hass, ["x"]) is None
    assert emb._EMBED_LAST_ERROR[0] == "Ollama /api/embed HTTP 502: "


async def test_unreachable_trims_exception_text(emb, ollama, fake_hass, health):
    ollama(lambda url, body: OSError("E" * 150))
    assert await emb.embed_texts(fake_hass, ["x"]) is None
    assert emb._EMBED_LAST_ERROR[0] == "Ollama /api/embed unreachable: " + "E" * 100
    assert health == [("embeddings", False, "Ollama /api/embed unreachable: " + "E" * 100)]


@pytest.mark.parametrize("second", [
    Resp(404, {}), Resp(500, {}), Resp(200, {"embedding": []}), Resp(200, {}),
    OSError("refused")])
async def test_step_one_reason_wins_over_legacy_detail(emb, second):
    """Step 1 always leaves a reason when it falls through, so the legacy
    step's own wording is never used through _embed_batch."""
    def route(url, body):
        if url.endswith("/api/embed"):
            return Resp(200, {"embeddings": [[1.0]], "error": "partial"})
        return second
    emb._EMBED_LAST_ERROR[0] = "untouched"
    assert await emb._embed_batch(Session(route), BASE, "m", ["a", "b"]) is None
    assert emb._EMBED_LAST_ERROR[0] == "model 'm' returned no embeddings (partial)"


async def test_legacy_stops_at_first_failure(emb):
    session = Session(lambda url, body: Resp(404, {}))
    assert await emb._embed_batch(session, BASE, "m", ["a", "b", "c"]) is None
    assert [c[0] for c in session.calls] == [BASE + "/api/embed", BASE + "/api/embeddings"]


async def test_first_step_success_never_touches_legacy_or_error(emb):
    session = Session(lambda url, body: Resp(200, {"embeddings": [[1.0], [2.0]]}))
    emb._EMBED_LAST_ERROR[0] = "kept"
    assert await emb._embed_batch(session, BASE, "m", ["a", "b"]) == [[1.0], [2.0]]
    assert len(session.calls) == 1 and emb._EMBED_LAST_ERROR[0] == "kept"


async def test_legacy_success_after_step_one_failure_records_no_error(emb):
    def route(url, body):
        if url.endswith("/api/embed"):
            return OSError("down")
        return Resp(200, {"embedding": [0.5]})
    emb._EMBED_LAST_ERROR[0] = ""
    assert await emb._embed_batch(Session(route), BASE, "m", ["a"]) == [[0.5]]
    assert emb._EMBED_LAST_ERROR[0] == ""


async def test_partial_batches_discard_everything(emb, ollama, fake_hass, health):
    calls = {"n": 0}

    def route(url, body):
        if url.endswith("/api/embed"):
            calls["n"] += 1
            if calls["n"] == 1:
                return Resp(200, {"embeddings": [[1.0]] * len(body["input"])})
            return Resp(404, {})
        return Resp(404, {})
    ollama(route)
    assert await emb.embed_texts(fake_hass, ["t"] * 40) is None
    assert health and health[-1][1] is False


async def test_generic_failure_reason_when_none_recorded(emb, monkeypatch, fake_hass, health):
    import homeassistant.helpers.aiohttp_client as ac
    monkeypatch.setattr(emb, "_ollama_base", lambda: BASE)
    monkeypatch.setattr(ac, "async_get_clientsession", lambda hass: object())

    async def nothing(session, base, model, batch):
        return None
    monkeypatch.setattr(emb, "_embed_batch", nothing)
    assert await emb.embed_texts(fake_hass, ["x"]) is None
    assert health == [("embeddings", False, "Ollama embed call returned no vectors")]


async def test_probe_mode_records_nothing(emb, ollama, fake_hass, health):
    ollama(lambda url, body: Resp(404, {}))
    assert await emb.embed_texts(fake_hass, ["x"], _is_probe=True) is None
    ollama(lambda url, body: Resp(200, {"embeddings": [[1.0]]}))
    emb._EMBED_LAST_ERROR[0] = "kept"
    assert await emb.embed_texts(fake_hass, ["x"], _is_probe=True) == [[1.0]]
    assert emb._EMBED_LAST_ERROR[0] == "kept"
    assert health == []


def test_record_health_swallows_errors(emb, load, monkeypatch):
    sh = load("diagnostics.service_health")

    def boom(*a, **k):
        raise RuntimeError("health store down")
    monkeypatch.setattr(sh, "record_usage", boom)
    assert emb._record_health(False, "x") is None


# ── embed_one / probe ────────────────────────────────────────────────────────

async def test_embed_one_unwraps(emb, monkeypatch, fake_hass):
    seen = []

    async def fake(hass, texts, _is_probe=False):
        seen.append((texts, _is_probe))
        return [[9.0, 8.0]] if texts[0] == "ok" else None
    monkeypatch.setattr(emb, "embed_texts", fake)
    assert await emb.embed_one(fake_hass, "ok") == [9.0, 8.0]
    assert await emb.embed_one(fake_hass, "bad", _is_probe=True) is None
    assert seen == [(["ok"], False), (["bad"], True)]


async def test_probe_without_base(emb, monkeypatch, fake_hass):
    monkeypatch.setattr(emb, "_ollama_base", lambda: None)
    assert await emb.probe(fake_hass) == {
        "ok": False, "error": "no Ollama base URL configured (set llm_base_url to your Ollama host)"}


async def test_probe_success(emb, monkeypatch, fake_hass, health):
    asked = []
    monkeypatch.setattr(emb, "_ollama_base", lambda: BASE)
    monkeypatch.setattr(emb, "_model", lambda: "tiny-embed")

    async def one(hass, text, _is_probe=False):
        asked.append((text, _is_probe))
        return [0.1] * 384
    monkeypatch.setattr(emb, "embed_one", one)
    assert await emb.probe(fake_hass) == {"ok": True, "model": "tiny-embed", "base": BASE,
                                          "dim": 384}
    assert asked == [("nova embedding health check", True)]
    assert health == []


async def test_probe_failure_uses_specific_reason(emb, monkeypatch, fake_hass):
    monkeypatch.setattr(emb, "_ollama_base", lambda: BASE)
    monkeypatch.setattr(emb, "_model", lambda: "tiny-embed")

    async def one(hass, text, _is_probe=False):
        emb._EMBED_LAST_ERROR[0] = "connection refused"
        return None
    monkeypatch.setattr(emb, "embed_one", one)
    assert await emb.probe(fake_hass) == {"ok": False, "model": "tiny-embed", "base": BASE,
                                          "error": "connection refused"}


@pytest.mark.parametrize("returned", [None, []])
async def test_probe_failure_generic_reason(emb, monkeypatch, fake_hass, returned):
    monkeypatch.setattr(emb, "_ollama_base", lambda: BASE)
    monkeypatch.setattr(emb, "_model", lambda: "tiny-embed")

    async def one(hass, text, _is_probe=False):
        return returned
    monkeypatch.setattr(emb, "embed_one", one)
    res = await emb.probe(fake_hass)
    assert res["ok"] is False
    assert res["error"] == ("Ollama did not return an embedding — is the embed model "
                            "pulled? Try: ollama pull tiny-embed")


def test_no_aiohttp_session_is_cached_at_import(emb):
    assert "aiohttp" not in vars(emb)
    assert not any(type(v).__name__ == "ClientSession" for v in vars(emb).values())
    assert "jc.embeddings" in sys.modules
