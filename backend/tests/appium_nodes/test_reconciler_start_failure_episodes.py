"""D5: a report from a superseded episode is not evidence about the current one."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services.reconciler_convergence import DesiredRow, ObservedEntry
from app.devices.models import DeviceOperationalState
from app.lifecycle.services import remediation_log
from app.lifecycle.services.incidents import LifecycleIncidentService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host


def _row(device: object, host_id: uuid.UUID, node: AppiumNode) -> DesiredRow:
    return DesiredRow(
        device_id=device.id,
        host_id=host_id,
        node_id=node.id,
        connection_target=device.connection_target,
        desired_state="running",
        desired_port=4723,
        port=4723,
        pid=4242,
        active_connection_target=device.connection_target,
        stop_pending=False,
    )


@pytest.mark.db
async def test_a_report_from_a_superseded_episode_never_escalates(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="superseded",
        identity_value="superseded-001",
        connection_target="superseded-target",
        operational_state=DeviceOperationalState.available,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=4242,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    started_at = datetime.now(UTC)
    stale_at = (started_at - timedelta(seconds=5)).isoformat()
    observed = [ObservedEntry(port=4723, pid=4242, connection_target=device.connection_target, started_at=started_at)]
    svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=db_session_maker,
        incidents=LifecycleIncidentService(),
    )

    await svc._ingest_start_failure_reports(
        [_row(device, db_host.id, node)],
        [
            {
                "port": 4723,
                "connection_target": device.connection_target,
                "kind": "port_conflict",
                "detail": "port in use",
                "at": stale_at,
            }
        ],
        observed=observed,
    )

    assert (await remediation_log.load_ladder(db_session, device.id)).attempts == 0
    assert svc._last_seen_failure_at[device.id] == stale_at, "the stale report was not folded"


@pytest.mark.db
async def test_a_foreign_node_sharing_the_port_does_not_supersede_the_report(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Supersession matches by connection target only.

    The observed node here belongs to another device that happens to hold the
    port this row is pinned to — exactly the cross-device collision a
    ``port_conflict`` reports. Resolving the observation by port would let that
    node vouch for this one, advancing the dedupe cursor past a real conflict
    and skipping both the escalation and the ``desired_port`` re-pin.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="port-collision",
        identity_value="port-collision-001",
        connection_target="port-collision-target",
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

    started_at = datetime.now(UTC)
    stale_at = (started_at - timedelta(seconds=5)).isoformat()
    # Same port, different device's target — and the row has no live target, so
    # neither target lookup can hit.
    observed = [ObservedEntry(port=4723, pid=9999, connection_target="some-other-device-target", started_at=started_at)]
    row = DesiredRow(
        device_id=device.id,
        host_id=db_host.id,
        node_id=node.id,
        connection_target=device.connection_target,
        desired_state="running",
        desired_port=4723,
        port=4723,
        pid=None,
        active_connection_target=None,
        stop_pending=False,
    )
    svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({"appium.port_range_start": 4723, "appium.port_range_end": 4823}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=db_session_maker,
        incidents=LifecycleIncidentService(),
    )

    await svc._ingest_start_failure_reports(
        [row],
        [
            {
                "port": 4723,
                "connection_target": device.connection_target,
                "kind": "port_conflict",
                "detail": "port in use",
                "at": stale_at,
            }
        ],
        observed=observed,
    )

    assert (await remediation_log.load_ladder(db_session, device.id)).attempts == 1, "a real conflict was swallowed"
    db_session.expire(node)
    reloaded = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    assert reloaded.desired_port != 4723, "the re-pin was skipped"


async def test_reconcile_host_folds_reports_before_convergence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordering guard: the success reset rides convergence, so ingestion of the
    previous episode's reports must already be done when it lands. No DB — the
    claim is about call order, and a full-stack rehearsal would prove it by
    accident at best."""
    from unittest.mock import AsyncMock

    from app.appium_nodes.services import reconciler as appium_reconciler

    calls: list[str] = []
    svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=Mock(),
        incidents=LifecycleIncidentService(),
    )
    monkeypatch.setattr(appium_reconciler, "_touch_last_observed", AsyncMock())

    async def fake_ingest(*_args: object, **_kwargs: object) -> None:
        calls.append("ingest")

    async def fake_converge(*_args: object, **_kwargs: object) -> None:
        calls.append("converge")

    monkeypatch.setattr(svc, "_ingest_start_failure_reports", fake_ingest)
    monkeypatch.setattr(svc, "converge_host_rows", fake_converge)

    device_id, host_id, node_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    row = DesiredRow(
        device_id=device_id,
        host_id=host_id,
        node_id=node_id,
        connection_target="order-target",
        desired_state="running",
        desired_port=4723,
        port=4723,
        pid=None,
        active_connection_target=None,
        stop_pending=False,
    )
    await svc.reconcile_host(
        host_id=host_id,
        host_ip="10.0.0.1",
        agent_port=5100,
        rows=[row],
        backoff_until_by_device={},
        payload={"appium_processes": {"running_nodes": [], "start_failures": []}},
    )

    assert calls == ["ingest", "converge"]


async def test_reconcile_host_folds_reports_even_when_every_row_is_in_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4a at the entry point: the ``if not active_rows: return`` early exit must
    not sit between the host payload and the ingest."""
    from unittest.mock import AsyncMock

    from app.appium_nodes.services import reconciler as appium_reconciler

    calls: list[str] = []
    svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=Mock(),
        incidents=LifecycleIncidentService(),
    )
    monkeypatch.setattr(appium_reconciler, "_touch_last_observed", AsyncMock())

    async def fake_ingest(*_args: object, **_kwargs: object) -> None:
        calls.append("ingest")

    monkeypatch.setattr(svc, "_ingest_start_failure_reports", fake_ingest)

    device_id, host_id, node_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    row = DesiredRow(
        device_id=device_id,
        host_id=host_id,
        node_id=node_id,
        connection_target="backoff-target",
        desired_state="running",
        desired_port=4723,
        port=4723,
        pid=None,
        active_connection_target=None,
        stop_pending=False,
    )
    await svc.reconcile_host(
        host_id=host_id,
        host_ip="10.0.0.1",
        agent_port=5100,
        rows=[row],
        backoff_until_by_device={device_id: datetime.now(UTC) + timedelta(seconds=60)},
        payload={"appium_processes": {"running_nodes": [], "start_failures": []}},
    )

    assert calls == ["ingest"]
