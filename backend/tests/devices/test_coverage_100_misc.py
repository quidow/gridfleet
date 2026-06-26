from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.agent_comm import reconfigure_delivery as agent_reconfigure_delivery
from app.appium_nodes.models import AppiumDesiredState
from app.appium_nodes.services import (
    common as node_service_common,
)
from app.appium_nodes.services import (
    desired_state_writer,
)
from app.appium_nodes.services import (
    reconciler as appium_reconciler,
)
from app.appium_nodes.services import (
    reconciler_agent as appium_reconciler_agent,
)
from app.auth.config import AuthConfig
from app.core.errors import PackDrainingError, _http_error_code
from app.core.leader import advisory as control_plane_leader_module
from app.core.leader import watcher as control_plane_leader_watcher
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState, DeviceType
from app.devices.schemas.device import AppiumNodeRead, DesiredNodeState
from app.devices.schemas.test_data import TestDataPayload
from app.devices.services import (
    bulk as bulk_service,
)
from app.devices.services import (
    capability as capability_service,
)
from app.devices.services import (
    data_cleanup,
    state_write_guard,
)
from app.devices.services import (
    groups as device_group_service,
)
from app.devices.services import (
    identity_conflicts as device_identity_conflicts,
)
from app.devices.services import (
    platform_label as platform_label_service,
)
from app.devices.services import (
    state as device_state,
)
from app.devices.services import test_data as test_data_service
from app.devices.services import (
    write as device_write,
)
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.presenter import DevicePresenterService as _DevicePresenterService
from app.devices.services.service import DeviceCrudService
from app.events import catalog as event_catalog
from app.hosts import service_versioning as host_versioning
from app.jobs.queue import DurableJobService
from app.lifecycle.services import recovery_job as device_recovery_job
from app.lifecycle.services.recovery_job import RecoveryJobService
from app.packs.manifest import AppiumInstallable
from app.packs.models import PackState
from app.packs.schemas import RuntimePolicy
from app.packs.services import (
    capability as pack_capability_service,
)
from app.packs.services import (
    discovery as pack_discovery_service,
)
from app.packs.services import (
    feature_dispatch as pack_feature_dispatch_service,
)
from app.packs.services import (
    platform_resolver as pack_platform_resolver,
)
from app.packs.services import release_ordering as pack_desired_state_service
from app.packs.services import (
    storage as pack_storage_service,
)
from app.packs.services.discovery import PackDiscoveryService as _PackDiscoveryService
from app.packs.services.driver_version import installed_driver_version as _installed_driver_version
from app.packs.services.feature_dispatch import FeatureService as PackFeatureService
from app.packs.services.lifecycle import PackLifecycleService
from app.packs.services.service import PackCatalogService
from app.packs.services.status import PackStatusService as _PackStatusService
from app.runs import service_reservation as run_reservation_service
from app.runs.models import TestRun
from app.runs.schemas import DeviceRequirement
from app.sessions import protocols as session_viability_protocols
from app.settings import registry as settings_registry
from app.settings import service_config as config_service
from app.settings.service_config import SettingsConfigService
from app.verification.services.execution import AgentCallContext, VerificationExecutionService
from app.verification.services.preparation import VerificationPreparationService
from app.verification.services.runner import VerificationRunnerService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from pathlib import Path

event_bus_mod = import_module("app.events.event_bus")


def test_config_and_error_guard_branches() -> None:
    with pytest.raises(ValidationError, match="at least 1 second"):
        AuthConfig(auth_session_ttl_sec=0)

    settings = AuthConfig(
        auth_enabled=True,
        auth_username="operator",
        auth_password="secret",
        auth_session_secret="session-secret-padded-to-32-bytes-min",
        machine_auth_username="machine",
        machine_auth_password="machine-secret",
    )
    assert settings.auth_enabled is True

    error = PackDrainingError("pack-a")
    assert error.pack_id == "pack-a"
    assert str(error) == "pack-a"
    assert _http_error_code(403) == "FORBIDDEN"


