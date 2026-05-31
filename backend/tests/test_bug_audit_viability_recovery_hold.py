"""Recovery probe must reject offline-and-held devices at the pre-lock gate.

The pre-lock predicate previously allowed ``checked_by=recovery`` past the
gate as long as ``operational_state == offline``, even if ``hold`` was set.
The post-lock re-check (see ``test_bug_audit_viability_hold_toctou``)
caught that case, but only after taking the row lock — and the manual
path emitted a free ``failed`` viability record on the way out. The
pre-lock predicate now also requires ``hold is None`` for the recovery
branch, so a recovery probe against an ``offline + maintenance`` device
fails fast with ``ValueError`` and never reaches the lock.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.devices.models import DeviceHold, DeviceOperationalState
from app.devices.services.capability import DeviceCapabilityService
from app.sessions.service_viability import SessionViabilityService
from app.sessions.viability_types import SessionViabilityCheckedBy
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device, create_host
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_recovery_probe_rejects_offline_held_device(
    db_session: AsyncSession,
    client: AsyncClient,
) -> None:
    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="viability-recovery-held",
        operational_state=DeviceOperationalState.offline,
        hold=DeviceHold.maintenance,
        verified=True,
    )
    await db_session.commit()

    svc = SessionViabilityService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        session_factory=AsyncMock(),
        capability=DeviceCapabilityService(),
    )
    with pytest.raises(ValueError, match="only run for available devices"):
        await svc.run_session_viability_probe(
            db_session,
            device,
            checked_by=SessionViabilityCheckedBy.recovery,
        )
