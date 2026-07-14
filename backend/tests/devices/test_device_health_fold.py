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