def test_schema_validator_and_model_guard_branches() -> None:
    assert AppiumInstallable(source="npm", package="appium", version=">=1").recommended is None

    run = TestRun(name="run")
    run.reserved_devices = None
    assert run.device_reservations == []

    with pytest.raises(ValidationError, match="version pins are only valid"):
        RuntimePolicy(mode="latest", appium_server_version="2.0.0")

    requirement = DeviceRequirement(pack_id="pack", platform_id="platform", allocation="all_available")
    assert requirement.min_count == 1
    with pytest.raises(ValidationError, match="min_count can only be provided"):
        DeviceRequirement(pack_id="pack", platform_id="platform", min_count=1)

    with pytest.raises(ValueError, match="JSON object"):
        TestDataPayload.model_construct(root=[])._validate()  # type: ignore[arg-type]


def test_device_readiness_effective_state_branches() -> None:
    now = datetime.now(UTC)
    naive_future = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    bad_timestamp = "not-a-date"

    assert (
        AppiumNodeRead(
            id=uuid.uuid4(),
            port=4723,
            pid=None,
            container_id=None,
            active_connection_target=None,
            started_at=now,
            desired_state=DesiredNodeState.running,
            lifecycle_policy_state={"recovery_suppressed_reason": "cooldown", "backoff_until": naive_future},
        ).effective_state
        == "blocked"
    )
    assert (
        AppiumNodeRead(
            id=uuid.uuid4(),
            port=4723,
            pid=123,
            container_id=None,
            active_connection_target=None,
            started_at=now,
            desired_state=DesiredNodeState.running,
            lifecycle_policy_state={"recovery_suppressed_reason": "cooldown", "backoff_until": bad_timestamp},
            health_state="ok",
            health_running=True,
        ).effective_state
        == "running"
    )


async def test_small_service_guard_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = AsyncMock()
    assert config_service._deep_merge({"a": {"b": 1}}, {"a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}
    audit_rows = [object()]
    db.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: audit_rows)))
    assert await SettingsConfigService(publisher=Mock()).get_config_history(db, uuid.uuid4(), limit=1) == audit_rows

    with pytest.raises(ValueError, match="desired_port"):
        await desired_state_writer.write_desired_state(
            db,
            node=SimpleNamespace(desired_state=AppiumDesiredState.running, transition_token=None, desired_port=1),
            caller="test",
            write=desired_state_writer.DesiredStateWrite(target=AppiumDesiredState.stopped, desired_port=1),
        )
    with pytest.raises(ValueError, match="transition_deadline"):
        await desired_state_writer.write_desired_state(
            db,
            node=SimpleNamespace(desired_state=AppiumDesiredState.running, transition_token=None, desired_port=1),
            caller="test",
            write=desired_state_writer.DesiredStateWrite(
                target=AppiumDesiredState.running, transition_token=uuid.uuid4()
            ),
        )

    assert (
        await device_identity_conflicts.DeviceIdentityConflictService().find_device_identity_conflict(
            db,
            identity_scope="global",
            identity_scheme=None,
            identity_value="serial",
            host_id=None,
        )
        is None
    )
    assert (
        await device_identity_conflicts.DeviceIdentityConflictService().find_device_identity_conflict(
            db,
            identity_scope="host",
            identity_scheme="serial",
            identity_value="serial",
            host_id=None,
        )
        is None
    )
    assert await device_locking.lock_devices(db, []) == []

    with state_write_guard.bypass():
        device = Device(id=uuid.uuid4(), name="d", operational_state=DeviceOperationalState.maintenance)
    monkeypatch.setattr(device_state, "_persistent_session", lambda _device: object())
    assert (
        await device_state.set_operational_state(
            device, DeviceOperationalState.maintenance, publish_event=False, publisher=event_bus
        )
        is False
    )

    assert event_catalog.normalize_public_event_names("bad") == []
    assert event_catalog.normalize_public_event_names(
        ["bad", 1, "device.operational_state_changed", "device.operational_state_changed"]
    ) == ["device.operational_state_changed"]
    assert host_versioning.normalize_agent_version_setting(123) is None

    device_for_caps = SimpleNamespace(
        id=uuid.uuid4(),
        name="device",
        ip_address=None,
        platform_id="android_mobile",
        device_type=DeviceType.real_device,
        os_version="unknown",
        manufacturer=None,
        model=None,
        device_config={},
        tags=None,
    )
    # Pack stereotype is the only source for routing keys; the builder no longer
    # injects browserName defaults of its own. The pack manifest decides whether
    # browserName belongs in the stereotype.
    pack_stereotype = {"browserName": "Chrome"}
    caps = node_service_common.build_grid_stereotype_caps(device_for_caps, pack_stereotype=pack_stereotype)
    assert caps["browserName"] == "Chrome"
    assert "gridfleet:deviceId" in caps

    storage = pack_storage_service.PackStorageService(tmp_path)
    with pytest.raises(pack_storage_service.PackStorageError):
        storage._safe_segment("")
    outside = tmp_path.parent / "outside-storage-file"
    outside.write_bytes(b"x")
    with pytest.raises(pack_storage_service.PackStorageError), storage.open(str(outside)):
        pass

    labels = await platform_label_service.load_platform_label_map(db, [])
    assert labels == {}
    label_db = AsyncMock()
    label_db.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])))
    assert await platform_label_service.load_platform_label(label_db, pack_id="pack", platform_id="platform") is None


