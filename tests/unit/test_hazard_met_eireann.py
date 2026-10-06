"""Met Éireann warnings (8.8.0), tested with Met Éireann's own example files."""
from __future__ import annotations

import json
import pathlib

import pytest

from fakes import FakeHass

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "met_eireann"
ORANGE_WIND = "2.49.0.1.372.0.260908081755.17b4d568-792a-4a9e-87a4-d5e357cfac23"
ORANGE_RAIN = "2.49.0.1.372.0.260908081755.32d2b236-8081-4586-aea1-3a96167d6dbb"
YELLOW_WIND = "2.49.0.1.372.0.260908075338.2a76f3bc-7047-49bd-8183-0fd47d1650cd"
MARINE_GALE = "2.49.0.1.372.0.260908080655.ec503002-4e09-4e48-9df1-0a23e7410b9a"
ADVISORY = "2.49.0.1.372.0.260908080732.7c813acf-8bbc-4677-b5c6-34e7a48ccf50"
BLIGHT = "2.49.0.1.372.0.260908080833.02cd7071-f785-4be0-a3df-26d3ace3143a"


@pytest.fixture
def met(load):
    return load("hazard_met_eireann")


@pytest.fixture
def cap(load):
    return load("hazard_cap")


def _json(name="warning_IRELAND.json"):
    return json.loads((FIX / "json" / name).read_text(encoding="utf-8"))


def _cap_alerts(cap, *, actual=True):
    """The example CAP files, parsed. They are all status Test; with
    ``actual`` only that one element is changed to Actual."""
    out = []
    for p in sorted((FIX / "cap").glob("2.49.*.xml")):
        raw = p.read_bytes()
        if actual:
            raw = raw.replace(b"<status>Test</status>", b"<status>Actual</status>")
        out.append(cap.parse_alert(cap.parse_xml(raw), "en"))
    return out


# ── the county table ────────────────────────────────────────────────────────

def test_the_county_table_is_the_26_codes_in_met_eireanns_advisory(met):
    # The "Weather Advisory for Ireland" example lists every county.
    advisory = next(w for w in _json("warning_ALL.json") if w["type"] == "Advisory")
    assert set(met.COUNTIES) == set(advisory["regions"])
    assert len(met.COUNTIES) == 26
    assert met.county_name("EI07") == "Dublin" and met.county_name("EI16") == "Limerick"
    assert met.county_name("ei04") == "Cork"
    assert not any(c.startswith("EI8") for c in met.COUNTIES)          # no sea areas


def test_no_northern_ireland_codes_appear_in_the_examples(met):
    regions = {r for w in _json("warning_ALL.json") for r in w["regions"]}
    assert regions - set(met.COUNTIES) == {"EI819", "EI820", "EI821"}   # sea areas only


@pytest.mark.parametrize("home,code", [
    ((53.349, -6.260), "EI07"),     # Dublin city
    ((52.664, -8.627), "EI16"),     # Limerick city
    ((51.897, -8.470), "EI04"),     # Cork city
    ((53.271, -9.049), "EI10"),     # Galway city
    ((54.950, -7.734), "EI06"),     # Letterkenny
])
def test_the_nearest_county_is_picked(met, home, code):
    assert met.nearest_county(*home) == code


def test_saved_counties_win_and_none_means_nearest(met):
    assert met.chosen_counties('["EI16", "EI03", "EI16"]', (53.35, -6.26)) == ["EI16", "EI03"]
    assert met.chosen_counties(["ei07"], None) == ["EI07"]
    assert met.chosen_counties("[]", (53.35, -6.26)) == ["EI07"]
    assert met.chosen_counties("", (53.35, -6.26)) == ["EI07"]
    assert met.chosen_counties('["XX99"]', (53.35, -6.26)) == ["EI07"]   # unknown dropped
    assert met.chosen_counties("not json", None) == []


@pytest.mark.parametrize("country,home,irish", [
    ("IE", None, True),
    ("ie", (40.7, -74.0), True),        # the country setting wins
    ("GB", (54.6, -5.93), False),       # Belfast: Northern Ireland, not covered
    ("US", (53.35, -6.26), False),
    (None, (53.35, -6.26), True),       # no country: the point decides
    ("", (51.9, -8.47), True),
    (None, (51.5, -0.12), False),       # London
    (None, None, False),
])
def test_an_irish_home(met, country, home, irish):
    assert met.in_ireland(country, home) is irish


# ── the JSON format (fallback) ──────────────────────────────────────────────

def test_the_json_example_parses_every_field(met):
    ws = met.from_json(_json(), list(met.COUNTIES))
    first = ws[0]
    raw = _json()[0]
    assert first["id"] == raw["capId"] == "2.49.0.1.372.0.260911102120.1cf4c8e2-25d5-4f1a-badb-40d32b0f7766"
    assert first["type"] == "Wind" and first["level"] == "yellow"
    assert first["onset"] == raw["onset"] and first["expiry"] == raw["expiry"]
    assert first["headline"] == raw["headline"]                 # unaltered
    assert first["description"] == raw["description"]           # unaltered
    assert first["areas"] == ["Leitrim", "Mayo", "Sligo", "Donegal"]
    assert first["source_label"] == "Met Éireann" and first["refs"] == []
    assert [w["type"] for w in ws] == ["Wind", "low-temperature", "Rain"]


