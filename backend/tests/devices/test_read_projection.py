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
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceGroup, DeviceGroupMembership, DeviceOperationalState, GroupType
from app.devices.services import presenter as presenter_module
from app.devices.services import read_projection as read_projection_module
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
async def test_paginated_device_list_reads_are_constant_with_page_size(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """A fixed ``limit`` must not turn per-member reads back on: fetching a full
    page of 10 must read exactly as much as fetching a page of 1, aside from the
    pagination ``COUNT`` statement itself."""
    await seed_devices(db_session, host_id=default_host_id, count=1, prefix="page-one")
    with capture_read_statements(db_session) as one:
        response_one = await client.get("/api/devices?limit=10&offset=0")
        assert response_one.status_code == 200
    assert len(response_one.json()["items"]) == 1

    await seed_devices(db_session, host_id=default_host_id, count=9, prefix="page-many")
    with capture_read_statements(db_session) as many:
        response_many = await client.get("/api/devices?limit=10&offset=0")
        assert response_many.status_code == 200
    assert len(response_many.json()["items"]) == 10

    assert len(many) == len(one), f"paginated device-list reads grew: {len(one)} -> {len(many)}"


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
    locked = await device_locking.lock_device_handle(db_session, device.id)
    await IntentService(db_session).register_intents(
        locked=locked,
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
        locked,
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


@pytest.mark.db
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_batch_projection_matches_async_serializer_for_multi_axis_device(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Byte-for-byte parity between the batch-constructed DecisionFacts path and the
    async per-device serializer for a device carrying every fact axis at once
    (reservation, operator recovery-deny intent, live session, remediation entry) —
    the case ``test_batch_serialization_matches_per_device`` does not cover, since
    its fixtures carry no reservation/intent/session. Freezes ``now_utc`` in the
    presenter module so the async path's internal recompute cannot drift from the
    ``now`` threaded into the batch projection at a second boundary."""
    fixed_now = now_utc()
    monkeypatch.setattr(presenter_module, "now_utc", lambda: fixed_now)

    device, _run = await _seed_projected_device(db_session, host_id=db_host.id, prefix="parity")
    [loaded_device] = await _load_with_declared_graph(db_session, [device.id])

    projections = await load_device_read_projections(db_session, [loaded_device], now=fixed_now)
    presenter = DevicePresenterService()

    projected = presenter.serialize_projected_device(loaded_device, projections[device.id])
    per_device = await presenter.serialize_device(db_session, loaded_device)

    assert projected == per_device


@pytest.mark.db
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_projection_reads_the_pack_tables_once(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """The catalog read is the device-list path's only walk of the pack tables.

    It used to walk them twice: ``load_platform_label_map`` added a three-
    statement ``selectinload`` chain over the same rows, for one string per
    platform the catalog was already carrying.
    """
    device, _run = await _seed_projected_device(db_session, host_id=db_host.id, prefix="label-source")
    [loaded_device] = await _load_with_declared_graph(db_session, [device.id])

    with capture_read_statements(db_session) as reads:
        projections = await load_device_read_projections(db_session, [loaded_device], now=now_utc())

    pack_reads = [sql for sql in reads if "driver_pack" in sql.lower()]
    assert len(pack_reads) == 1, f"pack tables read {len(pack_reads)} times: {pack_reads}"
    assert projections[device.id].platform_label == "Android"


@pytest.mark.db
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_incomplete_batch_facts_fail_with_a_named_error(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch loader that skips a device must name itself, not raise a bare KeyError.

    The comprehension indexes three maps by device id. A KeyError from there
    carries only a UUID, so an incident reads as "some dict is missing some
    device" with no way to tell which loader dropped it.
    """
    device, _run = await _seed_projected_device(db_session, host_id=db_host.id, prefix="incomplete")
    [loaded_device] = await _load_with_declared_graph(db_session, [device.id])

    async def _drop_everything(*_args: object, **_kwargs: object) -> dict[object, object]:
        return {}

    monkeypatch.setattr(read_projection_module, "assess_devices_async", _drop_everything)

    with pytest.raises(RuntimeError, match="readiness") as excinfo:
        await load_device_read_projections(db_session, [loaded_device], now=now_utc())
    assert str(device.id) in str(excinfo.value), "the error must name the device that was dropped"