async def test_pack_platform_and_capability_guard_branches() -> None:
    db = AsyncMock()
    device = SimpleNamespace(
        id=uuid.uuid4(),
        name="Pixel",
        device_type=DeviceType.simulator,
        appium_node=None,
        host_id=None,
    )

    with pytest.raises(LookupError, match="no resolved Appium platform name"):
        capability_service.build_capabilities(device, None, appium_platform_name=None)
    assert await capability_service._active_target_from_host_snapshot(db, device) is None
    device_with_node = SimpleNamespace(appium_node=SimpleNamespace(port=4723), host_id=uuid.uuid4())
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            capability_service.control_plane_state_store,
            "get_value",
            AsyncMock(return_value={"running_nodes": "bad"}),
        )
        assert await capability_service._active_target_from_host_snapshot(db, device_with_node) is None
    finally:
        monkeypatch.undo()

    disabled_pack = SimpleNamespace(state=PackState.disabled)
    draining_pack = SimpleNamespace(state=PackState.draining)
    unknown_pack = SimpleNamespace(state=PackState.draft)
    db.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalar_one_or_none=lambda: None),
            SimpleNamespace(scalar_one_or_none=lambda pack=disabled_pack: pack),
            SimpleNamespace(scalar_one_or_none=lambda pack=draining_pack: pack),
            SimpleNamespace(scalar_one_or_none=lambda pack=unknown_pack: pack),
        ]
    )

    with pytest.raises(pack_platform_resolver.PackUnavailableError):
        await pack_platform_resolver.assert_runnable(db, pack_id="missing", platform_id="p")
    with pytest.raises(pack_platform_resolver.PackDisabledError):
        await pack_platform_resolver.assert_runnable(db, pack_id="disabled", platform_id="p")
    with pytest.raises(pack_platform_resolver.PackDrainingError):
        await pack_platform_resolver.assert_runnable(db, pack_id="draining", platform_id="p")
    with pytest.raises(pack_platform_resolver.PackDisabledError):
        await pack_platform_resolver.assert_runnable(db, pack_id="uploaded", platform_id="p")


async def test_device_verification_runner_missing_job_branches() -> None:
    from app.verification.services.execution import AgentCallContext, VerificationExecutionService
    from app.verification.services.preparation import VerificationPreparationService
    from app.verification.services.runner import VerificationRunnerService

    class SessionCtx:
        async def __aenter__(self) -> AsyncMock:
            db = AsyncMock()
            db.get = AsyncMock(return_value=None)
            return db

        async def __aexit__(self, *_args: object) -> None:
            return None

    settings = FakeSettingsReader({})
    cb = Mock()
    publisher = AsyncMock()
    prep = VerificationPreparationService(
        settings=settings,
        circuit_breaker=cb,
        crud=DeviceCrudService(settings=settings, identity=DeviceIdentityConflictService(), publisher=event_bus),
        identity=DeviceIdentityConflictService(),
    )
    exec_svc = VerificationExecutionService(
        review=build_review_service(),
        publisher=publisher,
        agent=AgentCallContext(settings=settings, circuit_breaker=cb),
        crud=DeviceCrudService(settings=settings, identity=DeviceIdentityConflictService(), publisher=event_bus),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=AsyncMock(),
    )
    runner = VerificationRunnerService(
        session_factory=SessionCtx,
        publisher=publisher,
        settings=settings,
        circuit_breaker=cb,
        preparation=prep,
        execution=exec_svc,
    )
    assert await runner._load_persisted_job(str(uuid.uuid4())) is None
    await runner.run_persisted_verification_job(str(uuid.uuid4()), {"mode": "create"})


