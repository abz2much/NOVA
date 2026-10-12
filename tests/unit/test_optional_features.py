"""Optional features are off until set up, and the AI is not offered their
tools before then (8.41.0)."""
import pytest

from conftest import FakeHass, _install_nova_runtime


@pytest.fixture
def of(load, monkeypatch):
    mod = load("optional_features")
    saved = {}
    monkeypatch.setattr(load("nova_config"), "get", lambda k, d=None: saved.get(k, d))
    mod.saved = saved
    return mod


ALL_OPTIONAL = {"read_email", "ask_executive_assistant", "ask_marketing_agent",
                "ask_security_privacy_agent", "ask_homelab_infra_agent",
                "ask_house_manager_agent", "wellbeing_context", "web_research",
                "who_do_you_see", "look_at_camera", "where_last_seen", "what_changed",
                "energy_status", "solar_status", "energy_report", "energy_outlook"}


def test_a_new_install_offers_none_of_them(of):
    assert of.tools_not_set_up(FakeHass()) == ALL_OPTIONAL
    assert not any(of.status(FakeHass()).values())


def test_every_listed_tool_exists(of, load):
    names = {t["function"]["name"] for t in load("agent_runtime.tool_specs").NOVA_TOOLS}
    assert ALL_OPTIONAL <= names


def test_each_feature_is_offered_once_set_up(of):
    hass = FakeHass()
    of.saved.update({"imap_enabled": True, "imap_host": "mail.example.com",
                     "n8n_webhook_base_url": "http://n8n.local", "biometrics_enabled": True,
                     "search_backend": "duckduckgo", "recognition_source": "frigate",
                     "host_health_enabled": True, "voice_id_uri": "tcp://voice.local:10300"})
    hass.states.set("camera.porch", "idle")
    hass.states.set("sensor.house_power", "500", device_class="power")
    assert of.tools_not_set_up(hass) == frozenset()
    assert all(of.status(hass).values())


def test_email_needs_a_server_as_well(of):
    of.saved["imap_enabled"] = True
    assert not of.is_set_up(FakeHass(), "email")


def test_the_live_panel_value_wins(of):
    hass = FakeHass()
    _install_nova_runtime(hass, {"search_backend": "searxng"})
    of.saved["search_backend"] = "off"
    assert of.is_set_up(hass, "web_research")


def test_a_failing_check_never_hides_a_tool(of, monkeypatch):
    def boom(hass):
        raise RuntimeError("x")
    monkeypatch.setitem(of.FEATURES, "cameras", (boom, of.FEATURES["cameras"][1]))
    assert of.is_set_up(FakeHass(), "cameras")


def test_the_conversation_filters_the_offered_tools():
    import pathlib
    loop = (pathlib.Path(__file__).resolve().parents[2]
            / "custom_components/nova/agent_runtime/loop.py").read_text(encoding="utf-8")
    assert "optional_features.tools_not_set_up(hass)" in loop


async def test_web_research_does_nothing_until_set_up(load, monkeypatch):
    wr = load("web_research")
    monkeypatch.setattr(load("nova_config"), "get", lambda k, d=None: d)
    out = await wr.research(FakeHass(), "anything")
    assert "not set up" in out["error"]


def test_features_without_tools_are_still_reported(of):
    # Host health and Voice ID have no AI tools; the panel marks their cards.
    hass = FakeHass()
    assert of.status(hass)["host_health"] is False
    assert of.status(hass)["voice_id"] is False
    of.saved.update({"host_health_enabled": True, "voice_id_uri": "tcp://voice.local:10300"})
    assert of.status(hass)["host_health"] is True
    assert of.status(hass)["voice_id"] is True
    assert of.tools_not_set_up(hass) == ALL_OPTIONAL


def test_the_panel_marks_cards_from_the_backend_status():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    ws = (root / "custom_components" / "nova" / "websocket.py").read_text()
    assert '"optional_features": optional_features.status(hass),' in ws
    cat = (root / "frontend" / "src" / "panel" / "settings-catalogue.js").read_text()
    for feature in ("voice_id", "host_health", "cameras", "wellbeing"):
        assert f'feature: "{feature}"' in cat
