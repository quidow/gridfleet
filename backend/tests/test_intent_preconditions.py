from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.devices.models import DeviceIntent
from app.devices.services.intent_preconditions import is_satisfied
from app.devices.services.intent_types import NODE_PROCESS
from app.runs.models import RunState
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


@pytest.mark.db
async def test_run_active_satisfied_when_run_running(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-run-active-ok")
    run = await create_reserved_run(db_session, name="prec-active", devices=[device])
    run.state = RunState.active
    await db_session.commit()
    intent = DeviceIntent(
        device_id=device.id,
        source=f"cooldown:node:{run.id}",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "run_active", "run_id": str(run.id)},
    )
    assert await is_satisfied(db_session, intent) is True


@pytest.mark.db
async def test_run_active_unsatisfied_when_run_terminal(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-run-active-term")
    run = await create_reserved_run(db_session, name="prec-term", devices=[device])
    run.state = RunState.completed
    await db_session.commit()
    intent = DeviceIntent(
        device_id=device.id,
        source=f"cooldown:node:{run.id}",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "run_active", "run_id": str(run.id)},
    )
    assert await is_satisfied(db_session, intent) is False


@pytest.mark.db
async def test_run_active_unsatisfied_when_run_missing(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-run-active-miss")
    intent = DeviceIntent(
        device_id=device.id,
        source="cooldown:node:missing",
        axis=NODE_PROCESS,
        payload={},
        precondition={"kind": "run_active", "run_id": str(uuid4())},
    )
    assert await is_satisfied(db_session, intent) is False


@pytest.mark.db
async def test_is_satisfied_returns_true_when_precondition_is_none(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-none")
    intent = DeviceIntent(device_id=device.id, source="baseline", axis=NODE_PROCESS, payload={}, precondition=None)
    assert await is_satisfied(db_session, intent) is True