async def test_more_service_error_and_protocol_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    # HealthFailureHandler is a Protocol with ``...`` body; calling it exercises
    # the abstract stub without raising.
    await session_viability_protocols.HealthFailureHandler.__call__(
        object(),
        AsyncMock(),
        Device(id=uuid.uuid4(), name="d"),
        source="test",
        reason="boom",
    )

    leader = control_plane_leader_module.ControlPlaneLeader()

    class BadConnection:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("unlock failed")

        async def close(self) -> None:
            raise RuntimeError("close failed")

    leader._connection = BadConnection()  # type: ignore[assignment]
    await leader.release()
    assert leader._connection is None

    monkeypatch.setattr(control_plane_leader_watcher.os, "_exit", Mock(side_effect=SystemExit(70)))
    with pytest.raises(SystemExit):
        await control_plane_leader_watcher._exit_after_preempt()

    monkeypatch.setattr(event_bus, "publish", AsyncMock(side_effect=RuntimeError("publish failed")))
    monkeypatch.setattr(event_bus_mod.logger, "exception", Mock())
    await event_bus_mod._publish_pending_events(
        [("device.operational_state_changed", {"device_id": "d"}, None)], event_bus
    )
    event_bus_mod.logger.exception.assert_called_once()

    listeners: dict[str, object] = {}

    def capture_listener(_target: object, identifier: str, fn: object, **_kwargs: object) -> None:
        listeners[identifier] = fn

    monkeypatch.setattr(event_bus_mod.sa_event, "listen", capture_listener)
    sync_session = SimpleNamespace(info={})
    event_bus.queue_for_session(sync_session, "device.operational_state_changed", {"device_id": "d"})
    event_bus.queue_for_session(sync_session, "device.operational_state_changed", {"device_id": "d"})
    listener = sync_session.info[event_bus_mod._PENDING_EVENTS_LISTENER_KEY]
    assert listener is True
    event_bus._handler_tasks.clear()
    sync_session.info[event_bus_mod._PENDING_EVENTS_KEY] = []
    listeners["after_commit"](sync_session)  # type: ignore[operator]
    assert event_bus._handler_tasks == set()

    assert (
        appium_reconciler.detect_orphans(
            host_id=uuid.uuid4(),
            agent_running=[],
            db_running_rows=[{"host_id": uuid.uuid4(), "device_connection_target": "serial"}],
        )
        == []
    )
    assert await bulk_service._load_existing_device_ids(AsyncMock(), []) == []

    class SessionCtx:
        async def __aenter__(self) -> AsyncMock:
            return AsyncMock()

        async def __aexit__(self, *_args: object) -> None:
            return None

    from app.appium_nodes.services.reconciler import ReconcilerService

    clear = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=SessionCtx,
    )._clear_token_factory(require_leader=True, session_scope=SessionCtx)
    monkeypatch.setattr(appium_reconciler, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(appium_reconciler, "_clear_transition_token", AsyncMock())
    await clear(row=SimpleNamespace(device_id=uuid.uuid4()))
    appium_reconciler.assert_current_leader.assert_awaited_once()

    row = SimpleNamespace(
        device_id=uuid.uuid4(),
        reconciled_generation=1,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        grid_run_id=None,
        delivery_attempts=9,
        abandoned_at=None,
        abandoned_reason=None,
    )
    no_host_device = SimpleNamespace(host=None)

    class ExecuteResult:
        def __init__(self, value: object) -> None:
            self._value = value

        def scalar_one_or_none(self) -> object:
            return self._value

        def scalar_one(self) -> object:
            return self._value

        def scalars(self) -> ExecuteResult:
            return self

        def all(self) -> list[object]:
            return [row]

    reconfigure_db = AsyncMock()
    reconfigure_db.scalar = AsyncMock(return_value=1)
    reconfigure_db.execute = AsyncMock(
        side_effect=[
            ExecuteResult(None),
            ExecuteResult([row]),
            ExecuteResult(SimpleNamespace(generation=1)),
            ExecuteResult(no_host_device),
        ]
    )
    await agent_reconfigure_delivery.deliver_agent_reconfigures(
        reconfigure_db, row.device_id, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=Mock()
    )
    assert row.abandoned_reason == agent_reconfigure_delivery.ABANDONED_REASON_HOST_MISSING

    cleanup_db = AsyncMock()
    monkeypatch.setattr(data_cleanup, "_delete_in_batches", AsyncMock(return_value=0))
    await data_cleanup.DataCleanupService(
        publisher=AsyncMock(),
        settings=FakeSettingsReader(
            {
                "retention.audit_log_days": 0,
                "retention.event_log_days": 1,
                "retention.system_event_days": 1,
                "retention.background_loop_heartbeat_days": 1,
                "retention.automation_artifact_days": 1,
                "retention.host_resource_telemetry_hours": 1,
                "retention.hardware_telemetry_days": 1,
                "retention.test_data_audit_days": 1,
            }
        ),
    ).cleanup_old_data(cleanup_db)

    class Result:
        def __init__(self, value: object) -> None:
            self._value = value

        def scalar_one_or_none(self) -> object:
            return self._value

        def scalar_one(self) -> int:
            return 0

    delete_db = AsyncMock()
    delete_db.execute = AsyncMock(side_effect=[Result(SimpleNamespace(releases=[])), Result(0)])
    monkeypatch.setattr(
        PackLifecycleService,
        "count_active_work_for_pack",
        AsyncMock(return_value={"active_runs": 1, "live_sessions": 0}),
    )
    with pytest.raises(RuntimeError, match="active run"):
        await PackCatalogService(lifecycle=PackLifecycleService()).delete_pack(delete_db, "pack")


async def test_more_pack_and_reservation_helper_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    run_without_reservations = SimpleNamespace(device_reservations=[])
    assert run_reservation_service.get_reservation_entry_for_device(run_without_reservations, uuid.uuid4()) is None
    reservation_db = AsyncMock()
    reservation_db.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))
    )
    svc = run_reservation_service.RunReservationService(review=build_review_service())
    assert await svc.exclude_device_from_run(reservation_db, uuid.uuid4(), reason="r") is None
    assert await svc.restore_device_to_run(reservation_db, uuid.uuid4()) is None

    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    assert (
        await PackFeatureService(publisher=event_bus, circuit_breaker=Mock()).record_feature_status(
            db,
            host_id=uuid.uuid4(),
            pack_id="pack",
            feature_id="camera",
            ok=True,
            detail="ok",
        )
        is False
    )

    missing_pack_db = AsyncMock()
    missing_pack_db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    with pytest.raises(LookupError):
        await PackLifecycleService().try_complete_drain(missing_pack_db, "missing")
    with pytest.raises(LookupError):
        await PackLifecycleService().transition_pack_state(missing_pack_db, "missing", PackState.enabled)

    desired_pack = SimpleNamespace(releases=[], current_release=None)
    assert pack_desired_state_service.selected_release(desired_pack.releases, desired_pack.current_release) is None

    class DummyClient:
        async def get_pack_devices(
            self, _host: str, _port: int, *, settings: object, circuit_breaker: object, pool: object = None
        ) -> dict[str, object]:
            del settings, circuit_breaker, pool
            return {"devices": []}

    discovery_db = AsyncMock()
    discovery_db.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])))
    monkeypatch.setattr(
        pack_discovery_service.platform_label_service, "load_platform_label_map", AsyncMock(return_value={})
    )
    result = await _PackDiscoveryService(
        agent_get_pack_devices=DummyClient().get_pack_devices,
        agent_get_pack_device_properties=AsyncMock(return_value=None),
        settings=FakeSettingsReader(),
        circuit_breaker=Mock(),
        serializer=_DevicePresenterService(settings=FakeSettingsReader()),
        identity_guard=DeviceIdentityConflictService(),
    ).discover_devices(
        discovery_db,
        SimpleNamespace(id=uuid.uuid4(), ip="127.0.0.1", agent_port=5100),
    )
    assert result.new_devices == []

    class ClientManager:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        pack_feature_dispatch_service,
        "agent_request",
        AsyncMock(side_effect=pack_feature_dispatch_service.AgentCallError("host", "bad")),
    )
    with pytest.raises(pack_feature_dispatch_service._AgentDispatchError, match="Agent unreachable"):
        await PackFeatureService(publisher=event_bus, circuit_breaker=Mock())._call_agent(
            host="127.0.0.1",
            url="http://agent/pack-feature",
            body={"feature_id": "camera"},
            http_client_factory=lambda **_kwargs: ClientManager(),
            timeout=1,
        )
    monkeypatch.setattr(
        pack_feature_dispatch_service,
        "agent_request",
        AsyncMock(side_effect=pack_feature_dispatch_service.httpx.ConnectError("boom")),
    )
    with pytest.raises(pack_feature_dispatch_service._AgentDispatchError, match="Agent transport error"):
        await PackFeatureService(publisher=event_bus, circuit_breaker=Mock())._call_agent(
            host="127.0.0.1",
            url="http://agent/pack-feature",
            body={"feature_id": "camera"},
            http_client_factory=lambda **_kwargs: ClientManager(),
            timeout=1,
        )

    feature_db = AsyncMock()
    existing_status = SimpleNamespace(ok=True)
    feature_db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: existing_status))
    assert (
        await PackFeatureService(publisher=event_bus, circuit_breaker=Mock()).record_feature_status(
            feature_db,
            host_id=uuid.uuid4(),
            pack_id="pack",
            feature_id="camera",
            ok=True,
            detail="still ok",
        )
        is False
    )

    assert (
        await pack_capability_service.resolve_appium_env(
            AsyncMock(
                scalar=AsyncMock(
                    return_value=SimpleNamespace(
                        is_runnable=True,
                        releases=[SimpleNamespace(release="1", manifest_json={})],
                        current_release=None,
                    )
                )
            ),
            pack_id="pack",
            platform_id="android",
            device_type="real_device",
            os_version="1",
        )
        == {}
    )


