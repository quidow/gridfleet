import uuid
from datetime import UTC, datetime
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.models.device_reservation import DeviceReservation
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
    availability_status: str | DeviceAvailabilityStatus = DeviceAvailabilityStatus.offline,
    device_type: str = "real_device",
    connection_type: str | None = None,
    verified: bool = True,
    allow_android_network_identity_resolution: bool = False,
    manufacturer: str | None = None,
    model: str | None = None,
    tags: dict[str, Any] | None = None,
    auto_manage: bool = True,
    ip_address: str | None = None,
    roku_password: str | None = None,
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
    )
    device.availability_status = (
        availability_status
        if isinstance(availability_status, DeviceAvailabilityStatus)
        else DeviceAvailabilityStatus(availability_status)
    )
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
    claimed_device_ids: dict[str, str] | None = None,
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
    claimed_device_ids = claimed_device_ids or {}
    released_at = datetime.now(UTC) if mark_released else None
    for device in devices:
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
        if str(device.id) in claimed_device_ids:
            reservation.claimed_by = claimed_device_ids[str(device.id)]
            reservation.claimed_at = datetime.now(UTC)
        reservations.append(reservation)
    db_session.add_all(reservations)
    await db_session.commit()
    await db_session.refresh(run, attribute_names=["device_reservations"])
    return run
