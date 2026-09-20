"""Automatic lockdown accepts only the literal boolean True."""


def test_only_literal_true_enables_automatic_lockdown(load):
    safety_config = load("safety_config")

    assert safety_config.automatic_lockdown_enabled(
        {"lockdown_auto_on_arm": True}) is True
    for value in (False, "true", "false", 1, 0, None):
        assert safety_config.automatic_lockdown_enabled(
            {"lockdown_auto_on_arm": value}) is False


def test_panel_validation_rejects_non_boolean_automatic_lockdown(load):
    safety_config = load("safety_config")

    assert safety_config.valid_panel_value("lockdown_auto_on_arm", True) is True
    assert safety_config.valid_panel_value("lockdown_auto_on_arm", False) is True
    for value in ("true", "false", 1, 0, None):
        assert safety_config.valid_panel_value(
            "lockdown_auto_on_arm", value) is False
    assert safety_config.valid_panel_value("notify_service", "notify.phone") is True
