import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.device import Device, DeviceAvailabilityStatus
from app.models.device_reservation import DeviceReservation
from app.models.test_run import RunState
from app.schemas.run import ClaimRequest, ClaimResponse, ReleaseRequest, ReservedDeviceInfo
from app.services import run_service
from app.services.settings_service import settings_service
from tests.helpers import create_device_record, create_reserved_run
from tests.pack.factories import seed_test_packs


@pytest_asyncio.fixture(autouse=True)
async def seed_packs(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()


async def _make_device(db_session: AsyncSession, host_id: str, serial: str) -> Device:
    return await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=serial,
        name=f"Device {serial}",
        availability_status=DeviceAvailabilityStatus.reserved,
    )


async def _create_api_run(client: AsyncClient, name: str, count: int = 1) -> str:
    resp = await client.post(
        "/api/runs",
        json={
            "name": name,
            "requirements": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "count": count,
                }
            ],
        },
    )
    assert resp.status_code == 201
    return str(resp.json()["id"])


async def test_reservation_has_claim_columns(db_session: AsyncSession, default_host_id: str) -> None:
    device = await _make_device(db_session, default_host_id, "claim-col-001")
    run = await create_reserved_run(db_session, name="claim-col-run", devices=[device])
    reservation = run.device_reservations[0]

    assert reservation.claimed_by is None
    assert reservation.claimed_at is None


async def test_reserved_device_info_accepts_claim_fields() -> None:
    info = ReservedDeviceInfo(
        device_id=str(uuid.uuid4()),
        identity_value="serial-001",
        connection_target="serial-001",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        platform_label="Android",
        os_version="14",
        host_ip="10.0.0.1",
        claimed_by="gw0",
        claimed_at="2026-04-30T12:00:00+00:00",
    )

    assert info.claimed_by == "gw0"
    assert info.claimed_at == "2026-04-30T12:00:00+00:00"


async def test_claim_request_schema_allows_optional_worker() -> None:
    assert ClaimRequest(worker_id="gw0").worker_id == "gw0"
    assert ClaimRequest().worker_id is None

    with pytest.raises(ValueError):
        ClaimRequest(worker_id="")


async def test_claim_response_schema() -> None:
    resp = ClaimResponse(
        device_id="abc-123",
        identity_value="serial-001",
        connection_target="serial-001",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        platform_label="Android",
        os_version="14",
        host_ip="10.0.0.1",
        claimed_by="gw0",
        claimed_at="2026-04-30T12:00:00+00:00",
    )

    assert resp.device_id == "abc-123"
    assert resp.claimed_by == "gw0"


async def test_release_request_schema_requires_worker_id() -> None:
    req = ReleaseRequest(device_id="abc-123", worker_id="gw0")
    assert req.device_id == "abc-123"
    assert req.worker_id == "gw0"

    with pytest.raises(ValueError):
        ReleaseRequest(device_id="abc-123", worker_id="")


async def test_claim_ttl_setting_registered(db_session: AsyncSession) -> None:
    assert settings_service.get("reservations.claim_ttl_seconds") == 120


async def test_claim_device_success(db_session: AsyncSession, default_host_id: str) -> None:
    d1 = await _make_device(db_session, default_host_id, "claim-001")
    d2 = await _make_device(db_session, default_host_id, "claim-002")
    run = await create_reserved_run(db_session, name="claim-run", devices=[d1, d2])

    info = await run_service.claim_device(db_session, run.id, worker_id="gw0")
    assert info.device_id in {str(d1.id), str(d2.id)}
    assert info.claimed_by == "gw0"
    assert info.claimed_at is not None

    info2 = await run_service.claim_device(db_session, run.id, worker_id="gw1")
    assert info2.device_id != info.device_id
    assert info2.claimed_by == "gw1"


