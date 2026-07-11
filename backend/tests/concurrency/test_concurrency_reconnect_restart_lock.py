import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
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

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_reconnect_restart_does_not_overwrite_concurrent_maintenance(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    async def fake_lifecycle_action(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"success": True}

    restart_entered = asyncio.Event()
    allow_restart = asyncio.Event()

    async def fake_restart_node(
        db: AsyncSession,
        _device: Device,
        *,
        caller: str,
        **_kwargs: object,
    ) -> AppiumNode:
        assert caller == "operator_restart"
        device = await db.get(Device, device_id)
        assert device is not None
        assert device.appium_node is not None
        restart_entered.set()
        await asyncio.wait_for(allow_restart.wait(), timeout=2.0)
        return device.appium_node

    monkeypatch.setattr("app.devices.services.link_repair.pack_device_lifecycle_action", fake_lifecycle_action)

    async def reconnect() -> None:
        async with db_session_maker() as session:
            await devices_control.reconnect_device(
                device_id,
                db=session,
                device_services=SimpleNamespace(
                    crud=DeviceCrudService(
                        settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
                    ),
                    publisher=event_bus,
                ),
                settings_services=SimpleNamespace(service=FakeSettingsReader({})),
                agent_comm=SimpleNamespace(circuit_breaker=Mock(), http_pool=None),
                appium_services=SimpleNamespace(reconciler_agent=SimpleNamespace(restart_node=fake_restart_node)),
            )

    async def enter_maintenance_before_restart() -> None:
        await asyncio.wait_for(restart_entered.wait(), timeout=2.0)
        async with db_session_maker() as session:
            locked = await device_locking.lock_device(session, device_id)
            await MaintenanceService(
                review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
            ).enter_maintenance(session, locked)
        allow_restart.set()

    await asyncio.gather(reconnect(), enter_maintenance_before_restart())

    async with db_session_maker() as verify:
        final = (
            await verify.execute(select(Device.operational_state_last_emitted).where(Device.id == device_id))
        ).one()

    # §4 (Phase 2): the concurrent maintenance signal derives onto the operational axis and
    # outranks the offline that the reconnect/restart race would otherwise produce.
    assert final.operational_state_last_emitted == DeviceOperationalState.maintenance
    # hold is now derived by the reconciler (Task 7+8); check the maintenance_reason signal instead
    from sqlalchemy import select as sa_select

    from app.devices.models import Device as DeviceModel
    from app.devices.services.lifecycle_policy_state import state as ps

    async with db_session_maker() as verify2:
        device_row = (await verify2.execute(sa_select(DeviceModel).where(DeviceModel.id == device_id))).scalar_one()
        assert ps(device_row).get("maintenance_reason") is not None
