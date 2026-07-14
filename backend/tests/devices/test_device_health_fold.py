from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.core.observation_revision import next_observation_revision
from app.core.timeutil import now_utc
from app.devices.models import Device
from app.hosts.service_status_push import OBSERVATION_REVISION_KEY
from tests.helpers import build_connectivity_service, seed_host_with_devices

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.schemas.device_health_push import DeviceHealthItem
    from app.devices.services.connectivity import DeviceFoldOutcome

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


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


async def test_fold_retryable_device_holds_receipt_and_replays_only_that_device(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=2, identity_prefix="fold-partial")
    good, bad = devices
    good_id, bad_id, host_id = good.id, bad.id, host.id  # capture before per-device commits expire the rows
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
    await db_session.refresh(good)
    await db_session.refresh(bad)
    assert good.device_checks_fold_applied_revision == revision
    assert bad.device_checks_fold_applied_revision < revision

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
