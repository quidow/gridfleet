from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import NoResultFound

from app.appium_nodes.services import node_health
from app.appium_nodes.services.node_health import NodeHealthService
from app.devices.services import (
    intent_reconciler,
)
from app.devices.services.bulk import BulkOperationsService
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.data_cleanup import DataCleanupService
from app.devices.services.fleet_capacity import FleetCapacityService
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.presenter import DevicePresenterService
from app.devices.services.property_refresh import PropertyRefreshService
from app.devices.services.service import DeviceCrudService
from app.devices.services.test_data import TestDataService
from app.devices.services_container import DeviceServices
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class _Observation:
    @asynccontextmanager
    async def cycle(self) -> AsyncGenerator[AsyncMock]:
        yield AsyncMock()


@asynccontextmanager
async def _fake_session() -> AsyncGenerator[AsyncMock]:
    yield AsyncMock()


async def test_intent_reconciler_loop_logs_cycle_failure_and_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import background_loop

    monkeypatch.setattr(background_loop, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(
        intent_reconciler,
        "run_device_intent_reconciler_once",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    sleep = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(background_loop.asyncio, "sleep", sleep)

    _svc_settings_2 = FakeSettingsReader({"general.intent_reconcile_interval_sec": 1})
    _svc_pub_2 = AsyncMock()
    _svc_maint_2 = MaintenanceService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    )
    _svc_crud_2 = DeviceCrudService(
        settings=_svc_settings_2, identity=DeviceIdentityConflictService(), publisher=event_bus
    )
    loop = intent_reconciler.DeviceIntentReconcilerLoop(
        services=DeviceServices(
            fleet_capacity=FleetCapacityService(),
            data_cleanup=DataCleanupService(publisher=_svc_pub_2, settings=_svc_settings_2),
            property_refresh=PropertyRefreshService(discovery=Mock()),
            groups=DeviceGroupsService(publisher=_svc_pub_2, crud=_svc_crud_2),
            maintenance=_svc_maint_2,
            bulk=BulkOperationsService(
                publisher=_svc_pub_2,
                settings=_svc_settings_2,
                circuit_breaker=Mock(),
                maintenance=_svc_maint_2,
                crud=_svc_crud_2,
                operator=OperatorNodeLifecycleService(
                    review=build_review_service(), settings=_svc_settings_2, publisher=event_bus
                ),
            ),
            presenter=DevicePresenterService(settings=_svc_settings_2),
            test_data=TestDataService(publisher=_svc_pub_2),
            crud=_svc_crud_2,
            capability=DeviceCapabilityService(),
            connectivity=ConnectivityService(
                publisher=_svc_pub_2,
                settings=_svc_settings_2,
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=AsyncMock(),
            ),
            publisher=_svc_pub_2,
            settings=_svc_settings_2,
            session_factory=_fake_session,
            circuit_breaker=Mock(),
            health=AsyncMock(),
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await loop.run()

    # Sleeps the remainder of the 1s interval after a near-instant failed cycle
    # (cadence = interval - elapsed), not the full interval on top of cycle time.
    sleep.assert_awaited_once()
    (slept,) = sleep.await_args.args
    assert slept == pytest.approx(1.0, abs=0.1)


async def test_node_health_check_skips_device_deleted_after_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    device = Mock(id=__import__("uuid").uuid4(), host_id=__import__("uuid").uuid4())
    node = Mock(device=device, device_id=device.id, port=4723, pid=123, active_connection_target="serial")

    class Result:
        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return [node]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=Result())
    db.commit = AsyncMock()
    monkeypatch.setattr(node_health.device_locking, "lock_device", AsyncMock(side_effect=NoResultFound))

    from tests.fakes import FakeSettingsReader

    await NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        recovery_control=AsyncMock(),
        health=AsyncMock(),
        incidents=AsyncMock(),
    ).fold_host_nodes(
        db,
        device.host_id,
        {
            "reported_at": "2026-07-10T00:00:00+00:00",
            "nodes": [
                {
                    "port": node.port,
                    "pid": node.pid,
                    "connection_target": node.active_connection_target,
                    "running": True,
                }
            ],
        },
    )

    db.commit.assert_awaited_once()
