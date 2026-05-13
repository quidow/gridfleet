import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import settings_service as settings_module


def test_settings_service_validation_and_normalization_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    service = settings_module.SettingsService()
    service._defaults = {"notifications.toast_events": ["host.status_changed"]}

    assert service._normalize_value("notifications.toast_events", ["device.availability_changed"]) == [
        "device.operational_state_changed",
        "device.hold_changed",
    ]
    assert service._normalize_value("notifications.toast_events", ["unknown.event"]) == ["host.status_changed"]

    assert "Expected boolean" in (service._validate_value("general.leader_keepalive_enabled", "true") or "")
    assert "Expected string" in (service._validate_value("grid.hub_url", 123) or "")
    assert "not in allowed values" in (
        service._validate_value("notifications.toast_severity_threshold", "verbose") or ""
    )
    assert "Expected list" in (service._validate_value("notifications.toast_events", "host.status_changed") or "")
    assert "item" in (service._validate_value("notifications.toast_events", [""]) or "")
    assert "item" in (service._validate_value("notifications.toast_events", ["unknown.event"]) or "")

    monkeypatch.setattr(settings_module.process_settings, "auth_enabled", True)
    monkeypatch.setattr(settings_module.process_settings, "agent_terminal_token", "")
    assert "GRIDFLEET_AGENT_TERMINAL_TOKEN" in (
        settings_module._cross_field_validate("agent.enable_web_terminal", True) or ""
    )


async def test_settings_service_event_refresh_and_cancel_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    service = settings_module.SettingsService()
    assert await service.handle_system_event(SimpleNamespace(type="other")) is None
    assert await service.refresh_from_store() is None

    async def slow_refresh() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(slow_refresh())
    service._refresh_task = task
    await service._cancel_refresh_task()
    assert service._refresh_task is None

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    service._session_factory = lambda: _Session()
    init = AsyncMock()
    monkeypatch.setattr(service, "initialize", init)
    await service.refresh_from_store()
    init.assert_awaited_once()

    service._refresh_task = None
    monkeypatch.setattr(service, "refresh_from_store", AsyncMock())
    await service.handle_system_event(SimpleNamespace(type="settings.changed"))
    assert service._refresh_task is not None
    service._refresh_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await service._refresh_task
