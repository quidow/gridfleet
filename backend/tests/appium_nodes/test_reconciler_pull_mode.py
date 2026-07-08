"""Pull-mode (agent-pull node desired state) tests for the Appium reconciler.

See ``.superpowers/sdd/task-2-brief.md``: a host whose agent advertises
``node_desired_pull`` (``node_pull=True``) puts the reconciler in observe-only
mode for that host — no agent start/stop/restart or orphan reaps are issued,
and applied-transition-token facts reported by the agent are ingested
instead. ``node_pull=False`` (the default) must stay byte-identical to the
existing push path.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from app.appium_nodes.exceptions import NodeAlreadyRunningError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import reconciler as appium_reconciler
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services.reconciler_convergence import DesiredRow, ObservedEntry
from app.core.metrics_recorders import (
    APPIUM_PULL_MODE_ORPHANS_OBSERVED,
    APPIUM_PULL_MODE_SKIPPED_ACTIONS,
    APPIUM_TRANSITION_TOKEN_OVERRIDDEN,
)
from app.devices.models import DeviceOperationalState
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
        "transition_token": None,
        "transition_deadline": None,
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


def _override_total() -> float:
    return sum(
        sample.value
        for metric in APPIUM_TRANSITION_TOKEN_OVERRIDDEN.collect()
        for sample in metric.samples
        if sample.name.endswith("_total")
    )


class _DummySession:
    async def __aenter__(self) -> _DummySession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


async def test_pull_host_running_desired_node_absent_skips_start_and_counts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """desired=running, agent reports nothing → push mode would start the agent;
    a pull host must not, and must record the skip."""
    row = _desired_row(desired_state="running", desired_port=4723, connection_target="pull-start")
    start_spy = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "_start_for_node", start_spy)

    svc = _make_service()
    before = APPIUM_PULL_MODE_SKIPPED_ACTIONS.labels(kind="start")._value.get()

    await svc.converge_host_rows(
        None, [row], [], host_id=uuid.uuid4(), host_ip="10.0.0.1", agent_port=5100, node_pull=True
    )

    after = APPIUM_PULL_MODE_SKIPPED_ACTIONS.labels(kind="start")._value.get()
    start_spy.assert_not_awaited()
    assert after == before + 1


async def test_pull_host_observed_running_writes_same_as_push_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB-only action (db_mark_running) is untouched by pull mode: the observed
    columns are written exactly as ``_execute_action`` writes them in push mode."""
    device_id = uuid.uuid4()
    row = _desired_row(device_id=device_id, desired_state="running", desired_port=4723, connection_target="pull-mark")
    observed = [ObservedEntry(port=4723, pid=999, connection_target="pull-mark")]

    node = SimpleNamespace(desired_state="running", desired_port=4723, transition_token=None, transition_deadline=None)
    device = SimpleNamespace(id=device_id, appium_node=node)
    monkeypatch.setattr(appium_reconciler, "_load_device_for_reconciler", AsyncMock(return_value=device))
    mark_started = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "mark_node_started", mark_started)

    svc = _make_service()
    await svc.converge_host_rows(
        _DummySession(), [row], observed, host_id=uuid.uuid4(), host_ip="10.0.0.1", agent_port=5100, node_pull=True
    )

    mark_started.assert_awaited_once()
    kwargs = mark_started.await_args.kwargs
    assert kwargs["port"] == 4723
    assert kwargs["pid"] == 999
    assert kwargs["details"].active_connection_target == "pull-mark"
    assert kwargs["details"].clear_transition is False


@pytest.mark.db
async def test_pull_host_applied_transition_token_match_clears_via_natural_clear(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="pull-token-clear",
        identity_value="pull-token-clear-001",
        connection_target="pull-token-clear-target",
        operational_state=DeviceOperationalState.available,
    )
    token = uuid.uuid4()
    deadline = datetime.now(UTC) + timedelta(seconds=60)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=111,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        transition_token=token,
        transition_deadline=deadline,
        active_connection_target=device.connection_target,
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
        transition_token=token,
        transition_deadline=deadline,
        port=4723,
        pid=111,
        active_connection_target=device.connection_target,
    )
    raw_running_nodes = [
        {
            "port": 4723,
            "pid": 111,
            "connection_target": device.connection_target,
            "platform_id": device.platform_id,
            "applied_transition_token": str(token),
        }
    ]

    svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=_scope_for(db_session),
    )

    before = _override_total()
    await svc._ingest_pull_host_reports([row], raw_running_nodes)
    after = _override_total()

    await db_session.refresh(node)
    assert node.transition_token is None
    assert node.transition_deadline is None
    assert after == before, "natural clear must not trip APPIUM_TRANSITION_TOKEN_OVERRIDDEN"


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
    node = SimpleNamespace(desired_state="stopped", desired_port=None, transition_token=None, transition_deadline=None)
    device = SimpleNamespace(id=device_id, appium_node=node)
    monkeypatch.setattr(appium_reconciler, "_load_device_for_reconciler", AsyncMock(return_value=device))
    mark_stopped = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "mark_node_stopped", mark_stopped)

    svc = _make_service()
    await svc.converge_host_rows(
        _DummySession(), [row], [], host_id=uuid.uuid4(), host_ip="10.0.0.1", agent_port=5100, node_pull=True
    )

    mark_stopped.assert_awaited_once()