async def test_claim_device_no_free_devices(db_session: AsyncSession, default_host_id: str) -> None:
    d1 = await _make_device(db_session, default_host_id, "claim-full-001")
    run = await create_reserved_run(db_session, name="claim-full-run", devices=[d1])

    await run_service.claim_device(db_session, run.id, worker_id="gw0")

    with pytest.raises(ValueError, match="No unclaimed devices available"):
        await run_service.claim_device(db_session, run.id, worker_id="gw1")


async def test_claim_device_skips_excluded(db_session: AsyncSession, default_host_id: str) -> None:
    d1 = await _make_device(db_session, default_host_id, "claim-excl-001")
    d2 = await _make_device(db_session, default_host_id, "claim-excl-002")
    run = await create_reserved_run(
        db_session,
        name="claim-excl-run",
        devices=[d1, d2],
        excluded_device_ids={str(d1.id)},
        exclusion_reason="broken",
    )

    info = await run_service.claim_device(db_session, run.id, worker_id="gw0")
    assert info.device_id == str(d2.id)


async def test_claim_device_run_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="Run not found"):
        await run_service.claim_device(db_session, uuid.uuid4(), worker_id="gw0")


async def test_claim_device_terminal_run(db_session: AsyncSession, default_host_id: str) -> None:
    d1 = await _make_device(db_session, default_host_id, "claim-term-001")
    run = await create_reserved_run(
        db_session,
        name="claim-term-run",
        devices=[d1],
        state=RunState.completed,
    )

    with pytest.raises(ValueError, match="terminal state"):
        await run_service.claim_device(db_session, run.id, worker_id="gw0")


