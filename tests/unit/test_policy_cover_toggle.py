"""Flipping or positioning a cover is gated like opening it (8.49.41).

Only cover.open and cover.open_cover were rated and voice blocked, so a
toggle or a set position (the AI's toggle action, or a plan step) opened a
garage door or gate by voice with no phone tap and no question at all.
Toggling a security switch now rates like turning it off.
"""
import pytest


@pytest.fixture
def pol(load):
    return load("policy")


@pytest.mark.parametrize("service", ["toggle", "set_cover_position"])
def test_a_cover_toggle_or_position_rates_like_opening(pol, service):
    risk, why = pol.classify("cover", service, "cover.garage_door")
    assert (risk, why) == ("medium", f"cover.{service} opens a physical barrier")


@pytest.mark.parametrize("service", ["toggle", "set_cover_position"])
def test_by_voice_it_always_needs_a_phone_tap(pol, fake_hass, service):
    assert pol.requires_confirmation(fake_hass, "cover", service, "cover.gate",
                                     voice=True) is True


@pytest.mark.parametrize("service", ["toggle", "set_cover_position"])
async def test_by_voice_only_a_phone_tap_allows_it(pol, fake_hass, monkeypatch, service):
    asked = []
    vc = pol.__name__.rsplit(".", 1)[0] + ".voice_confirm"
    import sys
    voice_confirm = sys.modules.get(vc)
    if voice_confirm is None:
        import importlib
        voice_confirm = importlib.import_module(vc)

    async def phone_only(hass, question, **kw):
        asked.append(question)
        return "rejected"
    monkeypatch.setattr(voice_confirm, "confirm_via_phone_only_typed", phone_only)
    ok, note, result = await pol.confirm_gate(fake_hass, "cover", service,
                                              "cover.garage_door", voice=True)
    assert (ok, result) == (False, "rejected")
    assert asked and "Voice alone can't unlock or open this" in asked[0]


def test_toggling_a_security_switch_rates_like_turning_it_off(pol):
    assert pol.classify("switch", "toggle", "switch.alarm_siren")[0] == "high"
    assert pol.classify("switch", "toggle", "switch.desk_fan")[0] == "low"


def test_the_optional_voice_confirmation_covers_them_too(load, monkeypatch, fake_hass):
    vc = load("voice_confirm")
    monkeypatch.setattr(vc, "is_enabled", lambda hass: True)
    monkeypatch.setattr(vc, "_cfg", lambda hass, key, default=None: default)
    for service in ("toggle", "set_cover_position"):
        assert vc.action_is_protected(fake_hass, "cover", service, "cover.gate") is True
    assert vc.action_is_protected(fake_hass, "switch", "toggle", "switch.alarm_siren") is True
    assert vc.action_is_protected(fake_hass, "switch", "toggle", "switch.desk_fan") is False


def test_closing_and_lights_stay_free(pol):
    assert pol.classify("cover", "close_cover", "cover.garage_door")[0] == "low"
    assert pol.classify("light", "toggle", "light.hall")[0] == "low"
