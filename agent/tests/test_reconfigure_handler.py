from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.appium.exceptions import DeviceNotFoundError
from agent_app.appium.schemas import AppiumStartRequest
from agent_app.main import app


def test_appium_start_request_accepts_orchestration_metadata() -> None:
    run_id = uuid4()

    request = AppiumStartRequest(
        connection_target="device-1",
        port=4723,
        grid_url="http://grid:4444",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        accepting_new_sessions=False,
        stop_pending=True,
        grid_run_id=run_id,
    )

    assert request.accepting_new_sessions is False
    assert request.stop_pending is True
    assert request.grid_run_id == run_id


def test_reconfigure_request_serializes_run_id() -> None:
    from agent_app.appium.schemas import AppiumReconfigureRequest

    run_id = uuid4()
    request: AppiumReconfigureRequest = AppiumReconfigureRequest(
        accepting_new_sessions=False,
        stop_pending=True,
        grid_run_id=run_id,
    )

    assert request.model_dump(mode="json") == {
        "accepting_new_sessions": False,
        "stop_pending": True,
        "grid_run_id": str(run_id),
    }


@pytest.mark.asyncio
async def test_reconfigure_route_invokes_manager() -> None:
    run_id = uuid4()
    with patch("agent_app.appium.appium_mgr.reconfigure", new_callable=AsyncMock) as reconfigure:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/agent/appium/4723/reconfigure",
                json={
                    "accepting_new_sessions": False,
                    "stop_pending": True,
                    "grid_run_id": str(run_id),
                },
            )

    assert response.status_code == 200
    assert response.json() == {
        "port": 4723,
        "accepting_new_sessions": False,
        "stop_pending": True,
        "grid_run_id": str(run_id),
    }
    reconfigure.assert_awaited_once_with(
        4723,
        accepting_new_sessions=False,
        stop_pending=True,
        grid_run_id=run_id,
    )


@pytest.mark.asyncio
async def test_reconfigure_unknown_port_returns_404() -> None:
    with patch(
        "agent_app.appium.appium_mgr.reconfigure",
        new_callable=AsyncMock,
        side_effect=DeviceNotFoundError("No Appium process for port 4723"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/agent/appium/4723/reconfigure",
                json={"accepting_new_sessions": True, "stop_pending": False, "grid_run_id": None},
            )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_health_capabilities_advertise_orchestration_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.host.capabilities import get_capabilities_snapshot

    monkeypatch.setattr(
        "agent_app.host.capabilities._capabilities_snapshot",
        {"platforms": ["android_mobile"], "tools": {}, "missing_prerequisites": []},
    )

    payload: dict[str, Any] = get_capabilities_snapshot()
    assert payload["orchestration_contract_version"] == 2
