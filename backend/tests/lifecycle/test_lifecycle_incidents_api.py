from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from app.devices.models import DeviceEvent, DeviceEventType
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

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
                details={"reason": "Node health probe failed", "port": 4723},
                created_at=datetime.now(UTC),
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/lifecycle/incidents")
    assert resp.status_code == 200
    data = resp.json()
    items = data["items"]
    assert len(items) == 3
    assert items[0]["event_type"] == "health_check_fail"
    assert items[0]["label"] == "Health Fail"
    assert items[0]["reason"] == "Node health probe failed"
    # Bare record_event rows carry no summary_state; the serializer defaults to idle.
    assert items[0]["summary_state"] == "idle"
    assert items[1]["event_type"] == "lifecycle_recovery_backoff"
    assert items[1]["summary_state"] == "backoff"
    assert items[1]["label"] == "Waiting to Retry"
    assert items[1]["device_name"] == "Lifecycle One"
    assert items[2]["event_type"] == "lifecycle_deferred_stop"


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


async def test_lifecycle_incidents_api_includes_failure_and_maintenance_events(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_device(db_session, default_host_id, identity_value="lifecycle-api-4", name="Lifecycle Four")
    base = datetime.now(UTC) - timedelta(minutes=20)
    # Details mirror what production writers record for each type; connectivity_restored
    # has no writer today and is included for forward-compatibility.
    included: list[tuple[DeviceEventType, dict[str, Any] | None]] = [
        (DeviceEventType.health_check_fail, {"port": 4723}),
        (DeviceEventType.connectivity_lost, {"reason": "Host offline"}),
        (DeviceEventType.connectivity_restored, None),
        (DeviceEventType.node_crash, {"error": "process exited", "source": "appium", "will_restart": True}),
        (DeviceEventType.node_restart, {"recovered_from": "agent_auto_restart"}),
        (DeviceEventType.maintenance_entered, {"reason": "run escalation"}),
        (DeviceEventType.maintenance_exited, {"reason": "exit maintenance"}),
    ]
    excluded: list[tuple[DeviceEventType, dict[str, Any] | None]] = [
        (DeviceEventType.session_started, None),
        (DeviceEventType.session_ended, None),
        (DeviceEventType.desired_state_changed, None),
        (DeviceEventType.repair_attempted, None),
        (DeviceEventType.hardware_health_changed, None),
    ]
    db_session.add_all(
        [
            DeviceEvent(
                device_id=device["id"],
                event_type=event_type,
                details=details,
                created_at=base + timedelta(minutes=index),
            )
            for index, (event_type, details) in enumerate(included + excluded)
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/lifecycle/incidents", params={"device_id": device["id"]})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["event_type"] for item in items] == [
        "maintenance_exited",
        "maintenance_entered",
        "node_restart",
        "node_crash",
        "connectivity_restored",
        "connectivity_lost",
        "health_check_fail",
    ]
    assert items[0]["label"] == "Maintenance Exited"
    assert items[0]["reason"] == "exit maintenance"
    assert items[1]["label"] == "Maintenance Entered"
    assert items[1]["reason"] == "run escalation"
    assert items[-1]["summary_state"] == "idle"


async def test_lifecycle_incidents_api_policy_scope_excludes_failure_and_maintenance_events(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_device(db_session, default_host_id, identity_value="lifecycle-api-5", name="Lifecycle Five")
    base = datetime.now(UTC) - timedelta(minutes=10)
    events: list[tuple[DeviceEventType, dict[str, Any] | None]] = [
        (DeviceEventType.lifecycle_recovery_failed, {"reason": "recovery gave up", "summary_state": "recovery_failed"}),
        (DeviceEventType.connectivity_lost, {"reason": "Host offline"}),
        (DeviceEventType.lifecycle_recovered, {"reason": "healthy again", "summary_state": "idle"}),
    ]
    db_session.add_all(
        [
            DeviceEvent(
                device_id=device["id"],
                event_type=event_type,
                details=details,
                created_at=base + timedelta(minutes=index),
            )
            for index, (event_type, details) in enumerate(events)
        ]
    )
    await db_session.commit()

    # Default (scope omitted) is the all-17 behavior: State History and existing callers
    # keep seeing the failure event that preceded the recovery.
    resp_default = await client.get("/api/lifecycle/incidents", params={"device_id": device["id"]})
    assert resp_default.status_code == 200
    assert [item["event_type"] for item in resp_default.json()["items"]] == [
        "lifecycle_recovered",
        "connectivity_lost",
        "lifecycle_recovery_failed",
    ]

    # scope=policy restricts to the original 10 lifecycle_* types, so a fixed-size
    # enrichment window (e.g. AttentionCard) can't be starved by a host-wide flap.
    resp_policy = await client.get("/api/lifecycle/incidents", params={"device_id": device["id"], "scope": "policy"})
    assert resp_policy.status_code == 200
    assert [item["event_type"] for item in resp_policy.json()["items"]] == [
        "lifecycle_recovered",
        "lifecycle_recovery_failed",
    ]


async def test_lifecycle_incidents_api_rejects_an_invalid_cursor(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    await _create_device(db_session, default_host_id, identity_value="lifecycle-api-5", name="Lifecycle Five")
    await db_session.commit()

    resp = await client.get("/api/lifecycle/incidents", params={"cursor": "not-a-real-cursor"})
    assert resp.status_code == 422


async def test_lifecycle_incidents_api_rejects_an_unknown_direction(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    await _create_device(db_session, default_host_id, identity_value="lifecycle-api-6", name="Lifecycle Six")
    await db_session.commit()

    resp = await client.get("/api/lifecycle/incidents", params={"direction": "sideways"})
    assert resp.status_code == 422


async def test_lifecycle_incidents_api_round_trips_its_own_cursor(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_device(db_session, default_host_id, identity_value="lifecycle-api-7", name="Lifecycle Seven")
    base = datetime.now(UTC) - timedelta(minutes=10)
    db_session.add_all(
        [
            DeviceEvent(
                device_id=device["id"],
                event_type=DeviceEventType.lifecycle_recovered,
                details={"summary_state": "idle"},
                created_at=base + timedelta(minutes=index),
            )
            for index in range(5)
        ]
    )
    await db_session.commit()

    first = await client.get("/api/lifecycle/incidents", params={"device_id": device["id"], "limit": 2})
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["items"]) == 2
    assert first_body["next_cursor"] is not None

    second = await client.get(
        "/api/lifecycle/incidents",
        params={"device_id": device["id"], "limit": 2, "cursor": first_body["next_cursor"], "direction": "older"},
    )
    assert second.status_code == 200
    second_body = second.json()
    first_ids = {item["id"] for item in first_body["items"]}
    second_ids = {item["id"] for item in second_body["items"]}
    assert first_ids.isdisjoint(second_ids), "the second page repeated a row from the first"
