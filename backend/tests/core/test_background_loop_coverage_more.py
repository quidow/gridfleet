from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import NoResultFound

from app.appium_nodes.services import node_health as node_health
from app.appium_nodes.services.node_health import NodeHealthLoop, NodeHealthService
from app.appium_nodes.services_container import AppiumNodeServices
from app.core.leader import keepalive as control_plane_leader_keepalive
from app.core.leader.advisory import LeadershipLost
from app.devices.services import (
    connectivity as device_connectivity,
)
from app.devices.services import (
    data_cleanup as data_cleanup,
)
from app.devices.services import (
    intent_reconciler as intent_reconciler,
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
from app.runs import service_reaper as run_reaper
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class _Observation:
    @asynccontextmanager
    async def cycle(self) -> AsyncGenerator[AsyncMock, None]:
        yield AsyncMock()


@asynccontextmanager
async def _fake_session() -> AsyncGenerator[AsyncMock, None]:
    yield AsyncMock()


async def test_intent_reconciler_loop_exits_on_leadership_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.background_loop as background_loop

    monkeypatch.setattr(background_loop, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(
        intent_reconciler,
        "run_device_intent_reconciler_once",
        AsyncMock(side_effect=LeadershipLost("stale leader")),
    )
    monkeypatch.setattr(background_loop.os, "_exit", Mock(side_effect=SystemExit(70)))

    _svc_settings_1 = FakeSettingsReader({"general.intent_reconcile_interval_sec": 1})
    _svc_pub_1 = AsyncMock()
    _svc_maint_1 = MaintenanceService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    )
    _svc_crud_1 = DeviceCrudService(
        settings=_svc_settings_1, identity=DeviceIdentityConflictService(), publisher=event_bus
    )
    loop = intent_reconciler.DeviceIntentReconcilerLoop(
        services=DeviceServices(
            fleet_capacity=FleetCapacityService(),
            data_cleanup=DataCleanupService(publisher=_svc_pub_1, settings=_svc_settings_1),
            property_refresh=PropertyRefreshService(discovery=Mock()),
            groups=DeviceGroupsService(publisher=_svc_pub_1, settings=_svc_settings_1, crud=_svc_crud_1),
            maintenance=_svc_maint_1,
            bulk=BulkOperationsService(
                publisher=_svc_pub_1,
                settings=_svc_settings_1,
                circuit_breaker=Mock(),
                maintenance=_svc_maint_1,
                crud=_svc_crud_1,
                operator=OperatorNodeLifecycleService(
                    review=build_review_service(), settings=_svc_settings_1, publisher=event_bus
                ),
            ),
            presenter=DevicePresenterService(settings=_svc_settings_1),
            test_data=TestDataService(publisher=_svc_pub_1),
            crud=_svc_crud_1,
            capability=DeviceCapabilityService(),
            connectivity=ConnectivityService(
                publisher=_svc_pub_1,
                settings=_svc_settings_1,
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=AsyncMock(),
            ),
            publisher=_svc_pub_1,
            settings=_svc_settings_1,
            session_factory=_fake_session,
            circuit_breaker=Mock(),
            health=AsyncMock(),
        )
    )

    with pytest.raises(SystemExit):
        await loop.run()

    background_loop.os._exit.assert_called_once_with(70)


async def test_intent_reconciler_loop_logs_cycle_failure_and_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.background_loop as background_loop

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
            groups=DeviceGroupsService(publisher=_svc_pub_2, settings=_svc_settings_2, crud=_svc_crud_2),
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


async def test_node_health_loop_exits_on_leadership_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.background_loop as background_loop

    monkeypatch.setattr(background_loop, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(NodeHealthService, "check_nodes", AsyncMock(side_effect=LeadershipLost("stale leader")))
    monkeypatch.setattr(background_loop.os, "_exit", Mock(side_effect=SystemExit(70)))

    settings = FakeSettingsReader({"general.node_check_interval_sec": 1})
    node_health_svc = NodeHealthService(
        publisher=Mock(),
        settings=settings,
        pool=Mock(),
        circuit_breaker=Mock(),
        recovery_control=AsyncMock(),
        health=AsyncMock(),
        incidents=AsyncMock(),
    )
    loop = NodeHealthLoop(
        services=AppiumNodeServices(
            settings=settings,
            reconciler=Mock(),
            reconciler_agent=Mock(),
            node_health=node_health_svc,
            heartbeat=Mock(),
            session_factory=_fake_session,
        )
    )
    with pytest.raises(SystemExit):
        await loop.run()

    background_loop.os._exit.assert_called_once_with(70)


async def test_node_health_check_skips_device_deleted_after_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    device = Mock(id=__import__("uuid").uuid4(), host_id=__import__("uuid").uuid4())
    node = Mock(device=device, port=4723, pid=123, active_connection_target="serial")

    class Result:
        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return [node]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=Result())
    db.commit = AsyncMock()
    monkeypatch.setattr(NodeHealthService, "_bounded_check_node_health", AsyncMock(return_value={"healthy": True}))
    monkeypatch.setattr(node_health, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(node_health.device_locking, "lock_device", AsyncMock(side_effect=NoResultFound))

    from tests.fakes import FakeSettingsReader

    await NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        recovery_control=AsyncMock(),
        health=AsyncMock(),
        incidents=AsyncMock(),
    ).check_nodes(db)

    db.commit.assert_awaited_once()


async def test_device_connectivity_loop_exits_on_leadership_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.background_loop as background_loop

    monkeypatch.setattr(background_loop, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(ConnectivityService, "check_expired_cooldowns", AsyncMock())
    monkeypatch.setattr(
        ConnectivityService,
        "check_connectivity",
        AsyncMock(side_effect=LeadershipLost("stale leader")),
    )
    monkeypatch.setattr(background_loop.os, "_exit", Mock(side_effect=SystemExit(70)))

    _svc_settings_3 = FakeSettingsReader({})
    _svc_pub_3 = AsyncMock()
    _svc_maint_3 = MaintenanceService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    )
    _svc_crud_3 = DeviceCrudService(
        settings=_svc_settings_3, identity=DeviceIdentityConflictService(), publisher=event_bus
    )
    loop = device_connectivity.DeviceConnectivityLoop(
        services=DeviceServices(
            fleet_capacity=FleetCapacityService(),
            data_cleanup=DataCleanupService(publisher=_svc_pub_3, settings=_svc_settings_3),
            property_refresh=PropertyRefreshService(discovery=Mock()),
            groups=DeviceGroupsService(publisher=_svc_pub_3, settings=_svc_settings_3, crud=_svc_crud_3),
            maintenance=_svc_maint_3,
            bulk=BulkOperationsService(
                publisher=_svc_pub_3,
                settings=_svc_settings_3,
                circuit_breaker=Mock(),
                maintenance=_svc_maint_3,
                crud=_svc_crud_3,
                operator=OperatorNodeLifecycleService(
                    review=build_review_service(), settings=_svc_settings_3, publisher=event_bus
                ),
            ),
            presenter=DevicePresenterService(settings=_svc_settings_3),
            test_data=TestDataService(publisher=_svc_pub_3),
            crud=_svc_crud_3,
            capability=DeviceCapabilityService(),
            connectivity=ConnectivityService(
                publisher=_svc_pub_3,
                settings=_svc_settings_3,
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=AsyncMock(),
            ),
            publisher=_svc_pub_3,
            settings=_svc_settings_3,
            session_factory=_fake_session,
            circuit_breaker=Mock(),
            health=AsyncMock(),
        )
    )

    with pytest.raises(SystemExit):
        await loop.run()

    background_loop.os._exit.assert_called_once_with(70)


async def test_run_reaper_loop_exits_on_initial_leadership_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import app.core.background_loop as background_loop

    monkeypatch.setattr(background_loop, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(
        run_reaper.RunReaperLoop, "_reap_stale_runs", AsyncMock(side_effect=LeadershipLost("stale leader"))
    )
    monkeypatch.setattr(background_loop.os, "_exit", Mock(side_effect=SystemExit(70)))

    mock_services = SimpleNamespace(
        lifecycle=AsyncMock(),
        settings=FakeSettingsReader({"reservations.reaper_interval_sec": 1}),
        session_factory=_fake_session,
    )
    loop = run_reaper.RunReaperLoop(services=mock_services)  # type: ignore[arg-type]

    with pytest.raises(SystemExit):
        await loop.run()

    background_loop.os._exit.assert_called_once_with(70)


async def test_run_reaper_loop_exits_on_repeated_leadership_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import app.core.background_loop as background_loop

    monkeypatch.setattr(background_loop, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(
        run_reaper.RunReaperLoop,
        "_reap_stale_runs",
        AsyncMock(side_effect=[None, LeadershipLost("stale leader")]),
    )
    monkeypatch.setattr(background_loop.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(background_loop.os, "_exit", Mock(side_effect=SystemExit(70)))

    mock_services = SimpleNamespace(
        lifecycle=AsyncMock(),
        settings=FakeSettingsReader({"reservations.reaper_interval_sec": 1}),
        session_factory=_fake_session,
    )
    loop = run_reaper.RunReaperLoop(services=mock_services)  # type: ignore[arg-type]

    with pytest.raises(SystemExit):
        await loop.run()

    background_loop.os._exit.assert_called_once_with(70)


async def test_data_cleanup_loop_logs_failure_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.background_loop as background_loop

    monkeypatch.setattr(data_cleanup, "schedule_background_loop", AsyncMock())
    monkeypatch.setattr(background_loop, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(
        data_cleanup.DataCleanupService, "cleanup_old_data", AsyncMock(side_effect=RuntimeError("boom"))
    )
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr(background_loop.asyncio, "sleep", sleep)

    _svc_settings_4 = FakeSettingsReader({})
    _svc_pub_4 = AsyncMock()
    _svc_maint_4 = MaintenanceService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    )
    _svc_crud_4 = DeviceCrudService(
        settings=_svc_settings_4, identity=DeviceIdentityConflictService(), publisher=event_bus
    )
    loop = data_cleanup.DataCleanupLoop(
        services=DeviceServices(
            fleet_capacity=FleetCapacityService(),
            data_cleanup=DataCleanupService(publisher=_svc_pub_4, settings=_svc_settings_4),
            property_refresh=PropertyRefreshService(discovery=Mock()),
            groups=DeviceGroupsService(publisher=_svc_pub_4, settings=_svc_settings_4, crud=_svc_crud_4),
            maintenance=_svc_maint_4,
            bulk=BulkOperationsService(
                publisher=_svc_pub_4,
                settings=_svc_settings_4,
                circuit_breaker=Mock(),
                maintenance=_svc_maint_4,
                crud=_svc_crud_4,
                operator=OperatorNodeLifecycleService(
                    review=build_review_service(), settings=_svc_settings_4, publisher=event_bus
                ),
            ),
            presenter=DevicePresenterService(settings=_svc_settings_4),
            test_data=TestDataService(publisher=_svc_pub_4),
            crud=_svc_crud_4,
            capability=DeviceCapabilityService(),
            connectivity=ConnectivityService(
                publisher=_svc_pub_4,
                settings=_svc_settings_4,
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=AsyncMock(),
            ),
            publisher=_svc_pub_4,
            settings=_svc_settings_4,
            session_factory=_fake_session,
            circuit_breaker=Mock(),
            health=AsyncMock(),
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await loop.run()

    data_cleanup.schedule_background_loop.assert_awaited_once_with(data_cleanup.LOOP_NAME, 3600.0)
    sleep.assert_any_await(3600.0)


async def test_control_plane_leader_keepalive_loop_exits_on_leadership_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = FakeSettingsReader({"general.leader_keepalive_interval_sec": 1})
    monkeypatch.setattr(control_plane_leader_keepalive, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(
        control_plane_leader_keepalive,
        "run_keepalive_once",
        AsyncMock(side_effect=LeadershipLost("stale leader")),
    )
    monkeypatch.setattr(control_plane_leader_keepalive.os, "_exit", Mock(side_effect=SystemExit(70)))

    loop = control_plane_leader_keepalive.LeaderKeepaliveLoop(settings=settings)

    with pytest.raises(SystemExit):
        await loop.run()

    control_plane_leader_keepalive.os._exit.assert_called_once_with(70)
