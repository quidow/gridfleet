from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.agent_comm import reconfigure_delivery as agent_reconfigure_delivery
from app.appium_nodes.models import AppiumDesiredState
from app.appium_nodes.services import (
    common as node_service_common,
)
from app.appium_nodes.services import (
    desired_state_writer as desired_state_writer,
)
from app.appium_nodes.services import (
    reconciler as appium_reconciler,
)
from app.appium_nodes.services import (
    reconciler_agent as appium_reconciler_agent,
)
from app.core.config import Settings
from app.core.errors import PackDrainingError, _http_error_code
from app.core.observability import sanitize_log_value
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceHold, DeviceType
from app.devices.schemas.device import AppiumNodeRead, DesiredNodeState, SessionCreate
from app.devices.schemas.test_data import TestDataPayload
from app.devices.services import (
    bulk as bulk_service,
)
from app.devices.services import (
    capability as capability_service,
)
from app.devices.services import (
    data_cleanup as data_cleanup,
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
    recovery_job as device_recovery_job,
)
from app.devices.services import (
    state as device_state,
)
from app.devices.services import test_data as test_data_service
from app.devices.services import (
    write as device_write,
)
from app.events import catalog as event_catalog
from app.hosts import service as host_service
from app.hosts import service_versioning as host_versioning
from app.jobs import queue as job_queue
from app.packs.manifest import AppiumInstallable
from app.packs.models import PackState
from app.packs.schemas import RuntimePolicy
from app.packs.services import (
    capability as pack_capability_service,
)
from app.packs.services import (
    delete as pack_delete_service,
)
from app.packs.services import (
    desired_state as pack_desired_state_service,
)
from app.packs.services import (
    discovery as pack_discovery_service,
)
from app.packs.services import (
    export as pack_export_service,
)
from app.packs.services import (
    feature_dispatch as pack_feature_dispatch_service,
)
from app.packs.services import (
    feature_status as pack_feature_status_service,
)
from app.packs.services import (
    lifecycle as pack_lifecycle_service,
)
from app.packs.services import (
    platform_resolver as pack_platform_resolver,
)
from app.packs.services import (
    status as pack_status_service,
)
from app.packs.services import (
    storage as pack_storage_service,
)
from app.packs.services import (
    template as pack_template_service,
)
from app.plugins import service as plugin_service
from app.runs import service_reservation as run_reservation_service
from app.runs.models import TestRun
from app.runs.schemas import DeviceRequirement
from app.seeding import runner as seeding_runner
from app.seeding.runner import SeedResult, wipe_all_tables
from app.seeding.scenarios import full_demo
from app.services import (
    control_plane_leader as control_plane_leader_module,
)
from app.services import control_plane_leader_watcher, event_bus
from app.sessions import service_viability as session_viability
from app.settings import registry as settings_registry
from app.settings import service_config as config_service
from app.webhooks.schemas import WebhookUpdate


