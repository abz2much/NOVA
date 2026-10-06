"""The generic CAP source (8.8.0): parsing, safety checks and area matching."""
from __future__ import annotations

import asyncio
import pathlib
import sys
import types

import pytest

from fakes import FakeHass

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "met_eireann" / "cap"
UA = "Nova test"


@pytest.fixture
def cap(load):
    return load("hazard_cap")


def _alert(status="Actual", msg_type="Alert", severity="Severe", refs="", areas="",
           ident="id-1", lang_blocks=None, expires="2099-01-01T00:00:00+00:00",
           params="") -> bytes:
    infos = lang_blocks or [("en-GB", "Storm", "Storm warning headline", "Full text.")]
    info_xml = "".join(f"""
  <info>
    <language>{lang}</language><event>{event}</event><severity>{severity}</severity>
    <onset>2026-01-01T10:00:00+00:00</onset><expires>{expires}</expires>
    <headline>{head}</headline><description>{desc}</description>{params}
    {areas}
  </info>""" for lang, event, head, desc in infos)
    references = f"<references>{refs}</references>" if refs else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>{ident}</identifier><sender>s@example.org</sender>
  <sent>2026-01-01T09:00:00+00:00</sent><status>{status}</status>
  <msgType>{msg_type}</msgType><scope>Public</scope>{references}{info_xml}
