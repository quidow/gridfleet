from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models.agent_reconfigure_outbox import AgentReconfigureOutbox
from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.services.agent_reconfigure_delivery import deliver_agent_reconfigures
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
