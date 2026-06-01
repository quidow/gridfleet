"""§14.4a: exit-maintenance eagerly re-validates via the recovery probe.

Original incident (device 2760001b-d800-4f3b-a67e-edac29a03938): exit-maintenance
registered a verification lease (→ ``operational_state == verifying``) and scheduled
a ``device_recovery`` job, but the recovery viability probe rejected every state
except ``available``/``offline`` — so it raised ``ValueError("...available devices")``,
crashed the recovery job, and the device sat ``verifying`` until the lease's ~210s
``expires_at`` safety net fired.

§14.4a (spec ``2026-05-31-device-operational-state-derivation-design.md``) defines the
intended path as ``maintenance → verifying → available|offline``, with
``_run_recovery_probe → run_session_viability_probe(checked_by=recovery)`` as the
validator. These tests pin the three wiring points that make that path work:

* **A** — the recovery probe *admits* a ``verifying`` device (gate fix).
* **B** — completing the recovery probe *revokes* the verification lease, so the
  post-probe reconcile derives ``available`` (pass) / ``offline`` (fail) instead of
  re-deriving ``verifying``.
* **C** — ``attempt_auto_recovery`` does not short-circuit a ``verifying`` device on
  the "node already healthy" early-return; it runs the full probe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.models.intent import DeviceIntent
from app.devices.services import state_write_guard
from app.devices.services.intent_types import NODE_PROCESS, PRIORITY_AUTO_RECOVERY, verification_intent_source
from app.devices.services.lifecycle_incidents import LifecycleIncidentService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus
from tests.test_session_viability import run_session_viability_probe

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

import pytest

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def _seed_verifying_device(db: AsyncSession, host_id: uuid.UUID, *, identity: str) -> Device:
    """A verified device held ``verifying`` by an active verification lease, node up."""
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=identity,
            connection_target=identity,
            name="Exit-maintenance Re-validation Device",
            os_version="14",
            host_id=host_id,
            operational_state=DeviceOperationalState.verifying,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db.add(device)
    await db.flush()

    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4733,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4733,
            pid=4242,
            active_connection_target="127.0.0.1:4733",
        )
    db.add(node)
    # The lease that exit_maintenance (§14.4a) registers — the source of `verifying`.
    db.add(
        DeviceIntent(
            device_id=device.id,
            source=verification_intent_source(device.id),
            axis=NODE_PROCESS,
            payload={"action": "start", "priority": PRIORITY_AUTO_RECOVERY},
        )
    )
    await db.commit()

    loaded = await db.get(Device, device.id)
    assert loaded is not None
    loaded.appium_node = await db.get(AppiumNode, node.id)
    return loaded


async def test_recovery_probe_admits_verifying_and_clears_lease(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A+B: a ``verifying`` recovery probe runs, passes, revokes the lease, and the
    device derives ``available`` — no ``ValueError``, no lingering ``verifying``."""
    device = await _seed_verifying_device(db_session, db_host.id, identity="repro-verifying-ab")

    with (
        patch(
            "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch(
            "app.sessions.service_viability.SessionViabilityService.probe_session_via_grid",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
    ):
        result = await run_session_viability_probe(db_session, device, checked_by="recovery")

    assert result["status"] == "passed"

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available

    lease = (
        await db_session.execute(
            select(DeviceIntent.id).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == verification_intent_source(device.id),
            )
        )
    ).first()
    assert lease is None, "verification lease must be revoked once the re-validation probe completes"


async def test_attempt_auto_recovery_probes_verifying_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """C: a ``verifying`` device with a running node still reaches the full probe —
    the "node already healthy" early-return must not strand it (§14.4a: full
    validation, not a light ping)."""
    from app.devices.services.lifecycle_policy import LifecyclePolicyService
    from app.devices.services.lifecycle_policy_actions import LifecyclePolicyActionsService
    from app.runs.service_reservation import RunReservationService

    device = await _seed_verifying_device(db_session, db_host.id, identity="repro-verifying-c")

    probe_called: list[bool] = []

    async def _capture_probe(self_arg: object, db: object, dev: object) -> dict[str, Any]:
        probe_called.append(True)
        return {"status": "passed"}

    svc = LifecyclePolicyService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=event_bus, reservation=RunReservationService(), incidents=LifecycleIncidentService()
        ),
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )
    with patch.object(LifecyclePolicyService, "_run_recovery_probe", new=_capture_probe):
        await svc.attempt_auto_recovery(
            db_session, device, source="exit_maintenance", reason="Operator exited maintenance"
        )

    assert probe_called, "attempt_auto_recovery early-returned instead of probing a verifying device"
