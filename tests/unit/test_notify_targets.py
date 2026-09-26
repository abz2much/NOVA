"""Tests for Nova's configured normal notification targets."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_module(monkeypatch, action_log):
    package = types.ModuleType("custom_components.nova")
    package.__path__ = [str(ROOT / "custom_components" / "nova")]
    monkeypatch.setitem(sys.modules, "custom_components.nova", package)
    monkeypatch.setitem(sys.modules, "custom_components.nova.action_log", action_log)
    spec = importlib.util.spec_from_file_location(
        "custom_components.nova.notify_targets",
        ROOT / "custom_components" / "nova" / "notify_targets.py",
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class _ActionLog(types.ModuleType):
    def __init__(self):
        super().__init__("custom_components.nova.action_log")
        self.started = []
        self.executions = []

    def new_request_id(self):
        return "request-1"

    def start_many(self, request_id, action, source, targets, **kwargs):
        self.started.append((request_id, action, source, targets, kwargs))
        return {target["key"]: index + 10 for index, target in enumerate(targets)}

    def set_execution(self, action_id, result, **kwargs):
        self.executions.append((action_id, result, kwargs))


class _Services:
    def __init__(self, failing=None):
        self.failing = set(failing or [])
        self.calls = []

    async def async_call(self, domain, service, payload, blocking=False):
        target = f"{domain}.{service}"
        self.calls.append((target, payload, blocking))
        if target in self.failing:
            raise RuntimeError("phone offline")


class _Hass:
    def __init__(self, failing=None):
        self.services = _Services(failing)
        self.data = {}

    async def async_add_executor_job(self, fn):
        return fn()


def test_selected_services_preserve_legacy_single_target(monkeypatch):
    module = _load_module(monkeypatch, _ActionLog())

    assert module.configured_notify_services({
        "notify_service": "notify.mobile_app_alex",
    }) == ["notify.mobile_app_alex"]


def test_selected_services_parse_json_and_remove_duplicates(monkeypatch):
    module = _load_module(monkeypatch, _ActionLog())

    assert module.configured_notify_services({
        "notify_services": (
            '["notify.mobile_app_alex", "notify.mobile_app_morgan", '
            '"notify.mobile_app_alex", "light.not_a_phone", 4]'
        ),
        "notify_service": "notify.legacy_must_not_return",
    }) == ["notify.mobile_app_alex", "notify.mobile_app_morgan"]


def test_selected_services_reject_malformed_service_identifiers(monkeypatch):
    module = _load_module(monkeypatch, _ActionLog())

    assert module.configured_notify_services({
        "notify_services": [
            "notify.valid_target", "notify.foo.bar", "notify. ",
            "notify.MixedCase",
        ],
    }) == ["notify.valid_target"]


def test_explicit_empty_selection_does_not_restore_legacy_target(monkeypatch):
    module = _load_module(monkeypatch, _ActionLog())

    assert module.configured_notify_services({
        "notify_services": "[]",
        "notify_service": "notify.mobile_app_alex",
    }) == []


@pytest.mark.asyncio
async def test_send_fans_out_and_isolates_device_failure(monkeypatch):
    action_log = _ActionLog()
    module = _load_module(monkeypatch, action_log)
    hass = _Hass(failing={"notify.mobile_app_alex"})
    config = {
        "notify_services": [
            "notify.mobile_app_alex",
            "notify.mobile_app_morgan",
        ]
    }

    sent = await module.async_send_configured_notifications(
        hass,
        config,
        {"title": "Nova", "message": "Test"},
        action="notify",
        source="proactive",
        requested_state="high",
    )

    assert [call[0] for call in hass.services.calls] == [
        "notify.mobile_app_alex",
        "notify.mobile_app_morgan",
    ]
    assert sent == ["notify.mobile_app_morgan"]
    assert action_log.started[0][3] == [
        {
            "key": "notify.mobile_app_alex",
            "domain": "notify",
            "service": "mobile_app_alex",
            "entity_id": None,
            "requested_state": "high",
        },
        {
            "key": "notify.mobile_app_morgan",
            "domain": "notify",
            "service": "mobile_app_morgan",
            "entity_id": None,
            "requested_state": "high",
        },
    ]
    assert action_log.executions == [
        (10, "failed", {"reason_code": "service_call_failed"}),
        (11, "accepted", {}),
    ]


@pytest.mark.asyncio
async def test_runtime_panel_selection_overrides_started_component_config(monkeypatch):
    module = _load_module(monkeypatch, _ActionLog())
    hass = _Hass()
    # The panel selection lives in the loaded entry's NovaRuntime.
    rt = importlib.import_module("custom_components.nova.runtime")
    monkeypatch.setitem(sys.modules, rt.__name__, rt)
    entry = types.SimpleNamespace(
        entry_id="entry-1",
        state=sys.modules["homeassistant.config_entries"].ConfigEntryState.LOADED,
        runtime_data=rt.NovaRuntime(
            client=object(), llm_provider_name="groq", sentinel=object(),
            reminder_watcher=object(), scheduler=object(), resources=object(),
            automation_contexts=object(),
            runtime_config={"notify_services": '["notify.mobile_app_new"]'},
        ),
    )
    hass.config_entries = types.SimpleNamespace(async_entries=lambda domain: [entry])

    await module.async_send_configured_notifications(
        hass,
        {"notify_service": "notify.mobile_app_old"},
        {"title": "Nova", "message": "Test"},
    )

    assert [call[0] for call in hass.services.calls] == [
        "notify.mobile_app_new",
    ]
