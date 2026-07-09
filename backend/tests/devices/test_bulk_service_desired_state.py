"""Phase 3 bulk + device-group desired-state caller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.services.bulk import BulkOperationsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_bulk_restart_persists_transition_token_when_auto_recovery_intent_present(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Regression: with an auto_recovery:node start command already registered,
    the operator restart command used to lose the lex-by-source tie-break,
    dropping its transition_token before write_desired_state ran. Convergence
    then emitted `confirm_running` and the node never restarted. The decision
    ladder now prefers token-bearing starts on same-tier `start` ties.
    """
    from app.appium_nodes.services.desired_state_writer import DesiredStateWrite, write_desired_state
    from app.devices.services import bulk as bulk_service
    from app.devices.services.intent import IntentService
    from app.devices.services.intent_types import NODE_PROCESS, IntentRegistration

    device = await create_device(db_session, host_id=db_host.id, name="bk-restart", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=12345,
        active_connection_target="device-1",
    )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node
    await write_desired_state(
        db_session,
        node=node,
        caller="bulk",
        write=DesiredStateWrite(target=AppiumDesiredState.running, desired_port=4723),
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
                payload={"action": "start"},
            ),
        ],
    )
    await db_session.commit()
    assert node.transition_token is None

    await bulk_service._bulk_restart_one(
        db_session,
        device,
        caller="bulk",
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
        ),
    )
    await db_session.refresh(node)

    assert node.transition_token is not None, "restart intent's token must reach the node row"
    assert node.transition_deadline is not None
    assert node.desired_state == AppiumDesiredState.running


async def test_operator_start_intent_is_ttl_bounded(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """_bulk_start_one must register operator:start with a TTL and no precondition.

    The node_running precondition was replaced by an expires_at TTL: the row is a
    no-op once the node runs (baseline:idle sustains it) and self-expires.
    """
    from sqlalchemy import select

    from app.devices.models import DeviceIntent
    from app.devices.services import bulk as bulk_service

    device = await create_device(db_session, host_id=db_host.id, name="op-start-prec", verified=True)
    device.appium_node = None  # avoid lazy-load in the same async context
    await bulk_service._bulk_start_one(
        db_session,
        device,
        caller="bulk",
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
        ),
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"operator:start:{device.id}",
            )
        )
    ).scalar_one()
    assert row.expires_at is not None


async def test_operator_restart_intent_is_ttl_bounded(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """_bulk_restart_one (restart variant with transition_token) must be TTL-bounded, no precondition."""
    from sqlalchemy import select

    from app.appium_nodes.models import AppiumDesiredState, AppiumNode
    from app.devices.models import DeviceIntent
    from app.devices.services import bulk as bulk_service

    device = await create_device(db_session, host_id=db_host.id, name="op-restart-prec", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=12345,
        active_connection_target="device-1",
        desired_state=AppiumDesiredState.running,
    )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node

    await bulk_service._bulk_restart_one(
        db_session,
        device,
        caller="bulk",
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
        ),
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"operator:start:{device.id}",
            )
        )
    ).scalar_one()
    assert row.expires_at is not None
    # Restart-specific payload fields remain intact.
    assert row.payload.get("transition_token") is not None
    assert row.payload.get("transition_deadline") is not None


async def test_bulk_start_nodes_tags_desired_state_as_bulk(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="bk-start", verified=True)
    await db_session.commit()

    captured: list[str] = []

    async def fake_start(_db: AsyncSession, dev: Device, caller: str, *, operator: object) -> AppiumNode:
        captured.append(caller)
        _bypass_tmp = AppiumNode(
            device_id=dev.id,
            port=4723,
            pid=0,
            active_connection_target="",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
        return _bypass_tmp

    from app.devices.services import bulk as bulk_service

    monkeypatch.setattr(bulk_service, "_bulk_start_one", fake_start)
    _settings_bulk = FakeSettingsReader({})
    await BulkOperationsService(
        publisher=event_bus,
        settings=_settings_bulk,
        circuit_breaker=MagicMock(),
        maintenance=MagicMock(),
        crud=DeviceCrudService(settings=_settings_bulk, identity=DeviceIdentityConflictService(), publisher=event_bus),
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=_settings_bulk, publisher=event_bus
        ),
    ).bulk_start_nodes(db_session, [device.id], caller="bulk")

    assert captured == ["bulk"]


async def test_bulk_start_nodes_accepts_group_caller(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="grp-start", verified=True)
    await db_session.commit()

    captured: list[str] = []

    async def fake_start(_db: AsyncSession, dev: Device, caller: str, *, operator: object) -> AppiumNode:
        captured.append(caller)
        _bypass_tmp = AppiumNode(
            device_id=dev.id,
            port=4723,
            pid=0,
            active_connection_target="",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
        return _bypass_tmp

    from app.devices.services import bulk as bulk_service

    monkeypatch.setattr(bulk_service, "_bulk_start_one", fake_start)
    _settings_group = FakeSettingsReader({})
    await BulkOperationsService(
        publisher=event_bus,
        settings=_settings_group,
        circuit_breaker=MagicMock(),
        maintenance=MagicMock(),
        crud=DeviceCrudService(settings=_settings_group, identity=DeviceIdentityConflictService(), publisher=event_bus),
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=_settings_group, publisher=event_bus
        ),
    ).bulk_start_nodes(db_session, [device.id], caller="group")

    assert captured == ["group"]
