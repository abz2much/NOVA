"""The agent's web lookup (web_research.py): off until set up, the two
backends, and the shapers that bound what reaches the model. HTTP is faked
with a stand-in session; nothing leaves the test."""
import sys
import types

import pytest


@pytest.fixture
def wr(load):
    return load("web_research")


class _Resp:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def json(self, content_type="application/json"):
        assert content_type is None
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, status=200, payload=None, exc=None):
        self.status, self.payload, self.exc = status, payload or {}, exc
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if self.exc:
            raise self.exc
        return _Resp(self.status, self.payload)


@pytest.fixture
def net(monkeypatch):
    """A fake shared session and a stand-in aiohttp.ClientTimeout."""
    holder = {"session": _Session()}
    ac = types.ModuleType("homeassistant.helpers.aiohttp_client")
    ac.async_get_clientsession = lambda hass: holder["session"]
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.aiohttp_client", ac)
    aio = types.ModuleType("aiohttp")
    aio.ClientTimeout = lambda total=None: ("timeout", total)
    monkeypatch.setitem(sys.modules, "aiohttp", aio)
    return holder


def settings(wr, monkeypatch, **cfg):
    monkeypatch.setattr(wr, "_cfg", lambda key, default: cfg.get(key, default))


def test_limits(wr):
    assert (wr._MAX_ABSTRACT, wr._MAX_RELATED, wr._TIMEOUT) == (1200, 5, 10)
    assert wr._DDG_ENDPOINT == "https://api.duckduckgo.com/"


# research(): the gate

@pytest.mark.parametrize("query", ["", "   ", None, "\n\t"])
async def test_blank_question(wr, query):
    assert await wr.research(None, query) == {"query": "", "error": "empty query"}


@pytest.mark.parametrize("backend", [None, "", "off", "google"])
async def test_off_until_a_backend_is_chosen(wr, net, monkeypatch, backend):
    settings(wr, monkeypatch, search_backend=backend)
    out = await wr.research(None, "  weather  in   town ")
    assert out == {"query": "weather in town",
                   "error": "web research is not set up — choose a backend in Nova's settings"}
    assert net["session"].calls == []


async def test_cfg_falls_back_on_none_and_errors(wr, load, monkeypatch):
    cfg = load("nova_config")
    monkeypatch.setattr(cfg, "get", lambda key, default=None: None)
    assert wr._cfg("search_backend", "off") == "off"

    def boom(key, default=None):
        raise RuntimeError("no entry")
    monkeypatch.setattr(cfg, "get", boom)
    assert wr._cfg("search_backend", "off") == "off"
    monkeypatch.setattr(cfg, "get", lambda key, default=None: "searxng")
    assert wr._cfg("search_backend", "off") == "searxng"


# DuckDuckGo

async def test_duckduckgo_request_and_result(wr, net, monkeypatch):
    settings(wr, monkeypatch, search_backend="DuckDuckGo")
    net["session"] = _Session(payload={"Answer": "42", "AbstractURL": "https://a"})
    out = await wr.research(object(), "meaning of life")
    url, params, timeout = net["session"].calls[0]
    assert url == "https://api.duckduckgo.com/"
    assert params == {"q": "meaning of life", "format": "json", "no_html": "1",
                      "skip_disambig": "1", "t": "nova_home"}
    assert timeout == ("timeout", 10)
    assert out == {"query": "meaning of life", "answer": "42", "source": "https://a",
                   "source_name": "", "related": [], "backend": "duckduckgo"}


async def test_duckduckgo_http_error(wr, net, monkeypatch):
    settings(wr, monkeypatch, search_backend="duckduckgo")
    net["session"] = _Session(status=503)
    assert await wr.research(None, "x") == {"query": "x", "error": "search returned HTTP 503"}


async def test_network_failure_becomes_an_error(wr, net, monkeypatch):
    settings(wr, monkeypatch, search_backend="duckduckgo")
    net["session"] = _Session(exc=OSError("dns down"))
    assert await wr.research(None, "x") == {"query": "x", "error": "lookup failed: dns down"}


def test_ddg_text_priority(wr):
    assert wr._shape_ddg("q", {"AbstractText": " A ", "Abstract": "B", "Answer": "C"})["answer"] == "A"
    assert wr._shape_ddg("q", {"AbstractText": "", "Abstract": "B", "Answer": "C"})["answer"] == "B"
    assert wr._shape_ddg("q", {"Answer": " ", "Definition": "D"})["answer"] == "D"