</alert>""".encode()


_SQUARE = ("<area><areaDesc>Square</areaDesc>"
           "<polygon>53.0,-7.0 54.0,-7.0 54.0,-6.0 53.0,-6.0 53.0,-7.0</polygon></area>")
_CIRCLE = "<area><areaDesc>Ring</areaDesc><circle>53.35,-6.26 10</circle></area>"
_CODE = ("<area><areaDesc>Coded</areaDesc><geocode><valueName>NUTS3</valueName>"
         "<value>IE061</value></geocode></area>")
_NAMED = "<area><areaDesc>Fingal</areaDesc></area>"
HOME = (53.35, -6.26)


def _warnings(cap, raw, home=HOME, codes=(), names=()):
    return cap.to_warnings([cap.parse_alert(cap.parse_xml(raw))], home=home,
                           area_codes=list(codes), area_names=list(names))


# ── XML safety ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    b'<?xml version="1.0"?><!DOCTYPE alert SYSTEM "http://evil/x.dtd"><alert/>',
    b'<?xml version="1.0"?><!doctype alert [<!ENTITY a "aaaa">]><alert>&a;</alert>',
    b'<alert><!ENTITY x SYSTEM "file:///etc/passwd"></alert>',
])
def test_xml_with_a_doctype_or_entity_is_refused(cap, raw):
    with pytest.raises(ValueError):
        cap.parse_xml(raw)


def test_malformed_xml_is_refused_and_a_bom_is_fine(cap):
    with pytest.raises(ValueError):
        cap.parse_xml(b"<alert><unclosed></alert>")
    assert cap._local(cap.parse_xml(b"\xef\xbb\xbf<alert/>").tag) == "alert"


# ── parsing ─────────────────────────────────────────────────────────────────

def test_a_real_met_eireann_cap_file_parses_every_field(cap):
    raw = (FIX / "2.49.0.1.372.0.260908081755.17b4d568-792a-4a9e-87a4-d5e357cfac23.xml").read_bytes()
    a = cap.parse_alert(cap.parse_xml(raw), "en")
    assert a["identifier"] == "2.49.0.1.372.0.260908081755.17b4d568-792a-4a9e-87a4-d5e357cfac23"
    assert a["sender"] == "forecasts@met.ie" and a["status"] == "Test" and a["msg_type"] == "Alert"
    assert a["event"] == "Orange Wind" and a["severity"] == "Severe"
    assert a["onset"] == "2026-09-09T12:00:00+01:00" and a["expires"] == "2026-09-10T12:00:00+01:00"
    assert a["headline"] == "Status Orange - Wind and Rain warning for Clare, Galway, Mayo"
    assert a["description"].startswith("<p>&nbsp;</p><p>Prepare for impacts:</p><ul><li>")
    assert a["params"] == {"awareness_type": "1; Wind", "awareness_level": "3; orange; Severe"}
    assert a["areas"][0]["desc"] == "Clare, Galway, Mayo"
    assert a["areas"][0]["geocodes"] == [("FIPS", "EI10"), ("FIPS", "EI20"), ("FIPS", "EI03")]
    assert cap.level_of(a) == "orange"


@pytest.mark.parametrize("severity,level", [("Moderate", "yellow"), ("Severe", "orange"),
                                            ("Extreme", "red"), ("Minor", None),
                                            ("Unknown", None)])
def test_severity_maps_to_levels_and_minor_is_ignored(cap, severity, level):
    a = cap.parse_alert(cap.parse_xml(_alert(severity=severity, areas=_SQUARE)))
    assert cap.level_of(a) == level
    assert len(_warnings(cap, _alert(severity=severity, areas=_SQUARE))) == (1 if level else 0)


def test_the_awareness_level_parameter_wins_over_severity(cap):
    params = ("<parameter><valueName>awareness_level</valueName>"
              "<value>4; red; Extreme</value></parameter>")
    a = cap.parse_alert(cap.parse_xml(_alert(severity="Moderate", params=params)))
    assert cap.level_of(a) == "red"
    green = params.replace("4; red; Extreme", "1; green; Minor")
    assert cap.level_of(cap.parse_alert(cap.parse_xml(_alert(params=green)))) is None


def test_the_info_block_in_home_assistants_language_is_used(cap):
    raw = _alert(areas=_SQUARE, lang_blocks=[
        ("en-GB", "Storm", "English headline", "English"),
        ("ga-IE", "Stoirm", "Ceannlíne Gaeilge", "Gaeilge")])
    assert cap.parse_alert(cap.parse_xml(raw), "ga")["headline"] == "Ceannlíne Gaeilge"
    assert cap.parse_alert(cap.parse_xml(raw), "en-GB")["headline"] == "English headline"
    assert cap.parse_alert(cap.parse_xml(raw), "de")["headline"] == "English headline"   # first


def test_references_are_identifiers(cap):
    a = cap.parse_alert(cap.parse_xml(_alert(
        refs="s@x,old-1,2026-01-01T00:00:00Z s@x,old-2,2026-01-01T01:00:00Z")))
    assert a["references"] == ["old-1", "old-2"]


def test_an_rss_and_an_atom_index_give_their_links(cap):
    rss = (FIX / "20260908071755.xml").read_bytes()
    links = cap.parse_index(cap.parse_xml(rss))
    assert len(links) == 6 and all(l.startswith("https://cap.met.ie//") for l in links)
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <link rel="alternate" href="https://x.org/page.html"/>
      <link type="application/cap+xml" href="https://x.org/a.xml"/></entry>
      <entry><link href="https://x.org/b.xml"/></entry></feed>"""
    assert cap.parse_index(cap.parse_xml(atom)) == ["https://x.org/a.xml", "https://x.org/b.xml"]
    assert cap.parse_index(cap.parse_xml(b"<other/>")) is None


# ── location matching ───────────────────────────────────────────────────────

def test_a_polygon_containing_the_home_matches(cap):
    ws = _warnings(cap, _alert(areas=_SQUARE))
    assert ws[0]["areas"] == ["Square"] and ws[0]["level"] == "orange"
    assert _warnings(cap, _alert(areas=_SQUARE), home=(51.9, -8.5)) == []


def test_a_circle_containing_the_home_matches(cap):
    assert _warnings(cap, _alert(areas=_CIRCLE))[0]["areas"] == ["Ring"]
    assert _warnings(cap, _alert(areas=_CIRCLE), home=(53.6, -6.26)) == []   # ~28 km away


def test_a_geocode_in_the_users_codes_matches(cap):
    assert _warnings(cap, _alert(areas=_CODE), home=None, codes=["ie061"])[0]["areas"] == ["Coded"]
    assert _warnings(cap, _alert(areas=_CODE), home=None, codes=["IE062"]) == []


def test_an_area_name_in_the_users_names_matches(cap):
    assert _warnings(cap, _alert(areas=_NAMED), home=None, names=[" fingal "])[0]["areas"] == ["Fingal"]


