from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models.agent_reconfigure_outbox import AgentReconfigureOutbox
from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.services.agent_reconfigure_delivery import MAX_DELIVERY_ATTEMPTS, deliver_agent_reconfigures
from tests.helpers import create_device

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host


async def test_stale_outbox_row_is_marked_delivered_without_agent_call(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="stale-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid:4444",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=3,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=False,
        stop_pending=True,
        reconciled_generation=2,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    reconfigure = AsyncMock()
    monkeypatch.setattr(
        "app.services.agent_reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure
    )

    await deliver_agent_reconfigures(db_session, device.id)

    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert stored.delivered_at is not None
    reconfigure.assert_not_awaited()


async def test_outbox_row_sends_when_generation_matches(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="fresh-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid:4444",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=4,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=False,
        stop_pending=True,
        reconciled_generation=4,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    reconfigure = AsyncMock(return_value={"port": 4723})
    monkeypatch.setattr(
        "app.services.agent_reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure
    )

    await deliver_agent_reconfigures(db_session, device.id)

    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert stored.delivered_at is not None
    reconfigure.assert_awaited_once_with(
        db_host.ip,
        db_host.agent_port,
        port=4723,
        accepting_new_sessions=False,
        stop_pending=True,
        grid_run_id=None,
    )


async def test_outbox_delivery_failure_increments_attempts(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.errors import AgentUnreachableError

    device = await create_device(db_session, host_id=db_host.id, name="failed-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid:4444",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=1,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=1,
        created_at=datetime.now(UTC),
    )
    db_session.add_all([node, row])
    await db_session.commit()
    monkeypatch.setattr(
        "app.services.agent_reconfigure_delivery.agent_operations.agent_appium_reconfigure",
        AsyncMock(side_effect=AgentUnreachableError(db_host.ip, "offline")),
    )

    await deliver_agent_reconfigures(db_session, device.id)

    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert stored.delivered_at is None
    assert stored.delivery_attempts == 1


async def test_delivery_marks_older_duplicate_generation_rows_delivered(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="duplicate-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid:4444",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=7,
    )
    older = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=False,
        stop_pending=True,
        reconciled_generation=7,
        created_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    newest = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=7,
        created_at=datetime.now(UTC),
    )
    db_session.add_all([node, older, newest])
    await db_session.commit()
    reconfigure = AsyncMock(return_value={"port": 4723})
    monkeypatch.setattr(
        "app.services.agent_reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure
    )

    await deliver_agent_reconfigures(db_session, device.id)

    await db_session.refresh(older)
    await db_session.refresh(newest)
    assert older.delivered_at is not None
    assert newest.delivered_at is not None
    reconfigure.assert_awaited_once_with(
        db_host.ip,
        db_host.agent_port,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        grid_run_id=None,
    )


async def test_delivery_processes_at_most_one_batch_per_device(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="limited-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid:4444",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=1,
    )
    now = datetime.now(UTC)
    rows = [
        AgentReconfigureOutbox(
            device_id=device.id,
            port=4723,
            accepting_new_sessions=bool(index % 2),
            stop_pending=False,
            reconciled_generation=index + 1,
            created_at=now + timedelta(seconds=index),
        )
        for index in range(6)
    ]
    db_session.add_all([node, *rows])
    await db_session.commit()
    reconfigure = AsyncMock(return_value={"port": 4723})
    monkeypatch.setattr(
        "app.services.agent_reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure
    )

    await deliver_agent_reconfigures(db_session, device.id)

    delivered = (
        (
            await db_session.execute(
                select(AgentReconfigureOutbox).where(AgentReconfigureOutbox.delivered_at.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    pending = (
        (await db_session.execute(select(AgentReconfigureOutbox).where(AgentReconfigureOutbox.delivered_at.is_(None))))
        .scalars()
        .all()
    )
    assert len(delivered) == 5
    assert len(pending) == 1
    assert reconfigure.await_count == 5


async def test_delivery_abandons_row_after_max_attempts(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.errors import AgentUnreachableError

    device = await create_device(db_session, host_id=db_host.id, name="abandoned-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid:4444",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=1,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=1,
        delivery_attempts=MAX_DELIVERY_ATTEMPTS - 1,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    reconfigure = AsyncMock(side_effect=AgentUnreachableError(db_host.ip, "offline"))
    monkeypatch.setattr(
        "app.services.agent_reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure
    )

    await deliver_agent_reconfigures(db_session, device.id)
    await deliver_agent_reconfigures(db_session, device.id)

    await db_session.refresh(row)
    assert row.delivered_at is None
    assert row.abandoned_at is not None
    assert row.delivery_attempts == MAX_DELIVERY_ATTEMPTS
    assert reconfigure.await_count == 1
