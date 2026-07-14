from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from sqlalchemy import select

from app.core.observation_revision import next_observation_revision
from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.health import DeviceHealthService
from app.hosts.service_status_push import OBSERVATION_REVISION_KEY
from app.jobs.models import Job
from app.packs.models import DriverPack, PackState
from tests.fakes import FakeSettingsReader
from tests.helpers import build_connectivity_service, seed_host_and_device, seed_host_with_devices

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.protocols import HealthFailureHandler
    from app.devices.schemas.device_health_push import DeviceHealthItem
    from app.devices.services.connectivity import DeviceFoldOutcome

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _loop_service(
    *,
    settings: dict[str, int] | None = None,
    lifecycle_policy: HealthFailureHandler | None = None,
) -> ConnectivityService:
    policy = lifecycle_policy if lifecycle_policy is not None else AsyncMock()
    return ConnectivityService(
        publisher=Mock(),
        settings=FakeSettingsReader(settings or {}),
        circuit_breaker=Mock(),
        lifecycle_policy=policy,
        health=DeviceHealthService(publisher=Mock()),
    )


def _health_section(
    device_id: uuid.UUID,
    *,
    revision: int,
    reported_at: str,
    health: dict[str, Any],
) -> dict[str, Any]:
    return {
        "reported_at": reported_at,
        "section_sequence": revision,
        OBSERVATION_REVISION_KEY: revision,
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(device_id),
                "probe_status": "observed",
                "presence": "present",
                "health": health,
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
        ],
    }


async def _fold_health_once(
    db_session: AsyncSession,
    service: ConnectivityService,
    *,
    host_id: uuid.UUID,
    device_id: uuid.UUID,
    reported_at: datetime,
    health: dict[str, Any],
) -> None:
    revision = await next_observation_revision(db_session)
    section = _health_section(
        device_id,
        revision=revision,
        reported_at=reported_at.isoformat(),
        health=health,
    )
    assert await service.fold_host_devices(db_session, host_id, section, boot_id=uuid.uuid4()) is True


async def test_device_has_device_health_fold_receipt_columns(db_session: AsyncSession) -> None:
    _host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-cols")
    device = devices[0]
    # Defaults match the AppiumNode receipt columns.
    assert device.device_checks_fold_applied_revision == 0
    assert device.device_checks_fold_boot_id is None
    assert device.device_checks_fold_section_sequence is None
    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert hasattr(reloaded, "device_checks_fold_applied_revision")


async def test_fold_applies_healthy_and_advances_receipt(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-healthy")
    device = devices[0]
    revision = await next_observation_revision(db_session)
    section: dict[str, Any] = {
        "reported_at": now_utc().isoformat(),
        "section_sequence": 3,
        OBSERVATION_REVISION_KEY: revision,
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(device.id),
                "probe_status": "observed",
                "presence": "present",
                "health": {"healthy": True, "checks": []},
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
        ],
    }
    service = build_connectivity_service(db_session_maker)
    settled = await service.fold_host_devices(db_session, host.id, section, boot_id=uuid.uuid4())
    assert settled is True
    await db_session.refresh(device)
    assert device.device_checks_healthy is True
    assert device.device_checks_fold_applied_revision == revision
    assert device.device_checks_fold_section_sequence == 3


async def test_fold_preloads_pack_catalog_once_for_multiple_devices(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.devices.services.connectivity as conn_mod
    import app.devices.services.readiness as readiness_mod

    host, devices = await seed_host_with_devices(db_session, count=3, identity_prefix="fold-pack-catalog")
    device_ids = [device.id for device in devices]
    revision = await next_observation_revision(db_session)
    section: dict[str, Any] = {
        "reported_at": now_utc().isoformat(),
        "section_sequence": 4,
        OBSERVATION_REVISION_KEY: revision,
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(device_id),
                "probe_status": "observed",
                "presence": "present",
                "health": {"healthy": True, "checks": []},
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
            for device_id in device_ids
        ],
    }
    real_load = readiness_mod.load_packs_by_ids
    load_packs = AsyncMock(wraps=real_load)
    monkeypatch.setattr(readiness_mod, "load_packs_by_ids", load_packs)
    monkeypatch.setattr(conn_mod, "load_packs_by_ids", load_packs, raising=False)

    service = build_connectivity_service(db_session_maker)
    settled = await service.fold_host_devices(db_session, host.id, section, boot_id=uuid.uuid4())

    assert settled is True
    receipt_rows = (
        (await db_session.execute(select(Device.device_checks_fold_applied_revision).where(Device.id.in_(device_ids))))
        .scalars()
        .all()
    )
    assert set(receipt_rows) == {revision}
    load_packs.assert_awaited_once()


