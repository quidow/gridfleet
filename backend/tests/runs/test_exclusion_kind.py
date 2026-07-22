"""WS-12.2: exclusion_kind drives the cooldown-vs-exclusion distinction."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceReservation, ExclusionKind
from app.devices.services.intent_reconciler import ReconcileCandidate, gather_decision_facts, reconcile_device_command
from app.runs.service_reservation import RunReservationService
from tests.fakes import build_review_service
from tests.helpers import create_device_record
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
def _stub_agent_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    # poke_node_refresh would attempt a real TCP connect otherwise (same stub
    # as tests/runs/test_run_device_cooldown.py).
    monkeypatch.setattr(
        "app.agent_comm.node_poke.agent_operations.agent_nodes_refresh",
        AsyncMock(),
    )


@pytest.fixture(autouse=True)
async def _seed_packs(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()


async def _create_available_device(db_session: AsyncSession, host_id: str, identity_value: str) -> Device:
    return await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=identity_value,
        connection_target=identity_value,
        name=f"Device {identity_value}",
        operational_state="available",
    )


async def _create_run(client: AsyncClient, **overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Exclusion Kind Run",
        "requirements": [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        **overrides,
    }
    resp = await client.post("/api/runs", json=payload)
    assert resp.status_code == 201
    return dict(resp.json())


async def _reservation_entry(db_session: AsyncSession, run_id: str, device_id: uuid.UUID) -> DeviceReservation:
    return (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.run_id == uuid.UUID(run_id),
                DeviceReservation.device_id == device_id,
            )
        )
    ).scalar_one()


async def test_indefinite_exclusion_drops_run_routing(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_available_device(db_session, default_host_id, "exkind-001")
    run = await _create_run(client)

    svc = RunReservationService(review=build_review_service())
    await svc.exclude_device_from_run(db_session, device.id, reason="health failure")

    entry = await _reservation_entry(db_session, run["id"], device.id)
    assert entry.exclusion_kind == ExclusionKind.exclusion

    facts = await gather_decision_facts(db_session, device, now_utc())
    assert facts.reservation_run_id is None
    assert facts.cooldown_active is False


async def test_cooldown_keeps_run_binding_and_blocks_sessions(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_available_device(db_session, default_host_id, "exkind-002")
    run = await _create_run(client)

    resp = await client.post(
        f"/api/runs/{run['id']}/devices/{device.id}/cooldown",
        json={"reason": "flaky", "ttl_seconds": 120},
    )
    assert resp.status_code == 200

    facts = await gather_decision_facts(db_session, device, now_utc())
    assert facts.reservation_run_id == uuid.UUID(run["id"])
    assert facts.cooldown_active is True
    assert facts.cooldown_reason == "flaky"


async def test_expired_unswept_cooldown_is_inactive(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    """kind stays 'cooldown' until the TTL clear runs; an elapsed window must read inactive."""
    device = await _create_available_device(db_session, default_host_id, "exkind-003")
    run = await _create_run(client)
    resp = await client.post(
        f"/api/runs/{run['id']}/devices/{device.id}/cooldown",
        json={"reason": "flaky", "ttl_seconds": 120},
    )
    assert resp.status_code == 200

    entry = await _reservation_entry(db_session, run["id"], device.id)
    expired_until = datetime.now(UTC) - timedelta(seconds=1)
    entry.excluded_at = expired_until - timedelta(seconds=120)
    entry.excluded_until = expired_until
    await db_session.commit()

    facts = await gather_decision_facts(db_session, device, now_utc())
    assert facts.reservation_run_id == uuid.UUID(run["id"])
    assert facts.cooldown_active is False


async def test_kind_is_authoritative_over_window(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    """A row with a future window but no kind (illegal post-migration shape) is not a cooldown."""
    device = await _create_available_device(db_session, default_host_id, "exkind-004")
    run = await _create_run(client)

    entry = await _reservation_entry(db_session, run["id"], device.id)
    entry.excluded = True
    entry.excluded_at = datetime.now(UTC)
    entry.excluded_until = datetime.now(UTC) + timedelta(seconds=120)
    entry.exclusion_kind = None
    await db_session.commit()

    facts = await gather_decision_facts(db_session, device, now_utc())
    assert facts.cooldown_active is False


async def test_expiry_clears_kind_and_skips_indefinite_exclusions(
    client: AsyncClient,
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    default_host_id: str,
) -> None:
    device = await _create_available_device(db_session, default_host_id, "exkind-005")
    run = await _create_run(client)
    resp = await client.post(
        f"/api/runs/{run['id']}/devices/{device.id}/cooldown",
        json={"reason": "flaky", "ttl_seconds": 120},
    )
    assert resp.status_code == 200
    entry = await _reservation_entry(db_session, run["id"], device.id)
    expired_until = datetime.now(UTC) - timedelta(seconds=1)
    entry.excluded_at = expired_until - timedelta(seconds=120)
    entry.excluded_until = expired_until
    await db_session.commit()

    excluded_device = await _create_available_device(db_session, default_host_id, "exkind-006")
    run2 = await _create_run(client)
    svc = RunReservationService(review=build_review_service())
    await svc.exclude_device_from_run(db_session, excluded_device.id, reason="health failure")

    await reconcile_device_command(
        db_session_maker,
        ReconcileCandidate(device.id, delete_expired_intents=False, clear_elapsed_cooldown=True),
        publisher=event_bus,
        packs={},
    )

    await db_session.refresh(entry)
    assert entry.excluded is False
    assert entry.exclusion_kind is None

    excluded_entry = await _reservation_entry(db_session, run2["id"], excluded_device.id)
    assert excluded_entry.excluded is True
    assert excluded_entry.exclusion_kind == ExclusionKind.exclusion
