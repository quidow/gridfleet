"""Pull-mode (agent-pull node desired state) tests for the Appium reconciler.

Pull is now the only reconcile mode: the reconciler is always observe-only —
no agent start/stop or orphan reaps are issued.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import reconciler as appium_reconciler
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services.reconciler_convergence import DesiredRow, ObservedEntry
from app.core.metrics_recorders import (
    APPIUM_PULL_MODE_ORPHANS_OBSERVED,
    APPIUM_PULL_MODE_SKIPPED_ACTIONS,
)
from app.devices.models import DeviceOperationalState
from app.lifecycle.services import remediation_log
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def _start_failure(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "port": 4723,
        "connection_target": "pull-conflict",
        "kind": "port_conflict",
        "detail": "port in use",
        "at": datetime.now(UTC).isoformat(),
    }
    values.update(overrides)
    return values


def _desired_row(**overrides: object) -> DesiredRow:
    values: dict[str, object] = {
        "device_id": uuid.uuid4(),
        "host_id": uuid.uuid4(),
        "node_id": uuid.uuid4(),
        "connection_target": "serial-1",
        "desired_state": "running",
        "desired_port": 4723,
        "port": None,
        "pid": None,
        "active_connection_target": None,
        "stop_pending": False,
    }
    values.update(overrides)
    return DesiredRow(**values)  # type: ignore[arg-type]


def _make_service(*, session_factory: object = None) -> ReconcilerService:
    return ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=session_factory or Mock(),
    )


def _scope_for(db: AsyncSession) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    @asynccontextmanager
    async def _scope() -> AsyncIterator[AsyncSession]:
        yield db

    return _scope


class _DummySession:
    async def __aenter__(self) -> _DummySession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


async def test_pull_host_running_desired_node_absent_skips_start_and_counts_it() -> None:
    """desired=running, agent reports nothing → the reconciler never starts a
    node itself; it records the skip and leaves the start to the agent."""
    row = _desired_row(desired_state="running", desired_port=4723, connection_target="pull-start")

    svc = _make_service()
    before = APPIUM_PULL_MODE_SKIPPED_ACTIONS.labels(kind="start")._value.get()

    await svc.converge_host_rows(None, [row], [], host_id=uuid.uuid4(), host_ip="10.0.0.1", agent_port=5100)

    after = APPIUM_PULL_MODE_SKIPPED_ACTIONS.labels(kind="start")._value.get()
    assert after == before + 1


async def test_pull_host_observed_running_writes_same_as_push_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB-only action (db_mark_running) is untouched by pull mode: the observed
    columns are written exactly as ``_execute_action`` writes them in push mode."""
    device_id = uuid.uuid4()
    row = _desired_row(device_id=device_id, desired_state="running", desired_port=4723, connection_target="pull-mark")
    observed = [ObservedEntry(port=4723, pid=999, connection_target="pull-mark")]

    node = SimpleNamespace(desired_state="running", desired_port=4723, restart_requested_at=None)
    device = SimpleNamespace(id=device_id, appium_node=node)
    monkeypatch.setattr(appium_reconciler, "_load_device_for_reconciler", AsyncMock(return_value=device))
    mark_started = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "mark_node_started", mark_started)

    svc = _make_service()
    await svc.converge_host_rows(
        _DummySession(), [row], observed, host_id=uuid.uuid4(), host_ip="10.0.0.1", agent_port=5100
    )

    mark_started.assert_awaited_once()
    kwargs = mark_started.await_args.kwargs
    assert kwargs["port"] == 4723
    assert kwargs["pid"] == 999
    assert kwargs["details"].active_connection_target == "pull-mark"


async def test_pull_host_stopped_desired_absent_from_payload_marks_observed_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """desired=stopped, stale pid in DB, agent reports nothing this pass →
    db_clear_stale_running fires and marks the node stopped, unaffected by pull mode."""
    device_id = uuid.uuid4()
    row = _desired_row(
        device_id=device_id,
        desired_state="stopped",
        desired_port=None,
        connection_target="pull-stop",
        port=4723,
        pid=222,
        active_connection_target="pull-stop",
    )
    node = SimpleNamespace(desired_state="stopped", desired_port=None, restart_requested_at=None)
    device = SimpleNamespace(id=device_id, appium_node=node)
    monkeypatch.setattr(appium_reconciler, "_load_device_for_reconciler", AsyncMock(return_value=device))
    mark_stopped = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "mark_node_stopped", mark_stopped)

    svc = _make_service()
    await svc.converge_host_rows(_DummySession(), [row], [], host_id=uuid.uuid4(), host_ip="10.0.0.1", agent_port=5100)

    mark_stopped.assert_awaited_once()


