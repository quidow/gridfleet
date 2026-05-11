import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import ConnectionType, Device, DeviceHold, DeviceOperationalState, DeviceType
from app.models.device_reservation import DeviceReservation
from app.models.host import Host, HostStatus, OSType
from app.models.test_run import RunState, TestRun

DEFAULT_HOST_PAYLOAD = {
    "hostname": "test-host",
    "ip": "10.0.0.20",
    "os_type": "linux",
    "agent_port": 5100,
}


async def create_host(client: AsyncClient, **overrides: object) -> dict[str, Any]:
    payload = {**DEFAULT_HOST_PAYLOAD, **overrides}
    response = await client.post("/api/hosts", json=payload)
    assert response.status_code == 201
    return dict(response.json())


async def create_device(
    db_session: AsyncSession,
    *,
    host_id: str | uuid.UUID,
    name: str,
    identity_value: str | None = None,
    **overrides: object,
) -> Device:
    """Convenience wrapper around ``create_device_record`` for tests that don't care about identity_value."""
    resolved_identity = identity_value if identity_value is not None else f"auto-{uuid.uuid4().hex[:12]}"
    return await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=resolved_identity,
        name=name,
        **cast("dict[str, Any]", overrides),
    )


async def create_device_record(
    db_session: AsyncSession,
    *,
    host_id: str | uuid.UUID,
    identity_value: str,
    name: str,
    pack_id: str = "appium-uiautomator2",
    platform_id: str = "android_mobile",
    identity_scheme: str = "android_serial",
    identity_scope: str = "host",
    os_version: str = "14",
    connection_target: str | None = None,
    operational_state: str | DeviceOperationalState = DeviceOperationalState.offline,
    hold: str | DeviceHold | None = None,
    device_type: str = "real_device",
    connection_type: str | None = None,
    verified: bool = True,
    manufacturer: str | None = None,
    model: str | None = None,
    tags: dict[str, Any] | None = None,
    auto_manage: bool = True,
    ip_address: str | None = None,
    roku_password: str | None = None,
    test_data: dict[str, Any] | None = None,
    **overrides: object,
) -> Device:
    resolved_device_type = DeviceType(device_type)
    if connection_type is not None:
        resolved_connection_type = ConnectionType(connection_type)
    elif resolved_device_type in (DeviceType.emulator, DeviceType.simulator):
        resolved_connection_type = ConnectionType.virtual
    elif platform_id in {"roku_network", "tvos"}:
        resolved_connection_type = ConnectionType.network
    else:
        resolved_connection_type = ConnectionType.usb

    resolved_connection_target = connection_target if connection_target is not None else identity_value

    # Resolve ip_address from connection target if not supplied.
    resolved_ip_address: str | None = ip_address
    if (
        resolved_ip_address is None
        and resolved_connection_type == ConnectionType.network
        and resolved_connection_target
        and ":" in resolved_connection_target
    ):
        # Attempt to extract host portion from "host:port" target.
        head = resolved_connection_target.split(":")[0]
        if head and head.replace(".", "").isdigit():
            resolved_ip_address = head

    if resolved_connection_type == ConnectionType.virtual:
        resolved_ip_address = None

    device_config: dict[str, Any] = {}
    if roku_password:
        device_config["roku_password"] = roku_password
        device_config["appium_caps"] = {"appium:password": roku_password}

    resolved_test_data: dict[str, Any] = dict(test_data) if test_data else {}

    # Allow caller to override any of the resolved fields explicitly.
    extra = dict(overrides)

    device = Device(
        pack_id=pack_id,
        platform_id=platform_id,
        identity_scheme=identity_scheme,
        identity_scope=identity_scope,
        identity_value=identity_value,
        connection_target=resolved_connection_target,
        name=name,
        os_version=os_version,
        host_id=uuid.UUID(str(host_id)) if not isinstance(host_id, uuid.UUID) else host_id,
        device_type=resolved_device_type,
        connection_type=resolved_connection_type,
        ip_address=resolved_ip_address,
        manufacturer=manufacturer,
        model=model,
        tags=tags,
        auto_manage=auto_manage,
        device_config=device_config,
        test_data=resolved_test_data,
    )
    device.operational_state = (
        operational_state
        if isinstance(operational_state, DeviceOperationalState)
        else DeviceOperationalState(operational_state)
    )
    device.hold = hold if isinstance(hold, DeviceHold) or hold is None else DeviceHold(hold)
    if verified:
        device.verified_at = datetime.now(UTC)

    # Apply any remaining overrides directly on the Device instance.
    for field, value in extra.items():
        setattr(device, field, value)

    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)
    return device


