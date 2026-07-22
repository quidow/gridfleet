"""Regression: `reconcile_device` must tolerate a device row that vanished
between the scan select and its own `lock_device`.

A concurrent operator delete (or any other deleter) can remove the Device row
after the reconciler scan picked its id but before `reconcile_device` locks it.
Pre-fix, `lock_device` raised `NoResultFound`, which propagated out of
`reconcile_device` and aborted the *entire* reconcile cycle for every other
device in the scan. The vanished device must instead be a no-op.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from app.devices.services.intent_reconciler import (
    ReconcileCandidate,
    ReconcileCommandResult,
    reconcile_device,
    reconcile_device_command,
)
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def test_reconcile_device_no_ops_when_device_missing(db_session: AsyncSession) -> None:
    """A device id with no row must reconcile to a clean no-op, not raise."""
    await reconcile_device(db_session, uuid.uuid4(), publisher=event_bus)


async def test_reconcile_command_no_ops_when_device_missing(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    result = await reconcile_device_command(
        db_session_maker,
        ReconcileCandidate(uuid.uuid4()),
        publisher=event_bus,
        packs={},
    )

    assert result == ReconcileCommandResult(changed=False, target=None)


async def test_reconcile_device_tolerates_concurrent_delete(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Deleting the device, then reconciling its id, must not raise."""
    device = await create_device(db_session, host_id=db_host.id, name="vanished-reconcile")
    device_id = device.id
    await db_session.delete(device)
    await db_session.commit()

    await reconcile_device(db_session, device_id, publisher=event_bus)
