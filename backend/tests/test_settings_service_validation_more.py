import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import settings_service as settings_module
from app.services.settings_registry import SettingDefinition


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


async def test_settings_service_remaining_validation_and_update_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    service = settings_module.SettingsService()

    class EmptyResult:
        def scalars(self) -> "EmptyResult":
            return self

        def all(self) -> list[object]:
            return []

    class EmptySession:
        async def execute(self, *_args: object, **_kwargs: object) -> EmptyResult:
            return EmptyResult()

    await service.initialize(EmptySession())  # type: ignore[arg-type]

    task = asyncio.create_task(asyncio.sleep(10))
    service._refresh_task = task
    service._session_factory = lambda: None
    await service.handle_system_event(SimpleNamespace(type="settings.changed"))
    assert service._refresh_task is task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(KeyError, match="Unknown setting"):
        service.get("missing.setting")
    with pytest.raises(KeyError, match="Unknown setting"):
        await service.reset(AsyncMock(), "missing.setting")
    with pytest.raises(KeyError, match="Unknown setting"):
        await service.bulk_update(AsyncMock(), {"missing.setting": 1})

    assert "JSON-serializable" in (service._validate_value("notifications.toast_events", [object()]) or "")
    assert "Unknown item" in (service._validate_value("notifications.toast_events", ["../private.event"]) or "")
    monkeypatch.setitem(
        settings_module.SETTINGS_REGISTRY,
        "test.json_string_list",
        SettingDefinition(
            key="test.json_string_list",
            category="test",
            setting_type="json",
            default=[],
            description="test only",
            json_list_item_type="string",
            reject_item_prefixes=["../"],
        ),
    )
    service._cache["test.json_string_list"] = ["ok"]
    assert "Expected list" in (service._validate_value("test.json_string_list", "bad") or "")
    assert "Invalid item" in (service._validate_value("test.json_string_list", [""]) or "")
    assert "../bad" in (service._validate_value("test.json_string_list", ["../bad"]) or "")

    row = SimpleNamespace(value=None)

    class Result:
        def scalar_one_or_none(self) -> object:
            return row

    class UpdateSession:
        def __init__(self) -> None:
            self.committed = False

        async def execute(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

        async def commit(self) -> None:
            self.committed = True

    monkeypatch.setattr(settings_module, "_queue_settings_changed", lambda *_args, **_kwargs: None)
    db = UpdateSession()
    response = await service.update(db, "general.device_check_interval_sec", 11)  # type: ignore[arg-type]
    assert response["value"] == 11
    assert row.value == 11
    assert db.committed is True

    bulk_response = await service.bulk_update(db, {"general.device_check_interval_sec": 12})  # type: ignore[arg-type]
    assert bulk_response[0]["value"] == 12
    assert row.value == 12

    validation = service.get_setting_response("notifications.toast_events")["validation"]
    assert validation["item_allowed_values"]
    assert service.get_setting_response("test.json_string_list")["validation"] == {"item_type": "string"}