async def test_pull_host_orphan_process_is_counted_not_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray agent-reported node with no matching desired row is never stopped
    on a pull host — only counted."""
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
    stop_spy = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "stop_remote_node", stop_spy)

    svc = _make_service()
    before = APPIUM_PULL_MODE_ORPHANS_OBSERVED._value.get()

    await svc.reconcile_host(
        host_id=host_id,
        host_ip="10.0.0.9",
        agent_port=5100,
        rows=[row],
        backoff_until_by_device={},
        payload=payload,
        node_pull=True,
    )

    after = APPIUM_PULL_MODE_ORPHANS_OBSERVED._value.get()
    stop_spy.assert_not_awaited()
    assert after == before + 1


@pytest.mark.parametrize("node_pull_kwargs", [{}, {"node_pull": False}], ids=["default", "explicit-false"])
async def test_node_pull_false_runs_agent_start_unchanged(
    monkeypatch: pytest.MonkeyPatch, node_pull_kwargs: dict[str, bool]
) -> None:
    """``node_pull`` defaults to False and behaves identically to an explicit
    False — the legacy push path stays byte-identical (plan Global Constraints)."""
    row = _desired_row(desired_state="running", desired_port=4723, connection_target="push-start")
    fake_device = SimpleNamespace(appium_node=object())
    monkeypatch.setattr(appium_reconciler, "_load_device_for_reconciler", AsyncMock(return_value=fake_device))
    start_spy = AsyncMock(side_effect=NodeAlreadyRunningError("already running for target"))
    monkeypatch.setattr(appium_reconciler, "_start_for_node", start_spy)

    svc = _make_service()
    before = APPIUM_PULL_MODE_SKIPPED_ACTIONS.labels(kind="start")._value.get()

    await svc.converge_host_rows(
        _DummySession(), [row], [], host_id=uuid.uuid4(), host_ip="10.0.0.1", agent_port=5100, **node_pull_kwargs
    )

    after = APPIUM_PULL_MODE_SKIPPED_ACTIONS.labels(kind="start")._value.get()
    start_spy.assert_awaited_once()
    assert after == before


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
        settings=FakeSettingsReader({"appium_reconciler.start_failure_threshold": 5}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=_scope_for(db_session),
    )

    await svc._ingest_pull_host_reports([row], [], [failure])

    await db_session.refresh(node)
    await db_session.refresh(device)
    assert node.desired_port is not None
    assert node.desired_port != 4723
    first_new_port = node.desired_port
    assert device.lifecycle_policy_state["recovery_backoff_attempts"] == 1

    # Second sweep, same (port, at) report still in the agent's ring: dedupe
    # must skip both the re-pin and the backoff increment.
    await svc._ingest_pull_host_reports([row], [], [failure])

    await db_session.refresh(node)
    await db_session.refresh(device)
    assert node.desired_port == first_new_port
    assert device.lifecycle_policy_state["recovery_backoff_attempts"] == 1


@pytest.mark.db
async def test_pull_host_start_failure_threshold_sets_backoff_until(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Threshold behavior reuses ``_record_start_failure`` verbatim: backoff_until
    stays unset below threshold and is set once attempts reach it, matching the
    push path's window (appium.startup_timeout_sec * 4)."""
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
        settings=FakeSettingsReader({"appium_reconciler.start_failure_threshold": 2, "appium.startup_timeout_sec": 5}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=_scope_for(db_session),
    )

    t0 = datetime.now(UTC).isoformat()
    await svc._ingest_pull_host_reports(
        [row], [], [_start_failure(kind="spawn_failed", connection_target=device.connection_target, at=t0)]
    )
    await db_session.refresh(device)
    assert device.lifecycle_policy_state["recovery_backoff_attempts"] == 1
    assert device.lifecycle_policy_state["backoff_until"] is None

    t1 = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    before = datetime.now(UTC)
    await svc._ingest_pull_host_reports(
        [row], [], [_start_failure(kind="spawn_failed", connection_target=device.connection_target, at=t1)]
    )
    after = datetime.now(UTC)
    await db_session.refresh(device)
    assert device.lifecycle_policy_state["recovery_backoff_attempts"] == 2
    # backoff window = appium.startup_timeout_sec * 4 (see _record_start_failure),
    # matching the push path's computation exactly.
    backoff_until = datetime.fromisoformat(device.lifecycle_policy_state["backoff_until"])
    assert before + timedelta(seconds=20) <= backoff_until <= after + timedelta(seconds=20)


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
        settings=FakeSettingsReader({"appium_reconciler.start_failure_threshold": 5}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=_scope_for(db_session),
    )

    await svc._ingest_pull_host_reports([row], [], [failure])

    await db_session.refresh(node)
    await db_session.refresh(device)
    assert node.desired_port == 4723
    assert device.lifecycle_policy_state["recovery_backoff_attempts"] == 1


@pytest.mark.db
async def test_pull_host_port_conflict_repin_preserves_transition_token(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """The re-pin write must preserve the node's existing transition token/deadline
    so it stays off APPIUM_TRANSITION_TOKEN_OVERRIDDEN (desired_state_writer.py:116)."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="pull-repin-token",
        identity_value="pull-repin-token-001",
        connection_target="pull-repin-token-target",
        operational_state=DeviceOperationalState.available,
    )
    token = uuid.uuid4()
    deadline = datetime.now(UTC) + timedelta(seconds=60)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=None,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        transition_token=token,
        transition_deadline=deadline,
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
        transition_token=token,
        transition_deadline=deadline,
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

    before = _override_total()
    await svc._ingest_pull_host_reports([row], [], [failure])
    after = _override_total()

    await db_session.refresh(node)
    assert node.transition_token == token
    assert node.transition_deadline is not None
    assert node.desired_port != 4723
    assert after == before, "re-pin must preserve the existing token and not trip APPIUM_TRANSITION_TOKEN_OVERRIDDEN"
