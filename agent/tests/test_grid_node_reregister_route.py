from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.main import app, appium_mgr

if TYPE_CHECKING:
    from agent_app.grid_node.supervisor import GridNodeSupervisorHandle


class _FakeGridNodeService:
    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        self.calls: list[dict[str, object]] = []

    def slot_stereotype_caps(self) -> dict[str, object]:
        return {"platformName": "Android", "appium:udid": "device-1", "gridfleet:run_id": "free"}

    async def reregister_with_stereotype(
        self, *, new_caps: dict[str, object], drain_grace_sec: float | None = None
    ) -> None:
        self.calls.append(new_caps)


class _FakeSupervisorHandle:
    def __init__(self, service: _FakeGridNodeService) -> None:
        self.service = service


@pytest.fixture(autouse=True)
def clear_grid_supervisors() -> None:
    appium_mgr._grid_supervisors.clear()


@pytest.mark.asyncio
async def test_reregister_route_invokes_grid_node_service() -> None:
    node_id = str(uuid4())
    target = uuid4()
    service = _FakeGridNodeService(node_id)
    appium_mgr._grid_supervisors[4723] = cast("GridNodeSupervisorHandle", _FakeSupervisorHandle(service))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/grid/node/{node_id}/reregister", json={"target_run_id": str(target)})

    assert resp.status_code == 200
    assert resp.json() == {"grid_run_id": str(target)}
    assert service.calls == [{"platformName": "Android", "appium:udid": "device-1", "gridfleet:run_id": str(target)}]


@pytest.mark.asyncio
async def test_reregister_route_with_null_target_sets_free_stereotype() -> None:
    node_id = str(uuid4())
    service = _FakeGridNodeService(node_id)
    appium_mgr._grid_supervisors[4723] = cast("GridNodeSupervisorHandle", _FakeSupervisorHandle(service))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/grid/node/{node_id}/reregister", json={"target_run_id": None})

    assert resp.status_code == 200
    assert resp.json() == {"grid_run_id": None}
    assert service.calls == [{"platformName": "Android", "appium:udid": "device-1", "gridfleet:run_id": "free"}]


@pytest.mark.asyncio
async def test_reregister_route_returns_404_for_unknown_node() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/grid/node/{uuid4()}/reregister", json={"target_run_id": None})

    assert resp.status_code == 404
    assert cast("dict[str, Any]", resp.json()["detail"])["code"] == "DEVICE_NOT_FOUND"
