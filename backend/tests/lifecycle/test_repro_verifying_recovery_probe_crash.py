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

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.models.intent import DeviceIntent
from app.devices.services.intent_types import CommandKind, verification_intent_source
from app.devices.services.lifecycle_policy_state import recovery_generation, set_recovery_generation
from app.lifecycle.services.incidents import LifecycleIncidentService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus
from tests.sessions.test_session_viability import run_session_viability_probe

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

import pytest

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def _seed_verifying_device(db: AsyncSession, host_id: uuid.UUID, *, identity: str) -> Device:
    """A verified device held ``verifying`` by an active verification lease, node up."""
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
            kind=CommandKind.verification_start.value,
            payload={"action": "start"},
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
    assert device.operational_state_last_emitted == DeviceOperationalState.available

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
    from app.jobs import JOB_KIND_DEVICE_RECOVERY, JOB_STATUS_PENDING
    from app.jobs import queue as job_queue
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.policy import LifecyclePolicyService
    from app.lifecycle.services.recovery_job import RecoveryJobService
    from app.runs.service_reservation import RunReservationService

    device = await _seed_verifying_device(db_session, db_host.id, identity="repro-verifying-c")

    probe_called: list[bool] = []

    async def _capture_probe(device_id: object, *, checked_by: object) -> dict[str, Any]:
        probe_called.append(True)
        return {"status": "passed"}

    viability = Mock()
    viability.run_session_viability_probe = _capture_probe

    assert db_session.bind is not None
    sf = async_sessionmaker(db_session.bind, class_=type(db_session), expire_on_commit=False)
    generation = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    set_recovery_generation(locked.device, generation)
    await job_queue.create_job(
        db_session,
        kind=JOB_KIND_DEVICE_RECOVERY,
        payload={
            "device_id": str(device.id),
            "source": "exit_maintenance",
            "reason": "Operator exited maintenance",
        },
        snapshot={"status": JOB_STATUS_PENDING},
        max_attempts=1,
        job_id=generation,
        commit=False,
    )
    await db_session.commit()

    service = RecoveryJobService(
        session_factory=sf,
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        lifecycle_policy=LifecyclePolicyService(
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
        ),
        viability=viability,  # type: ignore[arg-type]
    )
    await service.run_device_recovery_job(
        str(generation),
        {"device_id": str(device.id), "source": "exit_maintenance", "reason": "Operator exited maintenance"},
    )

    assert probe_called, "recovery worker early-returned instead of probing a verifying device"


async def test_run_recovery_probe_retries_until_passed(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry contract: probe retries on 'failed' and returns the first 'passed' result."""
    from app.lifecycle.services import recovery_job as recovery_job_mod
    from app.lifecycle.services.recovery_job import RecoveryJobService

    monkeypatch.setattr(recovery_job_mod, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(recovery_job_mod, "RECOVERY_PROBE_JITTER_MAX_SEC", 0)

    device = await _seed_verifying_device(db_session, db_host.id, identity="repro-retry-until-passed")

    viability = Mock()
    viability.run_session_viability_probe = AsyncMock(
        side_effect=[{"status": "failed", "error": "boom"}, {"status": "passed"}]
    )

    assert db_session.bind is not None
    sf = async_sessionmaker(db_session.bind, class_=type(db_session), expire_on_commit=False)
    service = RecoveryJobService(
        session_factory=sf,
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        lifecycle_policy=AsyncMock(),
        viability=viability,
    )

    out = await service._run_probe(device.id)
    assert out == {"status": "passed"}
    assert viability.run_session_viability_probe.await_count == 2


async def test_run_recovery_probe_skips_on_in_progress_collision(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F6: when another viability probe (e.g. an active verification job) holds the device's
    probe lock, ``_run_probe`` must report ``skipped`` — NOT ``failed`` — and must not
    retry. A concurrency collision is not a device-health signal; counting it as a failed attempt
    feeds recovery backoff/review and can shelve a healthy device."""
    from app.lifecycle.services import recovery_job as recovery_job_mod
    from app.lifecycle.services.recovery_job import RecoveryJobService
    from app.sessions.service_viability import SessionViabilityProbeInProgressError

    monkeypatch.setattr(recovery_job_mod, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(recovery_job_mod, "RECOVERY_PROBE_JITTER_MAX_SEC", 0)

    device = await _seed_verifying_device(db_session, db_host.id, identity="repro-collision-skip")

    probe = AsyncMock(
        side_effect=SessionViabilityProbeInProgressError("Session viability check already in progress for this device")
    )
    viability = Mock()
    viability.run_session_viability_probe = probe

    assert db_session.bind is not None
    sf = async_sessionmaker(db_session.bind, class_=type(db_session), expire_on_commit=False)
    service = RecoveryJobService(
        session_factory=sf,
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        lifecycle_policy=AsyncMock(),
        viability=viability,
    )

    result = await service._run_probe(device.id)

    assert result.get("status") == "skipped", result
    assert probe.await_count == 1, "a probe-in-flight collision must not be retried as a failure"


async def test_finalize_auto_recovery_skip_clears_generation_and_writes_no_state(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """F6: a recovery whose probe was *skipped* (another probe in flight) is finalized as a
    benign *skip* — not a suppression, not a failure: no auto-stop, no backoff, no
    ``review_required``, and no ``suppressed``/``needs_attention`` badge. The flow that won the
    lock does the real recovery; the lifecycle loop also retries on its next cycle."""
    from app.devices.services.decision_snapshot import load_device_decision_snapshot
    from app.lifecycle.services.policy import LifecyclePolicyService

    device = await _seed_verifying_device(db_session, db_host.id, identity="repro-collision-suppress")

    actions = AsyncMock()
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
    generation = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    set_recovery_generation(locked.device, generation)
    await db_session.commit()
    await db_session.refresh(device)

    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    outcome = await svc.finalize_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=generation,
        result={"status": "skipped"},
        source="exit_maintenance",
        reason="Operator exited maintenance",
    )
    await db_session.commit()

    assert outcome == "skipped"
    await db_session.refresh(device)
    assert recovery_generation(device) is None
    actions.complete_auto_stop_locked.assert_not_awaited()
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
    from app.devices.services.decision_snapshot import load_device_decision_snapshot
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
    generation = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    set_recovery_generation(locked.device, generation)
    await db_session.commit()
    await db_session.refresh(device)

    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    outcome = await svc.finalize_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=generation,
        result={"status": "skipped"},
        source="exit_maintenance",
        reason="Operator exited maintenance",
    )
    await db_session.commit()

    assert outcome == "skipped"

    await db_session.refresh(device)
    assert recovery_generation(device) is None
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["recovery_state"] != "suppressed", (
        "a probe-collision skip must not derive recovery_state=suppressed → needs_attention"
    )
