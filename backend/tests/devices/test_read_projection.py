from __future__ import annotations

import ast
import contextlib
import inspect
import textwrap
import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event

from app.devices.models import Device, DeviceGroup, DeviceGroupMembership, GroupType
from app.devices.services.presenter import DevicePresenterService
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@contextlib.contextmanager
def capture_read_statements(session: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []

    def listener(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    bind = session.bind
    assert bind is not None
    engine = bind.sync_engine if hasattr(bind, "sync_engine") else bind
    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", listener)


async def seed_devices(db_session: AsyncSession, *, host_id: str, count: int, prefix: str) -> list[Device]:
    return [
        await create_device_record(
            db_session,
            host_id=host_id,
            identity_value=f"{prefix}-{number}-{uuid.uuid4().hex[:8]}",
            connection_target=f"{prefix}-{number}",
            name=f"{prefix}-{number}",
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            os_version="14",
            verified=True,
        )
        for number in range(count)
    ]


@pytest.mark.db
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_device_list_reads_are_constant_with_fleet_size(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    await seed_devices(db_session, host_id=default_host_id, count=1, prefix="one")
    with capture_read_statements(db_session) as one:
        assert (await client.get("/api/devices")).status_code == 200

    await seed_devices(db_session, host_id=default_host_id, count=12, prefix="many")
    with capture_read_statements(db_session) as many:
        assert (await client.get("/api/devices")).status_code == 200

    assert len(many) == len(one), f"device-list reads grew: {len(one)} -> {len(many)}"
    lowered = "\n".join(many).lower()
    tables = ("devices", "device_intents", "device_remediation_log", "device_reservations", "sessions", "driver_packs")
    for table in tables:
        assert table in lowered


@pytest.mark.db
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_group_detail_reads_are_constant_with_member_count(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    group = DeviceGroup(key=f"projection-{uuid.uuid4().hex[:8]}", name="projection", group_type=GroupType.static)
    db_session.add(group)
    await db_session.flush()
    one = await seed_devices(db_session, host_id=default_host_id, count=1, prefix="group-one")
    db_session.add(DeviceGroupMembership(group_id=group.id, device_id=one[0].id))
    await db_session.commit()
    with capture_read_statements(db_session) as one_member:
        assert (await client.get(f"/api/device-groups/{group.key}")).status_code == 200

    many = await seed_devices(db_session, host_id=default_host_id, count=12, prefix="group-many")
    db_session.add_all(DeviceGroupMembership(group_id=group.id, device_id=device.id) for device in many)
    await db_session.commit()
    with capture_read_statements(db_session) as many_members:
        assert (await client.get(f"/api/device-groups/{group.key}")).status_code == 200

    assert len(many_members) == len(one_member), f"group-detail reads grew: {len(one_member)} -> {len(many_members)}"


def test_projected_device_builder_is_synchronous_and_database_free() -> None:
    source = inspect.getsource(DevicePresenterService.serialize_projected_device)
    tree = ast.parse(textwrap.dedent(source))
    # ast.AsyncFunctionDef is not a subclass of ast.FunctionDef, so this rejects a
    # no-op ``async def`` that would pass the (vacuously true) no-Await check below.
    assert isinstance(tree.body[0], ast.FunctionDef), "serialize_projected_device must be a sync def"
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
    assert "AsyncSession" not in source
    assert list(inspect.signature(DevicePresenterService.serialize_projected_device).parameters) == [
        "self",
        "device",
        "projection",
    ]