def test_no_match_means_no_alert(cap):
    assert _warnings(cap, _alert(areas=_CODE + _NAMED), home=(51.9, -8.5),
                     codes=["XX"], names=["Cork"]) == []


def test_point_in_polygon_edges(cap):
    square = [(0, 0), (0, 10), (10, 10), (10, 0)]
    assert cap.point_in_polygon(5, 5, square) is True
    assert cap.point_in_polygon(11, 5, square) is False
    assert cap.point_in_polygon(5, -1, square) is False


@pytest.mark.parametrize("status", ["Test", "Exercise", "System", "Draft"])
def test_only_actual_alerts_count(cap, status):
    assert _warnings(cap, _alert(status=status, areas=_SQUARE)) == []


def test_update_and_cancel_keep_their_references(cap):
    upd = _warnings(cap, _alert(msg_type="Update", refs="s,old-1,t", areas=_SQUARE))
    assert upd[0]["msg_type"] == "Update" and upd[0]["refs"] == ["old-1"]
    cancel = _warnings(cap, _alert(msg_type="Cancel", refs="s,old-1,t", areas=_CODE), home=None)
    assert cancel == [{"id": "id-1", "msg_type": "Cancel", "refs": ["old-1"], "source": "cap"}]


# ── fetching ────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, body=b"", headers=None, chunk_delay=0.0):
        self.status, self._body, self.headers = status, body, headers or {}
        self._delay = chunk_delay
        self.content = self

    async def iter_chunked(self, n):
        for i in range(0, len(self._body), n):
            if self._delay:
                await asyncio.sleep(self._delay)
            yield self._body[i:i + n]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.routes[url]


@pytest.fixture
def net(cap, monkeypatch):
    """A stub aiohttp, a fake session and a fake destination check (the real
    one resolves DNS)."""
    aio = types.ModuleType("aiohttp")
    aio.ClientTimeout = lambda total=None: total
    monkeypatch.setitem(sys.modules, "aiohttp", aio)
    st = types.SimpleNamespace(session=_Session({}), checked=[])
    monkeypatch.setattr(sys.modules["homeassistant.helpers.aiohttp_client"],
                        "async_get_clientsession", lambda hass: st.session)
    real_check = cap._check_destination

    def check(url):
        st.checked.append(url)
        from urllib.parse import urlparse
        if urlparse(url).hostname in ("169.254.169.254", "metadata.google.internal"):
            real_check(url)          # the real policy refuses these without DNS
        elif urlparse(url).scheme != "https":
            real_check(url)
    monkeypatch.setattr(cap, "_check_destination", check)
    return st


async def test_a_redirect_is_followed_and_rechecked(cap, net):
    net.session.routes = {
        "https://a.example/feed": _Resp(302, headers={"Location": "https://b.example/feed"}),
        "https://b.example/feed": _Resp(200, b"<alert/>"),
    }
    assert await cap.fetch_bytes(FakeHass(), "https://a.example/feed", user_agent=UA) == b"<alert/>"
    assert net.checked == ["https://a.example/feed", "https://b.example/feed"]
    assert all(kw["allow_redirects"] is False for _u, kw in net.session.calls)
    assert net.session.calls[0][1]["headers"]["User-Agent"] == UA


@pytest.mark.parametrize("target", ["https://169.254.169.254/latest/meta-data",
                                    "https://metadata.google.internal/x",
                                    "http://b.example/feed"])
async def test_a_redirect_to_a_metadata_address_or_to_http_is_refused(cap, net, target):
    net.session.routes = {"https://a.example/feed": _Resp(302, headers={"Location": target})}
    with pytest.raises(cap.FetchError):
        await cap.fetch_bytes(FakeHass(), "https://a.example/feed", user_agent=UA)
    assert len(net.session.calls) == 1        # never fetched


async def test_https_only(cap, net):
    with pytest.raises(cap.FetchError):
        await cap.fetch_bytes(FakeHass(), "http://a.example/feed", user_agent=UA)
    assert net.session.calls == []


async def test_an_oversized_feed_is_refused(cap, net):
    net.session.routes = {"https://a.example/f": _Resp(200, b"x" * (cap.MAX_BYTES + 1))}
    with pytest.raises(cap.FetchError):
        await cap.fetch_bytes(FakeHass(), "https://a.example/f", user_agent=UA)


