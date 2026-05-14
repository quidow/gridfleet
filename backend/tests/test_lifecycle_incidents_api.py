from datetime import UTC, datetime, timedelta
from typing import Any, cast

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceEvent, DeviceEventType
from tests.helpers import create_device_record

DEVICE_PAYLOAD = {
    "identity_value": "lifecycle-api-device-1",
    "name": "Lifecycle API Device",
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "identity_scheme": "android_serial",
    "identity_scope": "host",
    "os_version": "14",
}


async def _create_device(
    db_session: AsyncSession,
    host_id: str,
    *,
    identity_value: str,
    name: str,
) -> dict[str, Any]:
    device = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=identity_value,
        connection_target=identity_value,
        name=name,
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version=DEVICE_PAYLOAD["os_version"],
    )
    return cast("dict[str, Any]", {"id": str(device.id)})


async def test_lifecycle_incidents_api_lists_recent_fleet_incidents(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_device(db_session, default_host_id, identity_value="lifecycle-api-1", name="Lifecycle One")
    db_session.add_all(
        [
            DeviceEvent(
                device_id=device["id"],
                event_type=DeviceEventType.lifecycle_deferred_stop,
                details={
                    "reason": "ADB not responsive",
                    "detail": "Waiting for the active client session to finish",
                    "summary_state": "deferred_stop",
                    "source": "device_checks",
                },
                created_at=datetime.now(UTC) - timedelta(minutes=2),
            ),
            DeviceEvent(
                device_id=device["id"],
                event_type=DeviceEventType.lifecycle_recovery_backoff,
                details={
                    "reason": "Recovery probe failed",
                    "detail": "Automatic recovery is backing off before the next retry",
                    "summary_state": "backoff",
                    "source": "session_viability",
                    "backoff_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                },
                created_at=datetime.now(UTC) - timedelta(minutes=1),
            ),
            DeviceEvent(
                device_id=device["id"],
                event_type=DeviceEventType.health_check_fail,
                details={"reason": "Low-level event only"},
                created_at=datetime.now(UTC),
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/lifecycle/incidents")
    assert resp.status_code == 200
    data = resp.json()
    items = data["items"]
    assert len(items) == 2
    assert items[0]["event_type"] == "lifecycle_recovery_backoff"
    assert items[0]["summary_state"] == "backoff"
    assert items[0]["label"] == "Recovery Backoff"
    assert items[0]["device_name"] == "Lifecycle One"
    assert items[1]["event_type"] == "lifecycle_deferred_stop"


async def test_lifecycle_incidents_api_filters_by_device(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device_one = await _create_device(
        db_session, default_host_id, identity_value="lifecycle-api-2", name="Lifecycle Two"
    )
    device_two = await _create_device(
        db_session, default_host_id, identity_value="lifecycle-api-3", name="Lifecycle Three"
    )
    db_session.add_all(
        [
            DeviceEvent(
                device_id=device_one["id"],
                event_type=DeviceEventType.lifecycle_run_excluded,
                details={
                    "reason": "Health probe failed",
                    "detail": "Excluded from Nightly Run",
                    "summary_state": "excluded",
                    "run_id": "11111111-1111-1111-1111-111111111111",
                    "run_name": "Nightly Run",
                },
                created_at=datetime.now(UTC) - timedelta(minutes=2),
            ),
            DeviceEvent(
                device_id=device_two["id"],
                event_type=DeviceEventType.lifecycle_recovered,
                details={
                    "reason": "Healthy again",
                    "detail": "Device recovered and rejoined automatic management",
                    "summary_state": "idle",
                },
                created_at=datetime.now(UTC) - timedelta(minutes=1),
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/lifecycle/incidents", params={"device_id": device_one["id"]})
    assert resp.status_code == 200
    data = resp.json()
    items = data["items"]
    assert len(items) == 1
    assert items[0]["device_id"] == device_one["id"]
    assert items[0]["event_type"] == "lifecycle_run_excluded"
    assert items[0]["run_name"] == "Nightly Run"