async def test_claim_device_generates_anonymous_worker_id(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    d1 = await _make_device(db_session, default_host_id, "claim-anon-001")
    run = await create_reserved_run(db_session, name="claim-anon-run", devices=[d1])

    info = await run_service.claim_device(db_session, run.id, worker_id=None)
    assert info.claimed_by is not None
    assert info.claimed_by.startswith("anonymous-")


async def test_concurrent_claims_get_distinct_devices(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    default_host_id: str,
) -> None:
    devices = [await _make_device(db_session, default_host_id, f"claim-concurrent-{idx:03d}") for idx in range(4)]
    run = await create_reserved_run(db_session, name="claim-concurrent-run", devices=devices)
    run_id = run.id

    async def claim(worker_id: str) -> str:
        async with db_session_maker() as session:
            info = await run_service.claim_device(session, run_id, worker_id=worker_id)
            return info.device_id

    claimed_ids = await asyncio.gather(*(claim(f"gw{idx}") for idx in range(4)))

    assert len(claimed_ids) == 4
    assert len(set(claimed_ids)) == 4

    result = await db_session.execute(
        select(DeviceReservation).where(DeviceReservation.run_id == run_id).execution_options(populate_existing=True)
    )
    reservations = result.scalars().all()
    assert sorted(r.claimed_by for r in reservations) == ["gw0", "gw1", "gw2", "gw3"]


async def test_claim_expires_after_configured_ttl(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    d1 = await _make_device(db_session, default_host_id, "ttl-001")
    run = await create_reserved_run(db_session, name="ttl-run", devices=[d1])

    first = await run_service.claim_device(db_session, run.id, worker_id="gw0")
    assert first.device_id == str(d1.id)

    await settings_service.update(db_session, "reservations.claim_ttl_seconds", 10)
    result = await db_session.execute(
        select(DeviceReservation).where(
            DeviceReservation.run_id == run.id,
            DeviceReservation.device_id == d1.id,
        )
    )
    reservation = result.scalar_one()
    reservation.claimed_at = datetime.now(UTC) - timedelta(seconds=11)
    await db_session.commit()

    second = await run_service.claim_device(db_session, run.id, worker_id="gw1")
    assert second.device_id == str(d1.id)
    assert second.claimed_by == "gw1"


async def test_release_claimed_device_success(db_session: AsyncSession, default_host_id: str) -> None:
    d1 = await _make_device(db_session, default_host_id, "rel-001")
    run = await create_reserved_run(db_session, name="rel-run", devices=[d1])

    info = await run_service.claim_device(db_session, run.id, worker_id="gw0")
    await run_service.release_claimed_device(
        db_session,
        run.id,
        device_id=uuid.UUID(info.device_id),
        worker_id="gw0",
    )

    reclaimed = await run_service.claim_device(db_session, run.id, worker_id="gw1")
    assert reclaimed.device_id == info.device_id
    assert reclaimed.claimed_by == "gw1"


async def test_release_claimed_device_rejects_wrong_worker(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    d1 = await _make_device(db_session, default_host_id, "rel-owner-001")
    run = await create_reserved_run(db_session, name="rel-owner-run", devices=[d1])

    await run_service.claim_device(db_session, run.id, worker_id="gw0")

    with pytest.raises(ValueError, match="claimed by another worker"):
        await run_service.release_claimed_device(
            db_session,
            run.id,
            device_id=d1.id,
            worker_id="gw1",
        )


async def test_release_claimed_device_not_claimed(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    d1 = await _make_device(db_session, default_host_id, "rel-nc-001")
    run = await create_reserved_run(db_session, name="rel-nc-run", devices=[d1])

    with pytest.raises(ValueError, match="not claimed"):
        await run_service.release_claimed_device(
            db_session,
            run.id,
            device_id=d1.id,
            worker_id="gw0",
        )


async def test_release_claimed_device_run_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="Run not found"):
        await run_service.release_claimed_device(
            db_session,
            uuid.uuid4(),
            device_id=uuid.uuid4(),
            worker_id="gw0",
        )


async def test_release_claimed_device_not_in_run(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    d1 = await _make_device(db_session, default_host_id, "rel-nir-001")
    run = await create_reserved_run(db_session, name="rel-nir-run", devices=[d1])

    with pytest.raises(ValueError, match="not reserved"):
        await run_service.release_claimed_device(
            db_session,
            run.id,
            device_id=uuid.uuid4(),
            worker_id="gw0",
        )


async def test_claim_endpoint(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    d1 = await _make_device(db_session, default_host_id, "api-claim-001")
    d2 = await _make_device(db_session, default_host_id, "api-claim-002")
    d1.availability_status = DeviceAvailabilityStatus.available
    d2.availability_status = DeviceAvailabilityStatus.available
    await db_session.commit()

    run_id = await _create_api_run(client, "API Claim Run", count=2)

    resp = await client.post(f"/api/runs/{run_id}/claim", json={"worker_id": "gw0"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["device_id"]
    assert data["pack_id"] == "appium-uiautomator2"
    assert data["claimed_by"] == "gw0"
    assert data["claimed_at"]

    resp2 = await client.post(f"/api/runs/{run_id}/claim", json={"worker_id": "gw1"})
    assert resp2.status_code == 200
    assert resp2.json()["device_id"] != data["device_id"]

    resp3 = await client.post(f"/api/runs/{run_id}/claim", json={"worker_id": "gw2"})
    assert resp3.status_code == 409


async def test_claim_endpoint_allows_missing_body(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    d1 = await _make_device(db_session, default_host_id, "api-nobody-001")
    d1.availability_status = DeviceAvailabilityStatus.available
    await db_session.commit()

    run_id = await _create_api_run(client, "No Body Run")

    resp = await client.post(f"/api/runs/{run_id}/claim")
    assert resp.status_code == 200
    assert resp.json()["claimed_by"].startswith("anonymous-")


async def test_release_endpoint(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    d1 = await _make_device(db_session, default_host_id, "api-rel-001")
    d1.availability_status = DeviceAvailabilityStatus.available
    await db_session.commit()

    run_id = await _create_api_run(client, "API Release Run")
    claim_resp = await client.post(f"/api/runs/{run_id}/claim", json={"worker_id": "gw0"})
    device_id = claim_resp.json()["device_id"]

    release_resp = await client.post(
        f"/api/runs/{run_id}/release",
        json={"device_id": device_id, "worker_id": "gw0"},
    )
    assert release_resp.status_code == 200
    assert release_resp.json() == {"status": "released"}

    reclaim_resp = await client.post(f"/api/runs/{run_id}/claim", json={"worker_id": "gw1"})
    assert reclaim_resp.status_code == 200
    assert reclaim_resp.json()["device_id"] == device_id


async def test_release_endpoint_rejects_wrong_worker(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    d1 = await _make_device(db_session, default_host_id, "api-rel-owner-001")
    d1.availability_status = DeviceAvailabilityStatus.available
    await db_session.commit()

    run_id = await _create_api_run(client, "API Release Owner Run")
    claim_resp = await client.post(f"/api/runs/{run_id}/claim", json={"worker_id": "gw0"})
    device_id = claim_resp.json()["device_id"]

    release_resp = await client.post(
        f"/api/runs/{run_id}/release",
        json={"device_id": device_id, "worker_id": "gw1"},
    )
    assert release_resp.status_code == 409


async def test_release_endpoint_invalid_device_id(client: AsyncClient) -> None:
    run_id = str(uuid.uuid4())
    resp = await client.post(
        f"/api/runs/{run_id}/release",
        json={"device_id": "not-a-uuid", "worker_id": "gw0"},
    )

    assert resp.status_code == 422


async def test_claim_endpoint_not_found(client: AsyncClient) -> None:
    fake_id = str(uuid.uuid4())
    resp = await client.post(f"/api/runs/{fake_id}/claim", json={"worker_id": "gw0"})
    assert resp.status_code == 404


async def test_get_run_shows_claim_status(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    d1 = await _make_device(db_session, default_host_id, "vis-001")
    d2 = await _make_device(db_session, default_host_id, "vis-002")
    d1.availability_status = DeviceAvailabilityStatus.available
    d2.availability_status = DeviceAvailabilityStatus.available
    await db_session.commit()

    run_id = await _create_api_run(client, "Visibility Run", count=2)
    claim_resp = await client.post(f"/api/runs/{run_id}/claim", json={"worker_id": "gw0"})
    claimed_device_id = claim_resp.json()["device_id"]

    run_resp = await client.get(f"/api/runs/{run_id}")
    assert run_resp.status_code == 200
    devices = run_resp.json()["devices"]
    claimed = [d for d in devices if d["device_id"] == claimed_device_id]
    unclaimed = [d for d in devices if d["device_id"] != claimed_device_id]

    assert claimed[0]["claimed_by"] == "gw0"
    assert claimed[0]["claimed_at"] is not None
    assert unclaimed[0]["claimed_by"] is None
    assert unclaimed[0]["claimed_at"] is None


async def test_claims_cleared_on_run_complete(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    d1 = await _make_device(db_session, default_host_id, "clear-001")
    d1.availability_status = DeviceAvailabilityStatus.available
    await db_session.commit()

    run_id = await _create_api_run(client, "Clear Claims Run")
    await client.post(f"/api/runs/{run_id}/claim", json={"worker_id": "gw0"})
    await client.post(f"/api/runs/{run_id}/ready")
    await client.post(f"/api/runs/{run_id}/active")
    await client.post(f"/api/runs/{run_id}/complete")

    run_resp = await client.get(f"/api/runs/{run_id}")
    assert run_resp.status_code == 200
    for device in run_resp.json()["devices"]:
        assert device["claimed_by"] is None
        assert device["claimed_at"] is None


async def test_create_reserved_run_can_seed_claimed_devices(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    d1 = await _make_device(db_session, default_host_id, "helper-claimed-001")
    run = await create_reserved_run(
        db_session,
        name="helper-claimed-run",
        devices=[d1],
        claimed_device_ids={str(d1.id): "gw0"},
    )

    reservation = run.device_reservations[0]
    assert reservation.claimed_by == "gw0"
    assert reservation.claimed_at is not None
