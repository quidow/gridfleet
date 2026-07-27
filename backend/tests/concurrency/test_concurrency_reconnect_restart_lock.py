import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import Device, DeviceOperationalState
from app.devices.routers import control as devices_control
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.service import DeviceCrudService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.locking import LockedDevice

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_reconnect_restart_does_not_overwrite_concurrent_maintenance(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A maintenance entry that lands mid-reconnect must survive the node lever.

    The race window is between the route's device read and its
    ``session_viability_*`` write: maintenance commits from another session in
    between. The route must not stomp ``lifecycle_policy_state`` on the way past,
    and its node lever — which now takes the Device aggregate lock on its own
    short session — must queue behind that maintenance rather than interleave
    with it.
    """
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="reconnect-maintenance-race",
        operational_state=DeviceOperationalState.offline,
        connection_type="network",
        ip_address="10.0.0.50",
        verified=True,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=0,
            active_connection_target="",
        )
    )
    await db_session.commit()
    device_id = device.id

    agent_call_entered = asyncio.Event()
    maintenance_committed = asyncio.Event()

    async def fake_lifecycle_action(*_args: object, **_kwargs: object) -> dict[str, object]:
        # The reconnect route holds no session or row lock across the agent call,
        # so the racer can enter maintenance right here.
        agent_call_entered.set()
        await asyncio.wait_for(maintenance_committed.wait(), timeout=2.0)
        return {"success": True}

    restart_callers: list[str] = []

    async def fake_restart_node_txn(
        db: AsyncSession,
        locked: LockedDevice,
        *,
        caller: str,
    ) -> AppiumNode:
        restart_callers.append(caller)
        locked.assert_active(db)
        assert locked.device.appium_node is not None
        return locked.device.appium_node

    monkeypatch.setattr("app.devices.services.link_repair.pack_device_lifecycle_action", fake_lifecycle_action)

    async def reconnect() -> None:
        async with db_session_maker() as session:
            await devices_control.reconnect_device(
                device_id,
                db=session,
                device_services=SimpleNamespace(  # type: ignore[arg-type]
                    crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
                    publisher=event_bus,
                ),
                settings_services=SimpleNamespace(service=FakeSettingsReader({})),  # type: ignore[arg-type]
                agent_comm=SimpleNamespace(circuit_breaker=Mock(), http_pool=None),  # type: ignore[arg-type]
                appium_services=SimpleNamespace(  # type: ignore[arg-type]
                    reconciler_agent=SimpleNamespace(restart_node_txn=fake_restart_node_txn),
                    session_factory=db_session_maker,
                ),
            )

    async def enter_maintenance_mid_reconnect() -> None:
        await asyncio.wait_for(agent_call_entered.wait(), timeout=2.0)
        async with db_session_maker.begin() as session:
            await MaintenanceService(
                review=build_review_service(),
                settings=FakeSettingsReader({}),
                publisher=event_bus,
                session_factory=db_session_maker,
            ).enter_maintenance(session, device_id)
        maintenance_committed.set()

    await asyncio.gather(reconnect(), enter_maintenance_mid_reconnect())

    assert restart_callers == ["operator_restart"]

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()

    # §4 (Phase 2): the concurrent maintenance signal derives onto the operational axis and
    # outranks the offline that the reconnect/restart race would otherwise produce.
    assert device_row.operational_state_last_emitted == DeviceOperationalState.maintenance
    # hold is now derived by the reconciler (Task 7+8); check the maintenance_reason signal instead
    from app.devices.services.lifecycle_policy_state import state as ps

    assert ps(device_row).get("maintenance_reason") is not None