async def test_fold_ip_ping_hysteresis_runs_through_loop_path(
    db_session: AsyncSession,
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-ip-ping")
    device = devices[0]
    lifecycle_policy = MagicMock()
    lifecycle_policy.handle_health_failure = AsyncMock()
    lifecycle_policy.clear_escalation_residue_on_self_heal = AsyncMock()
    lifecycle_policy.restore_run_after_self_heal = AsyncMock()
    lifecycle_policy.attempt_auto_recovery = AsyncMock(return_value=False)
    service = _loop_service(
        settings={
            "device_checks.ip_ping.fail_window_sec": 120,
            "device_checks.probe_failed.fail_window_sec": 120,
        },
        lifecycle_policy=lifecycle_policy,
    )
    start = now_utc()
    health = {"healthy": False, "checks": [{"check_id": "ip_ping", "ok": False}]}

    for offset in (0, 60):
        await _fold_health_once(
            db_session,
            service,
            host_id=host.id,
            device_id=device.id,
            reported_at=start + timedelta(seconds=offset),
            health=health,
        )
    await db_session.refresh(device)
    assert device.device_checks_healthy is True

    await _fold_health_once(
        db_session,
        service,
        host_id=host.id,
        device_id=device.id,
        reported_at=start + timedelta(seconds=120),
        health=health,
    )
    await db_session.refresh(device)
    assert device.device_checks_healthy is False
    lifecycle_policy.handle_health_failure.assert_awaited_once()


async def test_fold_debounceable_check_hysteresis_runs_through_loop_path(
    db_session: AsyncSession,
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-probe-failed")
    device = devices[0]
    lifecycle_policy = MagicMock()
    lifecycle_policy.handle_health_failure = AsyncMock()
    lifecycle_policy.clear_escalation_residue_on_self_heal = AsyncMock()
    lifecycle_policy.restore_run_after_self_heal = AsyncMock()
    lifecycle_policy.attempt_auto_recovery = AsyncMock(return_value=False)
    service = _loop_service(
        settings={
            "device_checks.ip_ping.fail_window_sec": 120,
            "device_checks.probe_failed.fail_window_sec": 120,
        },
        lifecycle_policy=lifecycle_policy,
    )
    start = now_utc()
    health = {
        "healthy": False,
        "checks": [
            {"check_id": "ping", "ok": False, "debounce": True},
            {"check_id": "ecp", "ok": False, "debounce": True},
        ],
    }

    for offset in (0, 60):
        await _fold_health_once(
            db_session,
            service,
            host_id=host.id,
            device_id=device.id,
            reported_at=start + timedelta(seconds=offset),
            health=health,
        )
    await db_session.refresh(device)
    assert device.device_checks_healthy is True

    await _fold_health_once(
        db_session,
        service,
        host_id=host.id,
        device_id=device.id,
        reported_at=start + timedelta(seconds=120),
        health=health,
    )
    await db_session.refresh(device)
    assert device.device_checks_healthy is False
    lifecycle_policy.handle_health_failure.assert_awaited_once()


async def test_fold_healthy_device_runs_self_heal_cleanup(
    db_session: AsyncSession,
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-self-heal")
    device = devices[0]
    lifecycle_policy = MagicMock()
    lifecycle_policy.clear_escalation_residue_on_self_heal = AsyncMock()
    lifecycle_policy.restore_run_after_self_heal = AsyncMock()
    service = _loop_service(lifecycle_policy=lifecycle_policy)

    await _fold_health_once(
        db_session,
        service,
        host_id=host.id,
        device_id=device.id,
        reported_at=now_utc(),
        health={"healthy": True, "checks": []},
    )

    lifecycle_policy.clear_escalation_residue_on_self_heal.assert_awaited_once()
    lifecycle_policy.restore_run_after_self_heal.assert_awaited_once()


async def test_fold_healthy_offline_device_attempts_auto_recovery(
    db_session: AsyncSession,
) -> None:
    host, device = await seed_host_and_device(
        db_session,
        identity="fold-offline-recovery",
        operational_state=DeviceOperationalState.offline,
    )
    lifecycle_policy = MagicMock()
    lifecycle_policy.attempt_auto_recovery = AsyncMock(return_value=True)
    service = _loop_service(lifecycle_policy=lifecycle_policy)

    await _fold_health_once(
        db_session,
        service,
        host_id=host.id,
        device_id=device.id,
        reported_at=now_utc(),
        health={"healthy": True, "checks": []},
    )

    lifecycle_policy.attempt_auto_recovery.assert_awaited_once()
    assert lifecycle_policy.attempt_auto_recovery.await_args.kwargs["reason"] == (
        "Startup recovery after healthy reconnect"
    )


async def test_fold_does_not_enqueue_remediation_for_draining_pack(
    db_session: AsyncSession,
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-draining")
    device = devices[0]
    pack = await db_session.get(DriverPack, device.pack_id)
    assert pack is not None
    pack.state = PackState.draining
    await db_session.commit()
    lifecycle_policy = MagicMock()
    lifecycle_policy.handle_health_failure = AsyncMock()
    service = _loop_service(lifecycle_policy=lifecycle_policy)

    await _fold_health_once(
        db_session,
        service,
        host_id=host.id,
        device_id=device.id,
        reported_at=now_utc(),
        health={
            "healthy": False,
            "checks": [{"check_id": "adb_connected", "ok": False}],
            "recommended_action": "reconnect",
        },
    )

    jobs = (await db_session.execute(select(Job).where(Job.remediation_device_id == device.id))).scalars().all()
    assert jobs == []


async def test_fold_terminal_noop_on_unknown_presence_advances_receipt(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-unknown")
    device = devices[0]
    revision = await next_observation_revision(db_session)
    section: dict[str, Any] = {
        "reported_at": now_utc().isoformat(),
        "section_sequence": 1,
        OBSERVATION_REVISION_KEY: revision,
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(device.id),
                "probe_status": "error",
                "presence": "unknown",
                "health": {},
                "lifecycle_state": {"status": "error", "value": None},
            }
        ],
    }
    service = build_connectivity_service(db_session_maker)
    settled = await service.fold_host_devices(db_session, host.id, section, boot_id=uuid.uuid4())
    assert settled is True  # deliberate no-op, but the generation is consumed
    await db_session.refresh(device)
    assert device.device_checks_fold_applied_revision == revision  # marker advanced
    assert device.device_checks_healthy is None  # no health axis write from an indeterminate observation


async def test_fold_applies_observed_health_when_presence_gather_is_incomplete(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-health-only")
    device = devices[0]
    revision = await next_observation_revision(db_session)
    section: dict[str, Any] = {
        "reported_at": now_utc().isoformat(),
        "section_sequence": 2,
        OBSERVATION_REVISION_KEY: revision,
        "complete_gather": False,
        "devices": [
            {
                "device_id": str(device.id),
                "probe_status": "observed",
                "presence": "unknown",
                "health": {"healthy": True, "checks": []},
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
        ],
    }
    service = build_connectivity_service(db_session_maker)

    assert await service.fold_host_devices(db_session, host.id, section, boot_id=uuid.uuid4()) is True
    await db_session.refresh(device)

    # Discovery completeness gates only absence. A successful direct health
    # observation remains valid and must not freeze whenever discovery fails.
    assert device.device_checks_healthy is True
    assert device.device_checks_fold_applied_revision == revision


async def test_fold_ignores_presence_absent_when_health_probe_errored(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """An absent discovery verdict with no health evidence (the direct probe
    errored) produces no verdict — presence never drives a disconnect. The
    receipt still advances so the generation is consumed."""
    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-absent")
    device = devices[0]
    revision = await next_observation_revision(db_session)
    section: dict[str, Any] = {
        "reported_at": now_utc().isoformat(),
        "section_sequence": 2,
        OBSERVATION_REVISION_KEY: revision,
        "complete_gather": True,
        "devices": [
            {
                # Absent from the discovery pass and the direct health probe could
                # not answer: no positive health evidence, so no verdict is applied.
                "device_id": str(device.id),
                "probe_status": "error",
                "presence": "absent",
                "health": None,
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
        ],
    }
    service = build_connectivity_service(db_session_maker)

    assert await service.fold_host_devices(db_session, host.id, section, boot_id=uuid.uuid4()) is True
    await db_session.refresh(device)

    assert device.device_checks_healthy is None  # no health axis write from an absent, unprobed device
    assert device.device_checks_summary != "Disconnected"
    assert device.device_checks_fold_applied_revision == revision


async def test_fold_keeps_registered_device_healthy_when_discovery_reports_absent(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """A registered device whose health check passes stays healthy even when the
    discovery pass reports it absent. Presence is a discovery signal — it must not
    gate the liveness of an already-registered device. Regression: a cross-subnet
    Roku fails SSDP (multicast) presence while its unicast health check passes, and
    the fold must not mark it Disconnected."""
    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-absent-healthy")
    device = devices[0]
    revision = await next_observation_revision(db_session)
    section: dict[str, Any] = {
        "reported_at": now_utc().isoformat(),
        "section_sequence": 2,
        OBSERVATION_REVISION_KEY: revision,
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(device.id),
                "probe_status": "observed",
                "presence": "absent",
                "health": {"healthy": True, "checks": []},
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
        ],
    }
    service = build_connectivity_service(db_session_maker)

    assert await service.fold_host_devices(db_session, host.id, section, boot_id=uuid.uuid4()) is True
    await db_session.refresh(device)

    assert device.device_checks_healthy is True
    assert device.device_checks_summary != "Disconnected"
    assert device.device_checks_fold_applied_revision == revision


async def test_fold_consumes_pre_maintenance_observation_without_health_or_remediation(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.devices.services.connectivity as conn_mod

    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-maintenance")
    device = devices[0]
    device.lifecycle_policy_state = {"maintenance_reason": "operator hold"}
    await db_session.commit()
    revision = await next_observation_revision(db_session)
    section: dict[str, Any] = {
        "reported_at": now_utc().isoformat(),
        "section_sequence": 3,
        OBSERVATION_REVISION_KEY: revision,
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(device.id),
                "probe_status": "observed",
                "presence": "present",
                "health": {
                    "healthy": False,
                    "checks": [{"check_id": "reconnect_probe", "ok": False}],
                    "recommended_action": "reconnect",
                },
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
        ],
    }
    enqueue = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(conn_mod, "enqueue_device_health_remediation", enqueue)
    service = build_connectivity_service(db_session_maker)

    assert await service.fold_host_devices(db_session, host.id, section, boot_id=uuid.uuid4()) is True
    await db_session.refresh(device)

    assert device.device_checks_healthy is None
    assert device.device_checks_fold_applied_revision == revision
    enqueue.assert_not_awaited()


async def test_fold_retryable_device_holds_receipt_and_replays_only_that_device(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=2, identity_prefix="fold-partial")
    # The fold orders by id. Force the retryable device to run first so its
    # rollback cannot hide catalog-lifecycle bugs behind random UUID ordering.
    bad_id, good_id = sorted(device.id for device in devices)
    host_id = host.id
    revision = await next_observation_revision(db_session)

    def _present(dev_id: uuid.UUID) -> dict[str, Any]:
        return {
            "device_id": str(dev_id),
            "probe_status": "observed",
            "presence": "present",
            "health": {"healthy": True, "checks": []},
            "lifecycle_state": {"status": "unsupported", "value": None},
        }

    section: dict[str, Any] = {
        "reported_at": now_utc().isoformat(),
        "section_sequence": 5,
        OBSERVATION_REVISION_KEY: revision,
        "complete_gather": True,
        "devices": [_present(good_id), _present(bad_id)],
    }
    service = build_connectivity_service(db_session_maker)
    real_apply = service._apply_device_health
    calls: list[uuid.UUID] = []

    async def flaky(db: AsyncSession, device_id: uuid.UUID, item: DeviceHealthItem, **kw: object) -> DeviceFoldOutcome:
        calls.append(device_id)
        if device_id == bad_id:
            raise RuntimeError("boom")
        return await real_apply(db, device_id, item, **kw)  # type: ignore[arg-type]

    service._apply_device_health = flaky  # type: ignore[method-assign]
    settled = await service.fold_host_devices(db_session, host_id, section, boot_id=uuid.uuid4())
    assert settled is False  # one device retryable -> host watermark held by the loop

    async def _fold_rev(dev_id: uuid.UUID) -> int:
        # Read the committed receipt directly: the fold's per-device rollback expires
        # the loaded rows, so an ORM attribute read here can trigger a sync lazy-load.
        value = await db_session.scalar(select(Device.device_checks_fold_applied_revision).where(Device.id == dev_id))
        assert value is not None
        return value

    assert await _fold_rev(good_id) == revision
    assert await _fold_rev(bad_id) < revision

    # Second pass replays only the retryable device: the committed peer is skipped.
    assert await service.fold_host_devices(db_session, host_id, section, boot_id=uuid.uuid4()) is False
    assert calls.count(good_id) == 1
    assert calls.count(bad_id) == 2


async def test_fold_ignores_device_absent_from_gather(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=2, identity_prefix="fold-omit")
    present, omitted = devices
    revision = await next_observation_revision(db_session)
    section: dict[str, Any] = {
        "reported_at": now_utc().isoformat(),
        "section_sequence": 2,
        OBSERVATION_REVISION_KEY: revision,
        "complete_gather": False,  # incomplete: cannot assert the omitted device is absent
        "devices": [
            {
                "device_id": str(present.id),
                "probe_status": "observed",
                "presence": "present",
                "health": {"healthy": True, "checks": []},
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
        ],
    }
    service = build_connectivity_service(db_session_maker)
    assert await service.fold_host_devices(db_session, host.id, section, boot_id=uuid.uuid4()) is True
    await db_session.refresh(omitted)
    assert omitted.device_checks_fold_applied_revision == 0  # never touched — "not gathered", not absent
    assert omitted.device_checks_healthy is None


async def test_stale_device_fold_does_not_override_fresh_synchronous_write(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from app.devices.services.health import DeviceHealthService
    from tests.helpers import test_event_bus as bus

    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-guard")
    device = devices[0]
    # The fold's generation is stamped FIRST (older revision) ...
    stale_revision = await next_observation_revision(db_session)
    # ... then a synchronous higher-authority writer (e.g. host-offline cascade,
    # lifecycle crash, restart ingest, create-failure) draws a fresh revision and
    # marks the device unhealthy.
    await DeviceHealthService(publisher=bus).update_device_checks(
        db_session, device, healthy=False, summary="host offline cascade", revision=None
    )
    await db_session.commit()

    section: dict[str, Any] = {
        "reported_at": now_utc().isoformat(),
        "section_sequence": 9,
        OBSERVATION_REVISION_KEY: stale_revision,
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(device.id),
                "probe_status": "observed",
                "presence": "present",
                "health": {"healthy": True, "checks": []},
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
        ],
    }
    service = build_connectivity_service(db_session_maker)
    settled = await service.fold_host_devices(db_session, host.id, section, boot_id=uuid.uuid4())
    assert settled is True  # the device settles (marker advances) ...
    await db_session.refresh(device)
    assert device.device_checks_healthy is False  # ... but the stale healthy verdict LOST the guard
    assert device.device_checks_fold_applied_revision == stale_revision  # not retried forever


def _emulator_section(device_id: uuid.UUID, value: str) -> dict[str, Any]:
    return {
        "reported_at": now_utc().isoformat(),
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(device_id),
                "probe_status": "observed",
                "presence": "present",
                "health": {"healthy": True, "checks": []},
                "lifecycle_state": {"status": "observed", "value": value},
            }
        ],
    }


async def test_pushed_emulator_state_uses_per_item_observation_time(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from datetime import timedelta

    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="em-item-time")
    device = devices[0]
    operator_time = now_utc()
    device.emulator_state = "device"
    device.emulator_state_source_time = operator_time
    await db_session.commit()

    section = _emulator_section(device.id, "booting")
    # The lifecycle result was gathered before the operator refresh, but a slow
    # peer made the overall section finish afterward.
    section["reported_at"] = (operator_time + timedelta(minutes=1)).isoformat()
    section["devices"][0]["lifecycle_state"]["observed_at"] = (operator_time - timedelta(minutes=1)).isoformat()

    service = build_connectivity_service(db_session_maker)
    await service.apply_pushed_emulator_state(db_session, host.id, section)
    await db_session.commit()
    await db_session.refresh(device)

    assert device.emulator_state == "device"


async def test_pushed_emulator_state_without_source_time_fails_safe(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="em-no-source-time")
    device = devices[0]
    device.emulator_state = "device"
    device.emulator_state_source_time = now_utc()
    await db_session.commit()

    section = _emulator_section(device.id, "booting")
    section.pop("reported_at")
    service = build_connectivity_service(db_session_maker)

    await service.apply_pushed_emulator_state(db_session, host.id, section)
    await db_session.commit()
    await db_session.refresh(device)

    # Without an observation time M2 cannot compare authority safely. Treat the
    # lifecycle item as unusable instead of manufacturing a fresh backend time.
    assert device.emulator_state == "device"


async def test_pushed_emulator_state_applied_synchronously(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="em-sync")
    device = devices[0]
    service = build_connectivity_service(db_session_maker)
    await service.apply_pushed_emulator_state(db_session, host.id, _emulator_section(device.id, "device"))
    await db_session.commit()  # the push handler commits em_db after this synchronous step
    await db_session.refresh(device)
    assert device.emulator_state == "device"


async def test_pushed_emulator_state_unchanged_takes_no_lock(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.devices import locking as device_locking

    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="em-unchanged")
    device = devices[0]
    device.emulator_state = "device"
    device.emulator_state_source_time = now_utc()
    await db_session.commit()

    calls = 0
    real_lock = device_locking.lock_device

    async def counting_lock(db: AsyncSession, device_id: uuid.UUID, **kwargs: object) -> Device:
        nonlocal calls
        calls += 1
        return await real_lock(db, device_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(device_locking, "lock_device", counting_lock)
    service = build_connectivity_service(db_session_maker)
    await service.apply_pushed_emulator_state(db_session, host.id, _emulator_section(device.id, "device"))
    assert calls == 0  # unchanged value → update_emulator_state early-returns before the lock


async def test_batch_loaders_match_per_device(db_session: AsyncSession) -> None:
    from sqlalchemy.orm import selectinload

    from app.devices.services.state import derive_operational_state, derive_operational_states
    from app.lifecycle.services.remediation_log import load_ladder, load_ladders

    _host, seeded = await seed_host_with_devices(db_session, count=3, identity_prefix="fold-parity")
    # Reload with the relationships the per-device derivation reads, so it does not
    # lazy-load in a sync context (the batch variant bulk-loads them instead).
    stmt = (
        select(Device)
        .where(Device.id.in_([d.id for d in seeded]))
        .options(selectinload(Device.appium_node), selectinload(Device.host))
        .order_by(Device.id)
    )
    devices = list((await db_session.execute(stmt)).scalars().all())
    now = now_utc()
    batch_states = await derive_operational_states(db_session, devices, now=now)
    batch_ladders = await load_ladders(db_session, [d.id for d in devices])
    for device in devices:
        assert batch_states[device.id] == await derive_operational_state(db_session, device, now=now)
        single = await load_ladder(db_session, device.id)
        assert batch_ladders[device.id].attempts == single.attempts  # ladder equivalence


async def test_stale_failing_fold_enqueues_remediation_that_self_cancels(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.devices.services.connectivity as conn_mod

    host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-residual")
    device = devices[0]
    revision = await next_observation_revision(db_session)
    section: dict[str, Any] = {
        "reported_at": now_utc().isoformat(),
        "section_sequence": 4,
        OBSERVATION_REVISION_KEY: revision,
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(device.id),
                "probe_status": "observed",
                "presence": "present",
                "health": {
                    "healthy": False,
                    "checks": [{"check_id": "reconnect_probe", "ok": False}],
                    "recommended_action": "reconnect",
                },
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
        ],
    }
    service = build_connectivity_service(db_session_maker)
    enqueue = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(conn_mod, "enqueue_device_health_remediation", enqueue)
    settled = await service.fold_host_devices(db_session, host.id, section, boot_id=uuid.uuid4())
    assert settled is True
    # A stale FAILING observation that transiently wins enqueues exactly one repeat-safe
    # remediation job (reconnect); the job worker's current-fact recheck (A3) cancels it
    # if a newer healthy fact commits before it runs.
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["action_id"] == "reconnect"