async def test_remaining_small_service_branches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert (
        appium_reconciler_agent._short_session_factory(SimpleNamespace(bind=None))
        is appium_reconciler_agent.async_session
    )
    static_group = SimpleNamespace(
        id=uuid.uuid4(),
        name="static",
        description=None,
        group_type=device_group_service.GroupType.static,
        filters=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class GroupListResult:
        def __init__(self, value: object) -> None:
            self._value = value

        def scalars(self) -> GroupListResult:
            return self

        def all(self) -> object:
            return self._value

        def scalar_one_or_none(self) -> object:
            return self._value

    group_db = AsyncMock()
    group_db.execute = AsyncMock(
        side_effect=[GroupListResult([static_group]), SimpleNamespace(all=lambda: [(static_group.id, 2)])]
    )
    _gs1 = FakeSettingsReader({})
    listed = await device_group_service.DeviceGroupsService(
        publisher=event_bus,
        crud=DeviceCrudService(settings=_gs1, identity=DeviceIdentityConflictService(), publisher=event_bus),
    ).list_groups(group_db)
    assert listed[0]["device_count"] == 2
    missing_group_db = AsyncMock()
    missing_group_db.execute = AsyncMock(return_value=GroupListResult(None))
    _gs2 = FakeSettingsReader({})
    assert (
        await device_group_service.DeviceGroupsService(
            publisher=event_bus,
            crud=DeviceCrudService(settings=_gs2, identity=DeviceIdentityConflictService(), publisher=event_bus),
        ).delete_group(missing_group_db, uuid.uuid4())
        is False
    )

    assert device_write._is_transport_identity(identity_value="10.0.0.1:5555", connection_target=None, ip_address=None)
    assert device_write._is_transport_identity(identity_value="10.0.0.1", connection_target=None, ip_address=None)
    assert device_write._is_transport_identity(
        identity_value="10.0.0.1",
        connection_target="serial",
        ip_address="10.0.0.1",
    )
    assert device_write._is_transport_identity(
        identity_value="10.0.0.1:5555",
        connection_target="10.0.0.1:5555",
        ip_address=None,
    )

    assert (
        settings_registry._parse_env_value(
            settings_registry.SettingDefinition(
                key="x",
                category="general",
                setting_type="float",
                default=1.0,
                description="x",
            ),
            "1.5",
        )
        == 1.5
    )

    class TestDataDb:
        def add(self, _obj: object) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def refresh(self, _obj: object) -> None:
            return None

    test_data_db = TestDataDb()
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", Mock())
    device = SimpleNamespace(id=uuid.uuid4(), name="device", test_data={"a": 1})
    assert await test_data_service.TestDataService(publisher=event_bus).replace_device_test_data(
        test_data_db, device, {"b": 2}, changed_by="operator"
    ) == {"b": 2}

    host_db = AsyncMock()
    host_db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    from app.hosts.service import HostCrudService as _HostCrudService

    assert (
        await _HostCrudService(publisher=event_bus, settings=FakeSettingsReader({})).reject_host(host_db, uuid.uuid4())
        is False
    )

    class RecoveryCtx:
        async def __aenter__(self) -> AsyncMock:
            db = AsyncMock()
            db.get = AsyncMock(return_value=None)
            return db

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        device_recovery_job.device_locking,
        "lock_device",
        AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
    )
    mock_lifecycle_policy = AsyncMock()
    mock_lifecycle_policy.attempt_auto_recovery = AsyncMock(side_effect=RuntimeError("boom"))
    await device_recovery_job.RecoveryJobService(
        session_factory=RecoveryCtx,
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        lifecycle_policy=mock_lifecycle_policy,
    ).run_device_recovery_job(
        str(uuid.uuid4()),
        {"device_id": str(uuid.uuid4())},
    )

    class QueueCtx:
        async def __aenter__(self) -> AsyncMock:
            db = AsyncMock()
            db.get = AsyncMock(return_value=None)
            return db

        async def __aexit__(self, *_args: object) -> None:
            return None

    job = SimpleNamespace(id=uuid.uuid4(), kind="demo", snapshot={})
    service = DurableJobService(
        session_factory=QueueCtx,
        publisher=AsyncMock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        verification_runner=VerificationRunnerService(
            session_factory=QueueCtx,
            publisher=AsyncMock(),
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            preparation=VerificationPreparationService(
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                crud=DeviceCrudService(
                    settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
                ),
                identity=DeviceIdentityConflictService(),
            ),
            execution=VerificationExecutionService(
                review=build_review_service(),
                publisher=AsyncMock(),
                agent=AgentCallContext(settings=FakeSettingsReader({}), circuit_breaker=Mock()),
                crud=DeviceCrudService(
                    settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
                ),
                viability=Mock(),
                capability=DeviceCapabilityService(),
                reconciler=AsyncMock(),
                node_manager=AsyncMock(),
            ),
        ),
        recovery_runner=RecoveryJobService(
            session_factory=QueueCtx,
            publisher=AsyncMock(),
            settings=FakeSettingsReader({}),
            lifecycle_policy=AsyncMock(),
        ),
    )
    monkeypatch.setattr(service, "claim_next_job", AsyncMock(return_value=job))
    assert await service.run_pending_once() is True

    desired_db = AsyncMock()
    desired_db.get = AsyncMock(return_value=None)
    desired_db.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [SimpleNamespace(releases=[], current_release=None)])
            ),
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])),
        ]
    )
    assert (await _PackStatusService(feature=Mock()).compute_desired(desired_db, uuid.uuid4()))["packs"] == []

    assert _installed_driver_version(SimpleNamespace(driver_specs=[{"version": "1.2.3"}])) == "1.2.3"

    label_db = AsyncMock()
    label_db.execute = AsyncMock(
        return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [SimpleNamespace(releases=[], current_release=None)])
        )
    )
    assert await platform_label_service.load_platform_label_map(label_db, [("pack", "platform")]) == {
        ("pack", "platform"): None
    }

    released_entry = SimpleNamespace(device_id=uuid.uuid4(), released_at=datetime.now(UTC))
    run = SimpleNamespace(device_reservations=[released_entry])
    assert run_reservation_service.get_reservation_entry_for_device(run, released_entry.device_id) is None
