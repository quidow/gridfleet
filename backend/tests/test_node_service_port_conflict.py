"""Regression: port-conflict detection uses AgentErrorCode, not message text."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio

from app.services.agent_error_codes import AgentErrorCode
from app.services.node_service import NodePortConflictError, start_remote_temporary_node
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.device import Device
    from app.models.host import Host


@pytest_asyncio.fixture
async def db_with_pending_device(
    db_session: AsyncSession,
    db_host: Host,
) -> AsyncGenerator[tuple[AsyncSession, Device]]:
    from app.services import device_service
    from tests.pack.factories import seed_test_packs

    await seed_test_packs(db_session)
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="port-conflict-001",
        connection_target="port-conflict-001",
        name="Port Conflict",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
    )
    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None
    yield db_session, loaded


@pytest.mark.asyncio
async def test_port_conflict_detected_via_code(
    monkeypatch: pytest.MonkeyPatch,
    db_with_pending_device: tuple[AsyncSession, Device],
) -> None:
    db, device = db_with_pending_device

    class _FakeStartResponse:
        status_code = 409

        def json(self) -> dict[str, Any]:
            return {"detail": {"code": AgentErrorCode.PORT_OCCUPIED.value, "message": "boom"}}

        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError(
                "409",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(409, json=self.json()),
            )

    async def fake_appium_start(*_args: object, **_kwargs: object) -> _FakeStartResponse:
        return _FakeStartResponse()

    monkeypatch.setattr("app.services.node_service.appium_start", fake_appium_start)

    with pytest.raises(NodePortConflictError):
        await start_remote_temporary_node(
            db,
            device,
            port=4723,
            allocated_caps={},
            agent_base="http://host:5100",
            http_client_factory=httpx.AsyncClient,
        )