def test_config_and_error_guard_branches() -> None:
    with pytest.raises(ValidationError, match="at least 1 second"):
        Settings(auth_session_ttl_sec=0)

    settings = Settings(
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
    assert sanitize_log_value("x" * 250, max_length=5) == "xxxxx..."
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

    assert WebhookUpdate(event_types=None).event_types is None
    with pytest.raises(ValueError, match="JSON object"):
        TestDataPayload.model_construct(root=[])._validate()  # type: ignore[arg-type]

    session_create = SessionCreate(session_id="session-1", requested_capabilities=None)
    assert session_create.requested_capabilities is None


def test_device_readiness_and_full_demo_helper_branches() -> None:
    now = datetime.now(UTC)
    naive_future = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    bad_timestamp = "not-a-date"

    assert (
        AppiumNodeRead(
            id=uuid.uuid4(),
            port=4723,
            grid_url="http://grid",
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
            grid_url="http://grid",
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

    states = full_demo._assign_terminal_states(random.Random(1), 1)  # type: ignore[attr-defined]
    assert len(states) == 1
    assert len(full_demo._assign_terminal_states(random.Random(2), 3)) == 3  # type: ignore[attr-defined]
    assert full_demo._has_started_node_setup(SimpleNamespace(pack_id="appium-roku-dlenroc", device_config={})) is False
    assert (
        full_demo._has_started_node_setup(
            SimpleNamespace(pack_id="appium-roku-dlenroc", device_config={"roku_password": "secret"})
        )
        is True
    )


async def test_small_service_guard_branches(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    db = AsyncMock()
    await wipe_all_tables(db, table_names=["alembic_version"])
    db.execute.assert_not_awaited()

    assert SeedResult(scenario="demo", row_counts={"a": 2, "b": 3}, elapsed_seconds=0.1).rows_written == 5
    assert config_service._deep_merge({"a": {"b": 1}}, {"a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}
    audit_rows = [object()]
    db.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: audit_rows)))
    assert await config_service.get_config_history(db, uuid.uuid4(), limit=1) == audit_rows

    with pytest.raises(ValueError, match="desired_port"):
        await desired_state_writer.write_desired_state(
            db,
            node=SimpleNamespace(desired_state=AppiumDesiredState.running, transition_token=None, desired_port=1),
            target=AppiumDesiredState.stopped,
            caller="test",
            desired_port=1,
        )
    with pytest.raises(ValueError, match="transition_deadline"):
        await desired_state_writer.write_desired_state(
            db,
            node=SimpleNamespace(desired_state=AppiumDesiredState.running, transition_token=None, desired_port=1),
            target=AppiumDesiredState.running,
            caller="test",
            transition_token=uuid.uuid4(),
        )

    assert (
        await device_identity_conflicts.find_device_identity_conflict(
            db,
            identity_scope="global",
            identity_scheme=None,
            identity_value="serial",
            host_id=None,
        )
        is None
    )
    assert (
        await device_identity_conflicts.find_device_identity_conflict(
            db,
            identity_scope="host",
            identity_scheme="serial",
            identity_value="serial",
            host_id=None,
        )
        is None
    )
    assert await device_locking.lock_devices(db, []) == []

    device = Device(id=uuid.uuid4(), name="d", hold=DeviceHold.maintenance)
    monkeypatch.setattr(device_state, "_persistent_session", lambda _device: object())
    assert await device_state.set_hold(device, DeviceHold.maintenance) is False

    assert event_catalog.normalize_public_event_names("bad") == []
    assert event_catalog.normalize_public_event_names(["bad", 1, "device.hold_changed", "device.hold_changed"]) == [
        "device.hold_changed"
    ]
    assert host_versioning.normalize_agent_version_setting(123) is None

    monkeypatch.setattr(node_service_common.settings_service, "get", lambda _key: [])
    assert node_service_common.get_default_plugins() == []
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
    monkeypatch_default = {"android_mobile": "Chrome"}
    original = node_service_common.DEFAULT_GRID_BROWSER_BY_PLATFORM
    node_service_common.DEFAULT_GRID_BROWSER_BY_PLATFORM = monkeypatch_default
    try:
        assert node_service_common.build_grid_stereotype_caps(device_for_caps)["browserName"] == "Chrome"
        assert (
            node_service_common.build_grid_stereotype_caps(device_for_caps, extra_caps={"browserName": "Safari"})[
                "browserName"
            ]
            == "Safari"
        )
    finally:
        node_service_common.DEFAULT_GRID_BROWSER_BY_PLATFORM = original

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
    db.scalar = AsyncMock(side_effect=[None, disabled_pack, draining_pack, unknown_pack])

    with pytest.raises(pack_platform_resolver.PackUnavailableError):
        await pack_platform_resolver.assert_runnable(db, pack_id="missing", platform_id="p")
    with pytest.raises(pack_platform_resolver.PackDisabledError):
        await pack_platform_resolver.assert_runnable(db, pack_id="disabled", platform_id="p")
    with pytest.raises(pack_platform_resolver.PackDrainingError):
        await pack_platform_resolver.assert_runnable(db, pack_id="draining", platform_id="p")
    with pytest.raises(pack_platform_resolver.PackDisabledError):
        await pack_platform_resolver.assert_runnable(db, pack_id="uploaded", platform_id="p")


async def test_device_verification_runner_missing_job_branches() -> None:
    from app.devices.services import verification_runner as device_verification_runner

    class SessionCtx:
        async def __aenter__(self) -> AsyncMock:
            db = AsyncMock()
            db.get = AsyncMock(return_value=None)
            return db

        async def __aexit__(self, *_args: object) -> None:
            return None

    assert await device_verification_runner._load_persisted_job(str(uuid.uuid4()), SessionCtx) is None
    await device_verification_runner.run_persisted_verification_job(str(uuid.uuid4()), {"mode": "create"}, SessionCtx)


async def test_more_service_error_and_protocol_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(NotImplementedError):
        await pack_discovery_service.AgentClient.get_pack_devices(object(), "127.0.0.1", 5100)
    with pytest.raises(NotImplementedError):
        await session_viability.HealthFailureHandler.__call__(
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

    monkeypatch.setattr(event_bus.event_bus, "publish", AsyncMock(side_effect=RuntimeError("publish failed")))
    monkeypatch.setattr(event_bus.logger, "exception", Mock())
    await event_bus._publish_pending_events([("device.hold_changed", {"device_id": "d"})])
    event_bus.logger.exception.assert_called_once()

    listeners: dict[str, object] = {}

    def capture_listener(_target: object, identifier: str, fn: object, **_kwargs: object) -> None:
        listeners[identifier] = fn

    monkeypatch.setattr(event_bus.sa_event, "listen", capture_listener)
    sync_session = SimpleNamespace(info={})
    event_bus.queue_event_for_session(sync_session, "device.hold_changed", {"device_id": "d"})
    event_bus.queue_event_for_session(sync_session, "device.hold_changed", {"device_id": "d"})
    listener = sync_session.info[event_bus._PENDING_EVENTS_LISTENER_KEY]
    assert listener is True
    event_bus.event_bus._handler_tasks.clear()
    sync_session.info[event_bus._PENDING_EVENTS_KEY] = []
    listeners["after_commit"](sync_session)  # type: ignore[operator]
    assert event_bus.event_bus._handler_tasks == set()

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

    clear = appium_reconciler._clear_token_factory(require_leader=True, session_scope=SessionCtx)
    monkeypatch.setattr(appium_reconciler, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(appium_reconciler, "_clear_transition_token", AsyncMock())
    await clear(row=SimpleNamespace(device_id=uuid.uuid4()), reason="done")
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
    await agent_reconfigure_delivery.deliver_agent_reconfigures(reconfigure_db, row.device_id)
    assert row.abandoned_reason == agent_reconfigure_delivery.ABANDONED_REASON_HOST_MISSING

    monkeypatch.setattr(data_cleanup.settings_service, "get", lambda key: 0 if key == "retention.audit_log_days" else 1)
    cleanup_db = AsyncMock()
    monkeypatch.setattr(data_cleanup, "_delete_in_batches", AsyncMock(return_value=0))
    monkeypatch.setattr(data_cleanup.event_bus, "publish", AsyncMock())
    await data_cleanup._cleanup_old_data(cleanup_db)

    assert session_viability._format_http_error(
        session_viability.httpx.RequestError(
            "",
            request=session_viability.httpx.Request("GET", "http://grid/session"),
        )
    ) == ("RequestError while calling http://grid/session")

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
        pack_delete_service,
        "count_active_work_for_pack",
        AsyncMock(return_value={"active_runs": 1, "live_sessions": 0}),
    )
    with pytest.raises(RuntimeError, match="active run"):
        await pack_delete_service.delete_pack(delete_db, "pack")


async def test_more_pack_and_reservation_helper_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    run_without_reservations = SimpleNamespace(device_reservations=[])
    assert run_reservation_service.get_reservation_entry_for_device(run_without_reservations, uuid.uuid4()) is None
    reservation_db = AsyncMock()
    reservation_db.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))
    )
    assert await run_reservation_service.exclude_device_from_run(reservation_db, uuid.uuid4(), reason="r") is None
    assert await run_reservation_service.restore_device_to_run(reservation_db, uuid.uuid4()) is None

    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    assert (
        await pack_feature_status_service.record_feature_status(
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
    missing_pack_db.get = AsyncMock(return_value=None)
    with pytest.raises(LookupError):
        await pack_lifecycle_service.try_complete_drain(missing_pack_db, "missing")
    missing_pack_db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    with pytest.raises(LookupError):
        await pack_lifecycle_service.transition_pack_state(missing_pack_db, "missing", PackState.enabled)

    desired_pack = SimpleNamespace(releases=[], current_release=None)
    assert pack_desired_state_service.selected_release(desired_pack.releases, desired_pack.current_release) is None

    class DummyClient:
        async def get_pack_devices(self, _host: str, _port: int) -> dict[str, object]:
            return {"devices": []}

    discovery_db = AsyncMock()
    discovery_db.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])))
    monkeypatch.setattr(
        pack_discovery_service.platform_label_service, "load_platform_label_map", AsyncMock(return_value={})
    )
    result = await pack_discovery_service.discover_devices(
        discovery_db,
        SimpleNamespace(id=uuid.uuid4(), ip="127.0.0.1", agent_port=5100),
        agent_get_pack_devices=DummyClient().get_pack_devices,
    )
    assert result.new_devices == []

    monkeypatch.setattr(plugin_service, "list_agent_plugins", AsyncMock(return_value=[{"name": "images"}]))
    assert await plugin_service.fetch_host_plugins(SimpleNamespace(ip="127.0.0.1", agent_port=5100)) == [
        {"name": "images"}
    ]

    offline_host = SimpleNamespace(status=SimpleNamespace(value="offline"), hostname="host")
    assert await plugin_service.auto_sync_host_plugins(offline_host, [{"name": "images"}]) is None
    online_host = SimpleNamespace(status=SimpleNamespace(value="online"), hostname="host")
    assert await plugin_service.auto_sync_host_plugins(online_host, []) is None

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
        await pack_feature_dispatch_service._call_agent(
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
        await pack_feature_dispatch_service._call_agent(
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
        await pack_feature_status_service.record_feature_status(
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
        await pack_capability_service.resolve_workaround_env(
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


async def test_remaining_small_service_branches(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    assert (
        appium_reconciler_agent._short_session_factory(SimpleNamespace(bind=None))
        is appium_reconciler_agent.async_session
    )
    monkeypatch.setattr(appium_reconciler_agent, "candidate_ports", AsyncMock(return_value=[4799]))
    assert await appium_reconciler_agent.allocate_port(AsyncMock(), host_id=uuid.uuid4()) == 4799

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
    listed = await device_group_service.list_groups(group_db)
    assert listed[0]["device_count"] == 2
    missing_group_db = AsyncMock()
    missing_group_db.execute = AsyncMock(return_value=GroupListResult(None))
    assert await device_group_service.delete_group(missing_group_db, uuid.uuid4()) is False

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
    monkeypatch.setattr(test_data_service, "queue_event_for_session", Mock())
    device = SimpleNamespace(id=uuid.uuid4(), name="device", test_data={"a": 1})
    assert await test_data_service.replace_device_test_data(test_data_db, device, {"b": 2}, changed_by="operator") == {
        "b": 2
    }

    host_db = AsyncMock()
    host_db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    assert await host_service.reject_host(host_db, uuid.uuid4()) is False

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
    monkeypatch.setattr(
        device_recovery_job.lifecycle_policy,
        "attempt_auto_recovery",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    await device_recovery_job.run_device_recovery_job(
        str(uuid.uuid4()),
        {"device_id": str(uuid.uuid4())},
        session_factory=RecoveryCtx,
    )

    class QueueCtx:
        async def __aenter__(self) -> AsyncMock:
            db = AsyncMock()
            db.get = AsyncMock(return_value=None)
            return db

        async def __aexit__(self, *_args: object) -> None:
            return None

    job = SimpleNamespace(id=uuid.uuid4(), kind="demo", snapshot={})
    monkeypatch.setattr(job_queue, "claim_next_job", AsyncMock(return_value=job))
    assert await job_queue.run_pending_jobs_once(QueueCtx) is True

    storage = pack_storage_service.PackStorageService(tmp_path)
    outside_artifact = tmp_path.parent / "outside-pack-artifact.tar.gz"
    outside_artifact.write_bytes(b"x")
    release_row = SimpleNamespace(artifact_path=str(outside_artifact), manifest_json={})
    export_db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: release_row,
            )
        )
    )
    with pytest.raises(LookupError, match="not readable"):
        await pack_export_service.export_pack(
            export_db,
            storage,
            "pack",
            "release",
        )

    monkeypatch.setattr(pack_template_service, "_TEMPLATES_DIR", tmp_path / "missing")
    assert pack_template_service._load_all_templates() == {}
    invalid_template = tmp_path / "bad.yaml"
    invalid_template.write_text("- bad\n", encoding="utf-8")
    monkeypatch.setattr(pack_template_service, "_TEMPLATES_DIR", tmp_path)
    assert pack_template_service._load_all_templates() == {}

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
    assert (await pack_desired_state_service.compute_desired(desired_db, uuid.uuid4()))["packs"] == []

    assert (
        pack_status_service._installed_driver_version(
            SimpleNamespace(runtime_id="runtime-a"),
            {"runtime-a": SimpleNamespace(driver_specs=[{"version": "1.2.3"}])},
        )
        == "1.2.3"
    )

    label_db = AsyncMock()
    label_db.execute = AsyncMock(
        return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [SimpleNamespace(releases=[], current_release=None)])
        )
    )
    assert await platform_label_service.load_platform_label_map(label_db, [("pack", "platform")]) == {
        ("pack", "platform"): None
    }

    monkeypatch.setattr(plugin_service, "sync_host_plugins", AsyncMock())
    await plugin_service.auto_sync_host_plugins(
        SimpleNamespace(status=SimpleNamespace(value="online"), hostname="host"),
        [{"name": "images"}],
    )
    plugin_service.sync_host_plugins.assert_awaited_once()

    released_entry = SimpleNamespace(device_id=uuid.uuid4(), released_at=datetime.now(UTC))
    run = SimpleNamespace(device_reservations=[released_entry])
    assert run_reservation_service.get_reservation_entry_for_device(run, released_entry.device_id) is None


async def test_seeding_runner_and_full_demo_remaining_branches(monkeypatch: pytest.MonkeyPatch, db_session) -> None:  # noqa: ANN001
    with pytest.raises(ValueError, match="unknown scenario"):
        await seeding_runner.run_scenario(session_factory=Mock(), scenario="missing", seed=1, wipe=False)

    applied_calls: list[bool] = []

    async def applied(_ctx: object, *, skip_telemetry: bool = False) -> None:
        applied_calls.append(skip_telemetry)

    class SessionCtx:
        async def __aenter__(self) -> object:
            class Session:
                async def commit(self) -> None:
                    return None

            return Session()

        async def __aexit__(self, *_args: object) -> None:
            return None

    original_collect_row_counts = seeding_runner._collect_row_counts
    monkeypatch.setattr(
        seeding_runner.importlib, "import_module", Mock(return_value=SimpleNamespace(apply_full_demo=applied))
    )
    monkeypatch.setattr(seeding_runner, "_collect_row_counts", AsyncMock(return_value={"devices": 1}))
    result = await seeding_runner.run_scenario(
        session_factory=SessionCtx,
        scenario="full_demo",
        seed=1,
        wipe=False,
        skip_telemetry=True,
    )
    assert result.rows_written == 1
    assert applied_calls == [True]

    count_db = AsyncMock()
    count_db.scalar = AsyncMock(return_value=2)
    with monkeypatch.context() as patch_context:
        patch_context.setattr(
            seeding_runner.Base,
            "metadata",
            SimpleNamespace(tables={"alembic_version": Device.__table__, "devices": Device.__table__}),
        )
        assert await original_collect_row_counts(count_db) == {"devices": 2}

    session = SimpleNamespace(added=None, add_all=lambda rows: setattr(session, "added", rows))
    ctx = SimpleNamespace(session=session, now=datetime.now(UTC), rng=random.Random(1))
    host = SimpleNamespace(id=uuid.uuid4(), status=full_demo.HostStatus.online)
    device = SimpleNamespace(
        id=uuid.uuid4(),
        host_id=host.id,
        pack_id="appium-roku-dlenroc",
        device_config={},
        verified_at=datetime.now(UTC),
        operational_state=full_demo.DeviceOperationalState.available,
        hold=None,
        connection_target="roku",
    )
    full_demo._build_appium_nodes(ctx, [device], [host], {})  # type: ignore[arg-type]
    assert session.added == []

    from app.seeding.context import SeedContext

    telemetry = AsyncMock()
    monkeypatch.setattr(full_demo, "_build_telemetry", telemetry)
    real_ctx = SeedContext.build(session=db_session, seed=123)
    await full_demo.apply_full_demo(real_ctx, skip_telemetry=False)
    telemetry.assert_awaited_once()