def test_json_advisories_and_sea_areas_are_not_warnings(met):
    ws = met.from_json(_json("warning_ALL.json"), list(met.COUNTIES))
    assert [w["type"] for w in ws] == ["Wind", "low-temperature", "Rain"]   # no Gale, Advisory, Blight


def test_json_only_the_chosen_counties(met):
    ws = met.from_json(_json(), ["EI04"])                       # Cork
    assert [w["type"] for w in ws] == ["low-temperature", "Rain"]
    assert ws[0]["areas"] == ["Cork"]
    assert met.from_json(_json(), ["EI07"]) == []               # Dublin: none
    assert met.from_json(_json(), ["EI04", "EI14"])[0]["areas"] == ["Leitrim"]


def test_json_that_is_not_the_list_is_none(met):
    assert met.from_json({"warnings": []}, ["EI07"]) is None
    assert met.from_json([], ["EI07"]) == []                    # a real empty list


# ── the CAP files (primary) ─────────────────────────────────────────────────

def test_the_cap_examples_are_status_test_and_ignored(met, cap):
    assert met.from_cap(_cap_alerts(cap, actual=False), list(met.COUNTIES)) == []


def test_actual_cap_warnings_for_the_chosen_counties(met, cap):
    ws = met.from_cap(_cap_alerts(cap), ["EI03"])               # Clare
    assert {w["id"] for w in ws} == {ORANGE_WIND, ORANGE_RAIN}
    wind = next(w for w in ws if w["id"] == ORANGE_WIND)
    assert wind["type"] == "Wind" and wind["level"] == "orange" and wind["areas"] == ["Clare"]
    assert wind["headline"] == "Status Orange - Wind and Rain warning for Clare, Galway, Mayo"
    assert next(w for w in ws if w["id"] == ORANGE_RAIN)["type"] == "Rain"


def test_cap_advisory_blight_and_sea_area_warnings_are_ignored(met, cap):
    ids = {w["id"] for w in met.from_cap(_cap_alerts(cap), list(met.COUNTIES))}
    assert ids == {ORANGE_WIND, ORANGE_RAIN, YELLOW_WIND}
    assert not ids & {ADVISORY, BLIGHT, MARINE_GALE}


def test_a_border_home_with_two_counties_sees_both(met, cap):
    # Leitrim and Roscommon: the yellow wind warning covers both, as one warning.
    ws = met.from_cap(_cap_alerts(cap), ["EI14", "EI24"])
    assert [w["id"] for w in ws] == [YELLOW_WIND]
    assert ws[0]["areas"] == ["Leitrim", "Roscommon"]
    assert met.from_cap(_cap_alerts(cap), ["EI07"]) == []       # Dublin: none


def test_cap_and_json_share_the_warning_id(met, cap):
    # capId is "the same as for the CAP version", so a warning keeps its id
    # whichever source saw it.
    assert all("." in w["capId"] and w["capId"].startswith("2.49.0.1.372.0.") for w in _json())


# ── fetching: RSS + CAP first, JSON only when the index fails ───────────────

@pytest.fixture
def feeds(met, cap, monkeypatch):
    state = {"rss": None, "json": None}

    async def collect(hass, url, *, lang, user_agent):
        assert url == met.RSS_URL
        return state["rss"]

    async def fetch_bytes(hass, url, *, user_agent, **kw):
        assert url == met.JSON_URL
        if isinstance(state["json"], Exception):
            raise state["json"]
        return state["json"]
    monkeypatch.setattr(cap, "collect", collect)
    monkeypatch.setattr(cap, "fetch_bytes", fetch_bytes)
    return state


async def test_fetch_uses_the_rss_index(met, cap, feeds):
    feeds["rss"] = (True, _cap_alerts(cap))
    complete, ws, via = await met.fetch(FakeHass(), ["EI03"], lang="en", user_agent="ua")
    assert (complete, via) == (True, "rss") and len(ws) == 2


async def test_a_good_empty_rss_list_is_complete(met, cap, feeds):
    feeds["rss"] = (True, [])
    assert await met.fetch(FakeHass(), ["EI03"], lang="en", user_agent="ua") == (True, [], "rss")


async def test_fetch_falls_back_to_json_when_the_index_fails(met, cap, feeds):
    feeds["rss"] = (False, [])
    feeds["json"] = (FIX / "json" / "warning_IRELAND.json").read_bytes()
    complete, ws, via = await met.fetch(FakeHass(), ["EI04"], lang="en", user_agent="ua")
    assert (complete, via) == (True, "json") and len(ws) == 2


async def test_both_failing_is_never_an_empty_complete_list(met, cap, feeds):
    feeds["rss"] = (False, [])
    feeds["json"] = cap.FetchError("HTTP 404")
    assert await met.fetch(FakeHass(), ["EI04"], lang="en", user_agent="ua") == (False, [], "none")
    feeds["json"] = b"<html>404</html>"
    assert await met.fetch(FakeHass(), ["EI04"], lang="en", user_agent="ua") == (False, [], "none")
    feeds["json"] = b'{"not": "a list"}'
    assert await met.fetch(FakeHass(), ["EI04"], lang="en", user_agent="ua") == (False, [], "none")


async def test_a_partial_rss_list_is_used_but_incomplete(met, cap, feeds):
    feeds["rss"] = (False, _cap_alerts(cap))
    complete, ws, via = await met.fetch(FakeHass(), ["EI03"], lang="en", user_agent="ua")
    assert (complete, via) == (False, "rss") and len(ws) == 2