async def create_reserved_run(
    db_session: AsyncSession,
    *,
    name: str,
    devices: list[Device],
    state: RunState = RunState.active,
    requirements: list[dict[str, Any]] | None = None,
    ttl_minutes: int = 60,
    heartbeat_timeout_sec: int = 120,
    created_by: str | None = None,
    excluded_device_ids: set[str] | None = None,
    exclusion_reason: str | None = None,
    mark_released: bool = False,
) -> TestRun:
    run = TestRun(
        name=name,
        state=state,
        requirements=requirements or [{"platform_id": devices[0].platform_id, "count": len(devices)}],
        ttl_minutes=ttl_minutes,
        heartbeat_timeout_sec=heartbeat_timeout_sec,
        created_by=created_by,
    )
    db_session.add(run)
    await db_session.flush()

    reservations: list[DeviceReservation] = []
    excluded_device_ids = excluded_device_ids or set()
    released_at = datetime.now(UTC) if mark_released else None
    for device in devices:
        if released_at is None:
            device.hold = DeviceHold.reserved
        reservation = DeviceReservation(
            run=run,
            device_id=device.id,
            identity_value=device.identity_value,
            connection_target=device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            platform_label=None,
            os_version=device.os_version,
            host_ip=None,
            excluded=str(device.id) in excluded_device_ids,
            exclusion_reason=exclusion_reason if str(device.id) in excluded_device_ids else None,
            excluded_at=datetime.now(UTC) if str(device.id) in excluded_device_ids else None,
            released_at=released_at,
        )
        reservations.append(reservation)
    db_session.add_all(reservations)
    await db_session.commit()
    await db_session.refresh(run, attribute_names=["device_reservations"])
    return run


async def settle_after_commit_tasks() -> None:
    """Drain after_commit-created publish tasks so contract assertions run after dispatch.

    The previous implementation yielded the event loop twice. That was sufficient
    in the common case but raced under load: when multiple host sessions commit
    concurrently (heartbeat cascade tests), each one schedules its own
    ``loop.create_task(_publish_pending_events(...))`` and two yields can return
    before all of those tasks reach their first append, leaving
    ``event_bus_capture`` empty when the assertion runs. Waiting on the
    ``event_bus._handler_tasks`` set is deterministic — it only returns after
    every queued publish task has completed.
    """
    from app.services.event_bus import event_bus

    # Yield once so any after_commit hooks fired during the just-completed
    # await have a chance to call ``loop.create_task`` and register on
    # ``_handler_tasks`` before we snapshot the set.
    await asyncio.sleep(0)
    pending = {task for task in event_bus._handler_tasks if not task.done()}
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def seed_host_and_device(
    db_session: AsyncSession,
    *,
    identity: str,
    operational_state: DeviceOperationalState = DeviceOperationalState.available,
    hold: DeviceHold | None = None,
) -> tuple[Host, Device]:
    """Seed a Host + a single Device on it. Used by event-bus contract tests."""
    host = Host(
        hostname=f"host-{identity}",
        ip="10.0.0.99",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value=identity,
        name=f"Device {identity}",
        operational_state=operational_state,
        hold=hold,
    )
    return host, device


async def seed_host_and_running_node(
    db_session: AsyncSession,
    *,
    identity: str,
    port: int = 4730,
) -> tuple[Host, Device, AppiumNode]:
    """Seed Host + Device + AppiumNode in running state. Used for crash/restart tests."""
    host, device = await seed_host_and_device(db_session, identity=identity)
    node = AppiumNode(
        device_id=device.id,
        port=port,
        grid_url="http://hub.invalid:4444",
        pid=12345,
        active_connection_target=device.connection_target,
        desired_state=AppiumDesiredState.running,
        desired_port=port,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(node)
    return host, device, node


async def seed_host_with_devices(
    db_session: AsyncSession,
    *,
    count: int,
    identity_prefix: str,
) -> tuple[Host, list[Device]]:
    """Seed a Host plus N devices on it. Used for heartbeat host-offline cascade tests."""
    host = Host(
        hostname=f"host-{identity_prefix}",
        ip="10.0.0.99",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    devices: list[Device] = []
    for i in range(count):
        identity = f"{identity_prefix}-{i}"
        device = await create_device_record(
            db_session,
            host_id=host.id,
            identity_value=identity,
            name=f"Device {identity}",
            operational_state=DeviceOperationalState.available,
        )
        devices.append(device)
    return host, devices