def test_ddg_source_fallbacks(wr):
    out = wr._shape_ddg("q", {"Definition": "d", "DefinitionURL": " https://d ",
                              "DefinitionSource": " Dict "})
    assert (out["source"], out["source_name"]) == ("https://d", "Dict")


def test_ddg_related_only(wr):
    out = wr._shape_ddg("q", {"RelatedTopics": [{"Text": "one"}, {"Topics": [{"Text": "x"}]},
                                                "junk", {"Text": ""}, {"Text": "two"}]})
    assert out["answer"] == "" and out["related"] == ["one", "two"]


def test_ddg_related_capped_at_five(wr):
    topics = [{"Text": f"t{i}"} for i in range(9)]
    assert wr._shape_ddg("q", {"RelatedTopics": topics})["related"] == ["t0", "t1", "t2", "t3", "t4"]


def test_ddg_related_looks_at_ten_entries_at_most(wr):
    topics = [{"Topics": []}] * 9 + [{"Text": "tenth"}, {"Text": "eleventh"}]
    assert wr._shape_ddg("q", {"RelatedTopics": topics})["related"] == ["tenth"]


def test_ddg_related_clipped(wr):
    out = wr._shape_ddg("q", {"RelatedTopics": [{"Text": "word " * 100}]})
    r = out["related"][0]
    assert r.endswith("…") and len(r) <= 161 and "  " not in r


def test_ddg_nothing_found(wr):
    for data in (None, {}, {"AbstractText": "", "RelatedTopics": None}):
        assert wr._shape_ddg("q", data) == {
            "query": "q", "error": "no results — try rephrasing, or this may need a full web search"}


def test_long_answer_clipped_on_a_word(wr):
    out = wr._shape_ddg("q", {"AbstractText": "abcdefghij " * 200})
    assert out["answer"].endswith("abcdefghij…") and len(out["answer"]) <= 1201


def test_unbroken_text_is_cut_at_the_limit(wr):
    out = wr._shape_ddg("q", {"AbstractText": "x" * 1500})
    assert out["answer"] == "x" * 1200 + "…"


# SearXNG

async def test_searxng_needs_its_address(wr, net, monkeypatch):
    settings(wr, monkeypatch, search_backend="searxng", searxng_url="")
    assert await wr.research(None, "x") == {"query": "x", "error": "searxng_url not configured"}
    assert net["session"].calls == []


async def test_searxng_request(wr, net, monkeypatch):
    settings(wr, monkeypatch, search_backend="searxng", searxng_url="http://sx.local:8080//")
    net["session"] = _Session(payload={"results": [{"title": "T", "url": "u", "engine": "e"}]})
    out = await wr.research(None, "x")
    url, params, timeout = net["session"].calls[0]
    assert url == "http://sx.local:8080/search"
    assert params == {"q": "x", "format": "json"} and timeout == ("timeout", 10)
    assert out == {"query": "x", "answer": "T", "source": "u", "source_name": "e",
                   "related": [], "backend": "searxng"}


async def test_searxng_http_error(wr, net, monkeypatch):
    settings(wr, monkeypatch, search_backend="searxng", searxng_url="http://sx")
    net["session"] = _Session(status=404)
    assert await wr.research(None, "x") == {"query": "x", "error": "searxng returned HTTP 404"}


def test_searxng_top_result_and_related(wr):
    results = [{"title": "Top", "content": "Body text", "url": "https://top"}] + \
              [{"title": f"R{i}"} for i in range(1, 8)]
    results.insert(2, {"content": "no title"})
    out = wr._shape_searxng("q", {"results": results})
    assert out["answer"] == "Body text" and out["source"] == "https://top"
    assert out["source_name"] == ""
    assert out["related"] == ["R1", "R2", "R3", "R4"]


def test_searxng_instant_answer(wr):
    assert wr._shape_searxng("q", {"answers": ["  7  days "]}) == {
        "query": "q", "answer": "7 days", "source": "", "source_name": "",
        "related": [], "backend": "searxng"}


def test_searxng_nothing(wr):
    for data in (None, {}, {"results": [], "answers": []}):
        assert wr._shape_searxng("q", data) == {"query": "q", "error": "no results"}
