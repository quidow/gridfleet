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
from app.lifecycle.services.incidents import LifecycleIncidentService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus
from tests.sessions.test_session_viability import run_session_viability_probe

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
            "app.sessions.service_viability.SessionViabilityService.probe_session_direct",
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
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.policy import LifecyclePolicyService
    from app.runs.service_reservation import RunReservationService

    device = await _seed_verifying_device(db_session, db_host.id, identity="repro-verifying-c")

    probe_called: list[bool] = []

    async def _capture_probe(self_arg: object, db: object, dev: object) -> dict[str, Any]:
        probe_called.append(True)
        return {"status": "passed"}

    svc = LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
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


async def test_run_recovery_probe_skips_on_in_progress_collision(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F6: when another viability probe (e.g. an active verification job) holds the device's
    probe lock, ``_run_recovery_probe`` must report ``skipped`` — NOT ``failed`` — and must not
    retry. A concurrency collision is not a device-health signal; counting it as a failed attempt
    feeds recovery backoff/review and can shelve a healthy device."""
    from app.lifecycle.services import policy as policy_mod
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.policy import LifecyclePolicyService
    from app.runs.service_reservation import RunReservationService
    from app.sessions.service_viability import SessionViabilityProbeInProgressError

    # Keep the test fast even if a regression reintroduces retrying on collision.
    monkeypatch.setattr(policy_mod, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(policy_mod, "RECOVERY_PROBE_JITTER_MAX_SEC", 0)

    device = await _seed_verifying_device(db_session, db_host.id, identity="repro-collision-skip")

    probe = AsyncMock(
        side_effect=SessionViabilityProbeInProgressError("Session viability check already in progress for this device")
    )
    viability = Mock()
    viability.run_session_viability_probe = probe

    svc = LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=viability,
        node_manager=AsyncMock(),
    )

    result = await svc._run_recovery_probe(db_session, device)

    assert result.get("status") == "skipped", result
    assert probe.await_count == 1, "a probe-in-flight collision must not be retried as a failure"


async def test_attempt_auto_recovery_records_skip_on_probe_collision(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """F6: a recovery whose probe was *skipped* (another probe in flight) is recorded as a
    benign *skip* — not a suppression, not a failure: no auto-stop, no backoff, no
    ``review_required``, and no ``suppressed``/``needs_attention`` badge. The flow that won the
    lock does the real recovery; the lifecycle loop also retries on its next cycle."""
    from app.lifecycle.services.policy import LifecyclePolicyService

    device = await _seed_verifying_device(db_session, db_host.id, identity="repro-collision-suppress")

    actions = AsyncMock()
    # The pre-probe gate consults this; a blanket AsyncMock returns a truthy mock and would
    # short-circuit into the "client session running" suppression before the probe runs.
    actions.has_running_client_session = AsyncMock(return_value=False)
    review = AsyncMock()
    svc = LifecyclePolicyService(
        review=review,
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=actions,
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )

    probe_mock = AsyncMock(return_value={"status": "skipped"})
    with patch.object(LifecyclePolicyService, "_run_recovery_probe", new=probe_mock):
        await svc.attempt_auto_recovery(
            db_session, device, source="exit_maintenance", reason="Operator exited maintenance"
        )

    probe_mock.assert_awaited_once()  # gates passed; we actually reached the probe + skip branch
    actions.record_recovery_skipped.assert_awaited_once()
    actions.record_recovery_suppressed.assert_not_awaited()
    actions.complete_auto_stop.assert_not_awaited()
    review.mark_review_required.assert_not_awaited()


async def test_probe_collision_skip_does_not_flag_needs_attention(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """ex-N11 'Fix B': a recovery whose probe was *skipped* because another viability probe
    held the device's lock is a benign, self-resolving collision — NOT an operator-actionable
    condition. The winning flow (exit-maintenance verification / the next connectivity tick)
    does the real recovery, so the skip must NOT leave the device deriving
    ``recovery_state="suppressed"`` (→ a false ``needs_attention`` / "Recovery Paused" badge).

    Uses the real ``LifecyclePolicyActionsService`` so the derived lifecycle policy is asserted,
    not the call shape."""
    from app.devices.services.lifecycle_policy_state import state as policy_state
    from app.devices.services.lifecycle_policy_summary import build_lifecycle_policy
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.policy import LifecyclePolicyService
    from app.runs.service_reservation import RunReservationService

    device = await _seed_verifying_device(db_session, db_host.id, identity="repro-collision-no-attention")

    svc = LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )

    probe_mock = AsyncMock(return_value={"status": "skipped"})
    with patch.object(LifecyclePolicyService, "_run_recovery_probe", new=probe_mock):
        restored = await svc.attempt_auto_recovery(
            db_session, device, source="exit_maintenance", reason="Operator exited maintenance"
        )

    assert restored is False  # a skip is not a successful recovery
    probe_mock.assert_awaited_once()  # gates passed; we reached the probe + skip branch

    await db_session.refresh(device)
    assert policy_state(device).get("recovery_suppressed_reason") is None, (
        "a benign probe-lock collision must not record a suppression reason"
    )
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["recovery_state"] != "suppressed", (
        "a probe-collision skip must not derive recovery_state=suppressed → needs_attention"
    )