async def test_a_slow_feed_times_out(cap, net):
    net.session.routes = {"https://a.example/f": _Resp(200, b"x" * 10, chunk_delay=1.0)}
    with pytest.raises(cap.FetchError):
        await cap.fetch_bytes(FakeHass(), "https://a.example/f", user_agent=UA, timeout=0.05)


async def test_a_bad_status_or_too_many_redirects_is_an_error(cap, net):
    net.session.routes = {"https://a.example/f": _Resp(503)}
    with pytest.raises(cap.FetchError):
        await cap.fetch_bytes(FakeHass(), "https://a.example/f", user_agent=UA)
    net.session.routes = {"https://a.example/f": _Resp(302, headers={"Location": "/f"})}
    with pytest.raises(cap.FetchError):
        await cap.fetch_bytes(FakeHass(), "https://a.example/f", user_agent=UA)


async def test_collect_an_index_of_cap_documents(cap, net):
    index = b"""<rss version="2.0"><channel>
      <item><link>https://a.example/1.xml</link></item>
      <item><link>https://a.example/2.xml</link></item></channel></rss>"""
    net.session.routes = {
        "https://a.example/index": _Resp(200, index),
        "https://a.example/1.xml": _Resp(200, _alert(ident="one", areas=_SQUARE)),
        "https://a.example/2.xml": _Resp(200, _alert(ident="two", areas=_SQUARE)),
    }
    complete, alerts = await cap.collect(FakeHass(), "https://a.example/index", lang="en",
                                         user_agent=UA)
    assert complete is True and [a["identifier"] for a in alerts] == ["one", "two"]


async def test_one_failed_link_makes_the_list_incomplete(cap, net):
    index = b"""<rss><channel><item><link>https://a.example/1.xml</link></item>
      <item><link>https://a.example/2.xml</link></item></channel></rss>"""
    net.session.routes = {
        "https://a.example/index": _Resp(200, index),
        "https://a.example/1.xml": _Resp(200, _alert(ident="one", areas=_SQUARE)),
        "https://a.example/2.xml": _Resp(500),
    }
    complete, alerts = await cap.collect(FakeHass(), "https://a.example/index", lang="en",
                                         user_agent=UA)
    assert complete is False and [a["identifier"] for a in alerts] == ["one"]


async def test_more_than_fifty_links_are_capped_and_incomplete(cap, net):
    items = "".join(f"<item><link>https://a.example/{i}.xml</link></item>" for i in range(60))
    net.session.routes = {"https://a.example/index": _Resp(200, f"<rss><channel>{items}</channel></rss>".encode())}
    for i in range(60):
        net.session.routes[f"https://a.example/{i}.xml"] = _Resp(200, _alert(ident=str(i)))
    complete, alerts = await cap.collect(FakeHass(), "https://a.example/index", lang="en",
                                         user_agent=UA)
    assert complete is False and len(alerts) == 50


async def test_a_failed_feed_or_a_doctype_is_never_an_empty_complete_list(cap, net):
    net.session.routes = {"https://a.example/f": _Resp(404)}
    assert await cap.collect(FakeHass(), "https://a.example/f", lang="en", user_agent=UA) == (False, [])
    net.session.routes = {"https://a.example/f": _Resp(200, b"<!DOCTYPE x><alert/>")}
    assert await cap.collect(FakeHass(), "https://a.example/f", lang="en", user_agent=UA) == (False, [])
    net.session.routes = {"https://a.example/f": _Resp(200, b"<html/>")}
    assert await cap.collect(FakeHass(), "https://a.example/f", lang="en", user_agent=UA) == (False, [])
    net.session.routes = {"https://a.example/f": _Resp(200, b"<rss><channel></channel></rss>")}
    assert await cap.collect(FakeHass(), "https://a.example/f", lang="en", user_agent=UA) == (True, [])


def test_the_real_destination_check_refuses_http_and_metadata(cap):
    for url in ("http://feeds.example/x", "https://169.254.169.254/x",
                "https://user:pw@feeds.example/x"):
        with pytest.raises(cap.FetchError):
            cap._check_destination(url)