async def test_pull_host_orphan_process_is_counted_not_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray agent-reported node with no matching desired row is never stopped
    by the reconciler — only counted; the agent owns its own orphan cleanup."""
    host_id = uuid.uuid4()
    row = _desired_row(
        host_id=host_id,
        connection_target="known-target",
        desired_state="stopped",
        desired_port=None,
        port=None,
        pid=None,
        active_connection_target=None,
    )
    payload = {
        "appium_processes": {
            "running_nodes": [
                {"port": 9999, "pid": 1, "connection_target": "orphan-target", "platform_id": "android_mobile"}
            ]
        }
    }
    monkeypatch.setattr(appium_reconciler, "_touch_last_observed", AsyncMock())

    svc = _make_service()
    before = APPIUM_PULL_MODE_ORPHANS_OBSERVED._value.get()

    await svc.reconcile_host(
        host_id=host_id,
        host_ip="10.0.0.9",
        agent_port=5100,
        rows=[row],
        backoff_until_by_device={},
        payload=payload,
    )

    after = APPIUM_PULL_MODE_ORPHANS_OBSERVED._value.get()
    assert after == before + 1


@pytest.mark.db
async def test_pull_host_port_conflict_repins_port_and_records_backoff_once(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Task 4 / D3: a port_conflict start_failures report re-pins desired_port to
    the next free candidate and trips backoff once. A second sweep reporting the
    SAME (port, at) failure must not re-pin or re-increment (level-style dedupe)."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="pull-port-conflict",
        identity_value="pull-port-conflict-001",
        connection_target="pull-port-conflict-target",
        operational_state=DeviceOperationalState.available,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=None,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    row = _desired_row(
        device_id=device.id,
        host_id=db_host.id,
        node_id=node.id,
        connection_target=device.connection_target,
        desired_state="running",
        desired_port=4723,
        port=None,
        pid=None,
        active_connection_target=None,
    )
    failure = _start_failure(port=4723, connection_target=device.connection_target, kind="port_conflict")

    svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=_scope_for(db_session),
    )

    await svc._ingest_start_failure_reports([row], [failure])

    await db_session.refresh(node)
    await db_session.refresh(device)
    assert node.desired_port is not None
    assert node.desired_port != 4723
    first_new_port = node.desired_port
    first_ladder = await remediation_log.load_ladder(db_session, device.id)
    assert first_ladder.attempts == 1

    # Second sweep, same (port, at) report still in the agent's ring: dedupe
    # must skip both the re-pin and the backoff increment.
    await svc._ingest_start_failure_reports([row], [failure])

    await db_session.refresh(node)
    await db_session.refresh(device)
    assert node.desired_port == first_new_port
    assert (await remediation_log.load_ladder(db_session, device.id)).attempts == 1


@pytest.mark.db
async def test_pull_host_start_failure_uses_shared_exponential_backoff(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Start failures back off from the first failure through the shared ladder."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="pull-threshold",
        identity_value="pull-threshold-001",
        connection_target="pull-threshold-target",
        operational_state=DeviceOperationalState.available,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=None,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    row = _desired_row(
        device_id=device.id,
        host_id=db_host.id,
        node_id=node.id,
        connection_target=device.connection_target,
        desired_state="running",
        desired_port=4723,
        port=None,
        pid=None,
        active_connection_target=None,
    )

    svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader(
            {
                "general.lifecycle_recovery_backoff_base_sec": 5,
                "general.lifecycle_recovery_backoff_max_sec": 60,
                "general.lifecycle_recovery_review_threshold": 5,
            }
        ),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=_scope_for(db_session),
    )

    t0 = datetime.now(UTC).isoformat()
    before_first = datetime.now(UTC)
    await svc._ingest_start_failure_reports(
        [row], [_start_failure(kind="spawn_failed", connection_target=device.connection_target, at=t0)]
    )
    after_first = datetime.now(UTC)
    await db_session.refresh(device)
    first_ladder = await remediation_log.load_ladder(db_session, device.id)
    assert first_ladder.attempts == 1
    assert first_ladder.backoff_until is not None
    first_backoff_until = first_ladder.backoff_until
    assert before_first + timedelta(seconds=5) <= first_backoff_until <= after_first + timedelta(seconds=5)

    t1 = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    before_second = datetime.now(UTC)
    await svc._ingest_start_failure_reports(
        [row], [_start_failure(kind="spawn_failed", connection_target=device.connection_target, at=t1)]
    )
    after_second = datetime.now(UTC)
    await db_session.refresh(device)
    second_ladder = await remediation_log.load_ladder(db_session, device.id)
    assert second_ladder.attempts == 2
    assert second_ladder.backoff_until is not None
    second_backoff_until = second_ladder.backoff_until
    assert before_second + timedelta(seconds=10) <= second_backoff_until <= after_second + timedelta(seconds=10)


@pytest.mark.db
async def test_pull_host_spawn_failed_records_backoff_without_repin(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """spawn_failed trips the same backoff bookkeeping but never touches desired_port."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="pull-spawn-failed",
        identity_value="pull-spawn-failed-001",
        connection_target="pull-spawn-failed-target",
        operational_state=DeviceOperationalState.available,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=None,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    row = _desired_row(
        device_id=device.id,
        host_id=db_host.id,
        node_id=node.id,
        connection_target=device.connection_target,
        desired_state="running",
        desired_port=4723,
        port=None,
        pid=None,
        active_connection_target=None,
    )
    failure = _start_failure(kind="spawn_failed", connection_target=device.connection_target, port=None)

    svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=_scope_for(db_session),
    )

    await svc._ingest_start_failure_reports([row], [failure])

    await db_session.refresh(node)
    await db_session.refresh(device)
    assert node.desired_port == 4723
    assert (await remediation_log.load_ladder(db_session, device.id)).attempts == 1


@pytest.mark.db
async def test_pull_host_port_conflict_repin_preserves_restart_watermark(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """The re-pin write must preserve the node's existing restart watermark."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="pull-repin-token",
        identity_value="pull-repin-token-001",
        connection_target="pull-repin-token-target",
        operational_state=DeviceOperationalState.available,
    )
    restart_requested_at = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=None,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        restart_requested_at=restart_requested_at,
    )
    db_session.add(node)
    await db_session.commit()

    row = _desired_row(
        device_id=device.id,
        host_id=db_host.id,
        node_id=node.id,
        connection_target=device.connection_target,
        desired_state="running",
        desired_port=4723,
        port=None,
        pid=None,
        active_connection_target=None,
    )
    failure = _start_failure(port=4723, connection_target=device.connection_target, kind="port_conflict")

    svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=_scope_for(db_session),
    )

    await svc._ingest_start_failure_reports([row], [failure])

    await db_session.refresh(node)
    assert node.restart_requested_at == restart_requested_at
    assert node.desired_port != 4723
