"""Phase 3 bulk + device-group desired-state caller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_bulk_restart_persists_transition_token_when_auto_recovery_intent_present(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Regression: with an auto_recovery:node intent already registered at
    PRIORITY_AUTO_RECOVERY, the operator restart intent (same priority) used
    to lose the lex-by-source tie-break, dropping its transition_token before
    write_desired_state ran. Convergence then emitted `confirm_running` and
    the node never restarted. The evaluator now prefers tokenized intents on
    same-priority `start` ties.
    """
    from app.appium_nodes.services.desired_state_writer import write_desired_state
    from app.devices.services import bulk as bulk_service
    from app.devices.services.intent import IntentService
    from app.devices.services.intent_types import NODE_PROCESS, PRIORITY_AUTO_RECOVERY, IntentRegistration

    device = await create_device(db_session, host_id=db_host.id, name="bk-restart", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=12345,
        active_connection_target="device-1",
    )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node
    await write_desired_state(
        db_session,
        node=node,
        target=AppiumDesiredState.running,
        desired_port=4723,
        caller="bulk",
    )
    # Simulate the standing baseline registered by lifecycle_policy when a
    # device boots healthy. Same priority + axis as the operator restart, but
    # no transition_token.
    await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"auto_recovery:node:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "start", "priority": PRIORITY_AUTO_RECOVERY, "desired_port": 4723},
            ),
        ],
        reason="seed baseline",
    )
    await db_session.commit()
    assert node.transition_token is None

    await bulk_service._bulk_restart_one(db_session, device, caller="bulk")
    await db_session.refresh(node)

    assert node.transition_token is not None, "restart intent's token must reach the node row"
    assert node.transition_deadline is not None
    assert node.desired_state == AppiumDesiredState.running


async def test_bulk_start_nodes_tags_desired_state_as_bulk(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="bk-start", verified=True)
    await db_session.commit()

    captured: list[str] = []

    async def fake_start(_db: AsyncSession, dev: Device, caller: str) -> AppiumNode:
        captured.append(caller)
        return AppiumNode(
            device_id=dev.id,
            port=4723,
            grid_url="http://hub:4444",
            pid=0,
            active_connection_target="",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )

    from app.devices.services import bulk as bulk_service

    monkeypatch.setattr(bulk_service, "_bulk_start_one", fake_start)
    monkeypatch.setattr(bulk_service.event_bus, "publish", AsyncMock())
    await bulk_service.bulk_start_nodes(db_session, [device.id])

    assert captured == ["bulk"]


async def test_bulk_start_nodes_accepts_group_caller(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="grp-start", verified=True)
    await db_session.commit()

    captured: list[str] = []

    async def fake_start(_db: AsyncSession, dev: Device, caller: str) -> AppiumNode:
        captured.append(caller)
        return AppiumNode(
            device_id=dev.id,
            port=4723,
            grid_url="http://hub:4444",
            pid=0,
            active_connection_target="",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )

    from app.devices.services import bulk as bulk_service

    monkeypatch.setattr(bulk_service, "_bulk_start_one", fake_start)
    monkeypatch.setattr(bulk_service.event_bus, "publish", AsyncMock())
    await bulk_service.bulk_start_nodes(db_session, [device.id], caller="group")

    assert captured == ["group"]
