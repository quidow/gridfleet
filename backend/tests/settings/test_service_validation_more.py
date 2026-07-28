import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.settings import service as settings_module
from tests.fakes import FakeSessionFactory
from tests.helpers import test_event_bus as event_bus


def test_settings_service_validation_and_normalization_edges() -> None:
    service = settings_module.SettingsService()

    assert "Expected boolean" in (service._validate_value("agent.auto_accept_hosts", "true") or "")


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
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    service._session_factory = _Session
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
        def scalars(self) -> EmptyResult:
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
        await asyncio.wait_for(task, timeout=1.0)

    with pytest.raises(KeyError, match="Unknown setting"):
        service.get("missing.setting")
    with pytest.raises(KeyError, match="Unknown setting"):
        await service.reset("missing.setting", publisher=event_bus)
    with pytest.raises(KeyError, match="Unknown setting"):
        await service.bulk_update({"missing.setting": 1}, publisher=event_bus)

    row = SimpleNamespace(value=None)

    class Result:
        def scalar_one_or_none(self) -> object:
            return row

    class UpdateSession:
        async def execute(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

    monkeypatch.setattr(settings_module, "_queue_settings_changed", lambda *_args, **_kwargs: None)
    # The boundary is the service's own now, so the fake is the *factory*: its
    # ``begun`` counter is what used to be ``UpdateSession.committed``.
    factory = FakeSessionFactory(UpdateSession())
    service.configure_store_refresh(factory, task_tracker=lambda _task: None)  # type: ignore[arg-type]
    response = await service.update("general.session_viability_timeout_sec", 11, publisher=event_bus)
    assert response["value"] == 11
    assert row.value == 11
    assert factory.begun == 1

    bulk_response = await service.bulk_update({"general.session_viability_timeout_sec": 12}, publisher=event_bus)
    assert bulk_response[0]["value"] == 12
    assert row.value == 12
    assert factory.begun == 2
