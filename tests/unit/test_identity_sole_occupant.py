"""Tests for the sole-occupant attribution fix (identity.quick_identify, v6.85.0).

When room resolution is inconclusive but exactly one person is home, the event
is attributed to them even with an area_id present — the fix that unstarves
single-person homes so per-person routines can form.
"""
import pytest


@pytest.fixture
def idm(load):
    return load("identity")


def test_sole_occupant_attributed_even_with_area(idm, monkeypatch, fake_hass):
    unknown = idm.Identification(idm.UNKNOWN, 0.0, "no_room_signal", {})
    monkeypatch.setattr(idm, "resolve", lambda hass, area_id=None: unknown)
    monkeypatch.setattr(idm, "_home_people", lambda hass: ["Username"])
    monkeypatch.setattr(idm, "_cfg", lambda k, d: True)
    ident = idm.quick_identify(fake_hass, area_id="kitchen")
    assert ident.known and ident.person == "Username"       # the fix


def test_room_resolution_wins_when_known(idm, monkeypatch, fake_hass):
    known = idm.Identification("Username2", 0.9, "face", {"Username2": 0.9})
    monkeypatch.setattr(idm, "resolve", lambda hass, area_id=None: known)
    monkeypatch.setattr(idm, "_cfg", lambda k, d: True)
    assert idm.quick_identify(fake_hass, area_id="kitchen").person == "Username2"


def test_multi_person_keeps_room_candidates(idm, monkeypatch, fake_hass):
    cand = idm.Identification(idm.UNKNOWN, 0.4, "room", {"Username": 0.4, "Username2": 0.3})
    monkeypatch.setattr(idm, "resolve", lambda hass, area_id=None: cand)
    monkeypatch.setattr(idm, "_home_people", lambda hass: ["Username", "Username2"])
    monkeypatch.setattr(idm, "_cfg", lambda k, d: True)
    ident = idm.quick_identify(fake_hass, area_id="kitchen")
    assert ident.candidates == {"Username": 0.4, "Username2": 0.3}   # room candidates preserved


def test_disabled_returns_unknown(idm, monkeypatch, fake_hass):
    monkeypatch.setattr(idm, "_cfg", lambda k, d: False)
    assert not idm.quick_identify(fake_hass, area_id="kitchen").known
