"""Coverage for reserved-device field exposure and reservation-lookup load behavior.

These tests guard behavior that is independent of the (now-removed) ?include
feature: _build_device_info copying tier1 fields/tags into the reserve response,
GET /api/runs{,/{id}} exposing those fields via to_reserved_device_info(), and
get_device_reservation_with_entry not loading reserved-device rows (N+1 guard).
"""

import contextlib
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import selectinload

from app.devices.models import Device
from app.runs import service as run_service
from app.runs.service_allocator import _build_device_info
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@contextlib.contextmanager
def _capture_statements(session: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []

    def listener(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    bind = session.bind
    sync_engine = bind.sync_engine if hasattr(bind, "sync_engine") else bind
    event.listen(sync_engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(sync_engine, "before_cursor_execute", listener)


@pytest.mark.db
@pytest.mark.asyncio
async def test_build_device_info_populates_tier1_fields(db_session: AsyncSession, default_host_id: str) -> None:
    created = await create_device(
        db_session,
        host_id=default_host_id,
        name="emu-pixel7-1",
        device_type="emulator",
        connection_type="virtual",
        manufacturer="Google",
        model="Pixel 7",
    )
    # Reload with host eagerly loaded, matching production query pattern.
    result = await db_session.execute(select(Device).where(Device.id == created.id).options(selectinload(Device.host)))
    device = result.scalar_one()
    info = _build_device_info(device, platform_label="Android 14")
    assert info.name == "emu-pixel7-1"
    assert info.device_type == "emulator"
    assert info.connection_type == "virtual"
    assert info.manufacturer == "Google"
    assert info.model == "Pixel 7"


@pytest.mark.db
@pytest.mark.asyncio
async def test_build_device_info_populates_tags(db_session: AsyncSession, default_host_id: str) -> None:
    created = await create_device(
        db_session,
        host_id=default_host_id,
        name="tagged-device",
    )
    created.tags = {"screen_type": "4k", "rack": "A1"}
    await db_session.flush()
    result = await db_session.execute(select(Device).where(Device.id == created.id).options(selectinload(Device.host)))
    device = result.scalar_one()
    info = _build_device_info(device, platform_label="Android")
    assert info.tags == {"screen_type": "4k", "rack": "A1"}


@pytest.mark.db
@pytest.mark.asyncio
async def test_run_detail_devices_expose_tier1_fields(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="run-detail-device",
        device_type="emulator",
        connection_type="virtual",
    )
    run = await create_reserved_run(db_session, name="rd", devices=[device])
    response = await client.get(f"/api/runs/{run.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["devices"][0]["name"] == "run-detail-device"
    assert body["devices"][0]["device_type"] == "emulator"
    assert body["devices"][0]["connection_type"] == "virtual"


@pytest.mark.db
@pytest.mark.asyncio
async def test_run_list_reserved_devices_expose_tier1_fields(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="run-list-device",
        device_type="real_device",
        connection_type="usb",
    )
    await create_reserved_run(db_session, name="rl", devices=[device])
    response = await client.get("/api/runs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert any(
        item["reserved_devices"] and item["reserved_devices"][0]["name"] == "run-list-device" for item in body["items"]
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_reservation_context_lookup_does_not_load_reserved_device_rows(
    db_session: AsyncSession, default_host_id: str
) -> None:
    devices = [
        await create_device(
            db_session,
            host_id=default_host_id,
            name=f"context-{index}",
            operational_state="available",
        )
        for index in range(3)
    ]
    await create_reserved_run(db_session, name="context-run", devices=devices)

    with _capture_statements(db_session) as statements:
        run, entry = await run_service.get_device_reservation_with_entry(db_session, devices[0].id)

    assert run is not None
    assert entry is not None
    device_selects = [statement for statement in statements if "FROM devices" in statement]
    assert device_selects == []
