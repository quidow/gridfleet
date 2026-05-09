"""D4: stale stop_pending on offline device must not trap recovery."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.models.host import Host
from app.models.session import Session, SessionStatus
from app.services.lifecycle_policy import attempt_auto_recovery, build_lifecycle_policy

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def _mark_device_available(_db: AsyncSession, device: Device) -> None:
    device.operational_state = DeviceOperationalState.available


async def test_stale_stop_pending_cleared_so_recovery_can_proceed(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """D4: offline device with stop_pending=true and no running session must recover.

    Before the fix, attempt_auto_recovery suppresses with
    "Waiting for active client session to finish" and the device is
    permanently stuck offline.
    """
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="stale-stop-pending-1",
        connection_target="stale-stop-pending-1",
        name="Stale Stop Pending Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        auto_manage=True,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        lifecycle_policy_state={
            "stop_pending": True,
            "stop_pending_reason": "Health probe failed",
            "stop_pending_since": "2026-05-09T12:00:00+00:00",
            "last_action": "auto_stop_deferred",
            "last_failure_source": "node_health",
            "last_failure_reason": "Probe failed",
            "recovery_suppressed_reason": None,
        },
    )
    db_session.add(device)
    await db_session.commit()

    # No Session row exists — this is the stale case (session already gone).
    # Patch start_managed_node and the viability probe to success so the
    # recovery path can complete past the stale-clear.
    with (
        patch("app.services.lifecycle_policy.start_managed_node", new=AsyncMock(side_effect=_mark_device_available)),
        patch(
            "app.services.session_viability.run_session_viability_probe",
            new_callable=AsyncMock,
            return_value={
                "status": "passed",
                "last_attempted_at": datetime.now(UTC).isoformat(),
                "last_succeeded_at": datetime.now(UTC).isoformat(),
                "error": None,
                "checked_by": "recovery",
            },
        ),
    ):
        recovered = await attempt_auto_recovery(db_session, device, source="device_checks", reason="Reconnected")

    await db_session.refresh(device)
    policy = await build_lifecycle_policy(db_session, device)

    # stop_pending must be cleared — device should not be stuck suppressed.
    assert policy.get("stop_pending") is False, "stop_pending must be cleared by recovery"
    assert policy.get("stop_pending_reason") is None

    # The device should NOT be stuck on the stop_pending suppression branch.
    assert policy.get("recovery_suppressed_reason") != "Waiting for active client session to finish", (
        "Recovery must not suppress with stop_pending reason when there is no running session"
    )

    # Recovery should have succeeded (device is now available after our mock).
    assert recovered is True, "Recovery should proceed and succeed when stop_pending is stale"


async def test_stop_pending_not_cleared_when_live_session_exists(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """D4 negative path: stop_pending must NOT be cleared while a client session is running.

    When a live Session row exists the stale-clear guard should be skipped and
    attempt_auto_recovery must return False with the stop_pending suppression reason.
    """
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="stale-stop-pending-2",
        connection_target="stale-stop-pending-2",
        name="Live Session Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        auto_manage=True,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        lifecycle_policy_state={
            "stop_pending": True,
            "stop_pending_reason": "Health probe failed",
            "stop_pending_since": "2026-05-09T12:00:00+00:00",
            "last_action": "auto_stop_deferred",
            "last_failure_source": "node_health",
            "last_failure_reason": "Probe failed",
            "recovery_suppressed_reason": None,
        },
    )
    db_session.add(device)
    await db_session.flush()

    # A live client session is still running — stop_pending is NOT stale.
    live_session = Session(
        session_id="sess-live-stop-pending",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(live_session)
    await db_session.commit()

    recovered = await attempt_auto_recovery(db_session, device, source="device_checks", reason="Reconnected")

    await db_session.refresh(device)
    policy = await build_lifecycle_policy(db_session, device)

    # stop_pending must still be set — the live session guards it.
    assert policy.get("stop_pending") is True, "stop_pending must not be cleared while a session is running"

    # Recovery must be suppressed with the stop_pending reason.
    assert policy.get("recovery_suppressed_reason") == "Waiting for active client session to finish", (
        "Recovery must suppress with stop_pending reason while a live session exists"
    )

    assert recovered is False, "attempt_auto_recovery must return False when stop_pending is guarded by a live session"
