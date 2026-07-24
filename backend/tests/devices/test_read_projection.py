from __future__ import annotations

import ast
import contextlib
import inspect
import textwrap
import uuid
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import raiseload, selectinload

from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceGroup, DeviceGroupMembership, DeviceOperationalState, GroupType
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import CommandKind, IntentRegistration
from app.devices.services.presenter import DevicePresenterService
from app.devices.services.read_projection import load_device_read_projections
from app.devices.services.recovery_projection import RecoveryBlockKind
from app.lifecycle.services import remediation_log
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record, create_reserved_run

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host
    from app.runs.models import TestRun


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


async def _seed_projected_device(
    db_session: AsyncSession, *, host_id: uuid.UUID, prefix: str
) -> tuple[Device, TestRun]:
    """One device carrying a reservation, an operator recovery-deny intent, a live
    session, and a remediation entry — every fact axis the projection composes."""
    device = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=f"{prefix}-{uuid.uuid4().hex[:8]}",
        connection_target=prefix,
        name=prefix,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
        verified=True,
    )
    run = await create_reserved_run(db_session, name=f"run-{prefix}", devices=[device])
    await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:stop:recovery:{device.id}",
                kind=CommandKind.operator_recovery_deny,
                payload={"allowed": False, "reason": "Operator stopped the node"},
            )
        ],
    )
    db_session.add(Session(session_id=f"sess-{prefix}", device_id=device.id, status=SessionStatus.running))
    await remediation_log.append_attempt(
        db_session,
        device.id,
        source="node_health",
        reason="probe failed",
        settings=FakeSettingsReader(
            {
                "general.lifecycle_recovery_backoff_base_sec": 600,
                "general.lifecycle_recovery_backoff_max_sec": 600,
            }
        ),
    )
    await db_session.commit()
    return device, run


async def _load_with_declared_graph(db_session: AsyncSession, device_ids: list[uuid.UUID]) -> list[Device]:
    """Reload devices through the same graph the list/group callers declare
    (``selectinload(appium_node)`` + ``raiseload("*")``) so the loader is exercised
    exactly as production callers feed it, not against freshly-created ORM objects
    with every relationship still unloaded."""
    stmt = select(Device).where(Device.id.in_(device_ids)).options(selectinload(Device.appium_node), raiseload("*"))
    return list((await db_session.execute(stmt)).scalars().all())


@pytest.mark.db
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_load_device_read_projections_bounded_and_immutable(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    now = now_utc()
    device, run = await _seed_projected_device(db_session, host_id=db_host.id, prefix="projection-one")
    [loaded_device] = await _load_with_declared_graph(db_session, [device.id])

    with capture_read_statements(db_session) as one_read:
        projections = await load_device_read_projections(db_session, [loaded_device], now=now)

    projection = projections[device.id]
    assert projection.operational_state is DeviceOperationalState.busy
    assert projection.reservation is not None
    assert projection.reservation.run_id == run.id
    ladders = await remediation_log.load_ladders(db_session, [device.id])
    assert projection.ladder == ladders[device.id]
    assert projection.recovery.kind is RecoveryBlockKind.operator
    with pytest.raises(FrozenInstanceError):
        projection.platform_label = "changed"  # type: ignore[misc]

    many_device_ids = [device.id]
    for index in range(11):
        extra_device, _ = await _seed_projected_device(
            db_session, host_id=db_host.id, prefix=f"projection-many-{index}"
        )
        many_device_ids.append(extra_device.id)
    many_devices = await _load_with_declared_graph(db_session, many_device_ids)

    with capture_read_statements(db_session) as many_reads:
        many_projections = await load_device_read_projections(db_session, many_devices, now=now)

    assert len(many_projections) == len(many_devices) == 12
    assert len(many_reads) == len(one_read), (
        f"projection reads grew with fleet size: {len(one_read)} -> {len(many_reads)}"
    )
    assert not any("devices.id = " in statement.lower() for statement in many_reads)
