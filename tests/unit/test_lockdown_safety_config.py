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


def test_panel_validation_accepts_only_notify_service_lists(load):
    safety_config = load("safety_config")

    assert safety_config.valid_panel_value(
        "notify_services",
        '["notify.mobile_app_abi", "notify.mobile_app_rachel"]',
    ) is True
    assert safety_config.valid_panel_value("notify_services", "[]") is True
    for value in (
        "not json",
        '{}',
        '["light.kitchen"]',
        '["notify.good", 4]',
        '["notify.foo.bar"]',
        '["notify. "]',
        '["notify.Mobile_App"]',
        True,
        None,
    ):
        assert safety_config.valid_panel_value("notify_services", value) is False
