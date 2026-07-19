from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import event

from app.devices.models import Device, DeviceGroup, DeviceGroupMembership, GroupType
from tests.helpers import create_device_record
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterator

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture(autouse=True)
async def seed_packs(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()


@contextlib.contextmanager
def _capture_statements(session: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []

    def listener(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    bind = session.bind
    assert bind is not None
    sync_engine = bind.sync_engine if hasattr(bind, "sync_engine") else bind
    event.listen(sync_engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(sync_engine, "before_cursor_execute", listener)


def _count_reads_through_lock(statements: list[str]) -> int:
    """Count SELECT/WITH reads through the locked recheck, stopping at the first
    ``INSERT INTO test_runs`` (the run row write that marks the end of the
    candidate-selection phase).
    """
    reads = 0
    for stmt in statements:
        upper = stmt.lstrip().upper()
        if upper.startswith("INSERT INTO TEST_RUNS"):
            break
        if upper.startswith(("SELECT", "WITH")):
            reads += 1
    return reads


async def _seed_available_device(
    db_session: AsyncSession,
    host_id: str,
    identity_value: str,
    name: str,
    *,
    pack_id: str = "appium-uiautomator2",
    platform_id: str = "android_mobile",
) -> Device:
    return await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=identity_value,
        connection_target=identity_value,
        name=name,
        pack_id=pack_id,
        platform_id=platform_id,
        os_version="14",
        operational_state="available",
    )


async def _seed_static_group(
    db_session: AsyncSession,
    *,
    key: str,
    device_ids: list[uuid.UUID],
) -> None:
    group = DeviceGroup(key=key, name=key, group_type=GroupType.static)
    db_session.add(group)
    await db_session.flush()
    for device_id in device_ids:
        db_session.add(DeviceGroupMembership(group_id=group.id, device_id=device_id))
    await db_session.commit()


async def _seed_dynamic_group(
    db_session: AsyncSession,
    *,
    key: str,
    filters: dict[str, Any],
) -> None:
    group = DeviceGroup(key=key, name=key, group_type=GroupType.dynamic, filters=filters)
    db_session.add(group)
    await db_session.commit()


def _run_payload(
    *,
    groups: list[str] | None = None,
    pack_id: str = "appium-uiautomator2",
    platform_id: str = "android_mobile",
    count: int = 1,
) -> dict[str, Any]:
    req: dict[str, Any] = {"pack_id": pack_id, "platform_id": platform_id, "count": count}
    if groups is not None:
        req["groups"] = groups
    return {"name": "Group Run", "requirements": [req]}


@pytest.mark.db
async def test_unknown_run_requirement_group_is_422(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    response = await client.post("/api/runs", json=_run_payload(groups=["missing"]))
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "unknown device groups: missing"


@pytest.mark.db
async def test_run_requirement_groups_are_anded(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """A requirement with two groups only matches devices in BOTH groups."""
    # Seed a decoy first so that without group filtering it would be picked by
    # created_at order — proving the AND filter actually constrains selection.
    decoy = await _seed_available_device(db_session, default_host_id, "and-decoy", "Decoy")
    east_tv = await _seed_available_device(db_session, default_host_id, "and-tv", "TV")
    east_phone = await _seed_available_device(db_session, default_host_id, "and-phone", "Phone")
    west_tv = await _seed_available_device(db_session, default_host_id, "and-wtv", "W TV")

    await _seed_static_group(db_session, key="east", device_ids=[east_tv.id, east_phone.id])
    await _seed_static_group(db_session, key="tv", device_ids=[east_tv.id, west_tv.id])
    await _seed_static_group(db_session, key="west", device_ids=[west_tv.id])
    # decoy is in no group; it must never be selected when groups filter applies.
    _ = decoy

    response = await client.post("/api/runs", json=_run_payload(groups=["east", "tv"]))
    assert response.status_code == 201
    body = response.json()
    assert len(body["devices"]) == 1
    assert body["devices"][0]["identity_value"] == "and-tv"


@pytest.mark.db
async def test_run_requirement_without_groups_keeps_pack_platform_routing(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """A requirement with no groups routes purely by pack/platform (no group constraint)."""
    device = await _seed_available_device(db_session, default_host_id, "no-group", "No Group")
    await _seed_static_group(db_session, key="east", device_ids=[device.id])

    response = await client.post("/api/runs", json=_run_payload())
    assert response.status_code == 201
    body = response.json()
    assert len(body["devices"]) == 1
    assert body["devices"][0]["identity_value"] == "no-group"


@pytest.mark.db
async def test_run_allocation_selects_in_request_order_excluding_already_selected(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Two requirements share the same candidate pool; the second excludes the first's pick."""
    # Decoy seeded first so it would be picked without group filtering.
    decoy = await _seed_available_device(db_session, default_host_id, "order-decoy", "Decoy")
    d1 = await _seed_available_device(db_session, default_host_id, "order-1", "Order 1")
    d2 = await _seed_available_device(db_session, default_host_id, "order-2", "Order 2")
    _ = decoy

    await _seed_static_group(db_session, key="pool-a", device_ids=[d1.id, d2.id])

    payload = {
        "name": "Two Req Run",
        "requirements": [
            {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1, "groups": ["pool-a"]},
            {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1, "groups": ["pool-a"]},
        ],
    }
    response = await client.post("/api/runs", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert len(body["devices"]) == 2
    identities = {dev["identity_value"] for dev in body["devices"]}
    assert identities == {"order-1", "order-2"}


@pytest.mark.db
async def test_run_allocation_read_count_constant_at_candidate_scale(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Candidate-selection reads (through locked recheck, before INSERT INTO test_runs)
    must not grow with the number of candidate devices.
    """
    one_device = await _seed_available_device(db_session, default_host_id, "scale-1", "Scale 1")
    await _seed_static_group(db_session, key="scale-a", device_ids=[one_device.id])

    with _capture_statements(db_session) as one_statements:
        resp = await client.post("/api/runs", json=_run_payload(groups=["scale-a"]))
    assert resp.status_code == 201
    one_reads = _count_reads_through_lock(one_statements)

    # Add 24 more candidate devices in a fresh group (the first run reserved
    # scale-1, so reuse is not possible). The read-count comparison is between
    # a 1-candidate run and a 24-candidate run.
    extra_ids: list[uuid.UUID] = []
    for i in range(2, 26):
        device = await _seed_available_device(db_session, default_host_id, f"scale-{i}", f"Scale {i}")
        extra_ids.append(device.id)
    await _seed_static_group(db_session, key="scale-many", device_ids=extra_ids)

    with _capture_statements(db_session) as many_statements:
        resp = await client.post("/api/runs", json=_run_payload(groups=["scale-many"], count=24))
    assert resp.status_code == 201
    many_reads = _count_reads_through_lock(many_statements)

    assert many_reads == one_reads, f"candidate-selection reads grew with candidate count: {one_reads} -> {many_reads}"


@pytest.mark.db
async def test_run_allocation_read_count_constant_at_requirement_scale(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Candidate-selection reads must not grow with the number of requirements."""

    async def _run_with_n_requirements(n: int) -> int:
        devices: list[uuid.UUID] = []
        for i in range(n):
            device = await _seed_available_device(db_session, default_host_id, f"req-{n}-{i}", f"Req {n} D{i}")
            devices.append(device.id)
        group_key = f"req-group-{n}"
        await _seed_static_group(db_session, key=group_key, device_ids=devices)
        payload: dict[str, Any] = {
            "name": f"Req Scale {n}",
            "requirements": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "count": 1,
                    "groups": [group_key],
                }
                for _ in range(n)
            ],
        }
        with _capture_statements(db_session) as statements:
            resp = await client.post("/api/runs", json=payload)
        assert resp.status_code == 201
        return _count_reads_through_lock(statements)

    one_reads = await _run_with_n_requirements(1)
    ten_reads = await _run_with_n_requirements(10)
    assert ten_reads == one_reads, f"candidate-selection reads grew with requirement count: {one_reads} -> {ten_reads}"


@pytest.mark.db
async def test_dynamic_requirement_group_parity_with_direct_routing(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """A dynamic group used as a requirement group selects the same devices the
    group-membership index (exposed by the device-groups detail endpoint)
    reports as members — i.e. run routing agrees with direct group routing.
    """
    member_a = await _seed_available_device(db_session, default_host_id, "parity-a", "Parity A")
    member_b = await _seed_available_device(db_session, default_host_id, "parity-b", "Parity B")
    outsider = await _seed_available_device(db_session, default_host_id, "parity-out", "Parity Out")
    _ = outsider

    await _seed_static_group(db_session, key="parity-pool", device_ids=[member_a.id, member_b.id])
    await _seed_dynamic_group(
        db_session,
        key="parity-dyn",
        filters={"member_of": ["parity-pool"]},
    )

    detail = await client.get("/api/device-groups/parity-dyn")
    assert detail.status_code == 200
    expected_ids = {device["id"] for device in detail.json()["devices"]}
    assert expected_ids == {str(member_a.id), str(member_b.id)}

    response = await client.post(
        "/api/runs",
        json=_run_payload(groups=["parity-dyn"], count=2),
    )
    assert response.status_code == 201
    reserved_ids = {device["device_id"] for device in response.json()["devices"]}
    assert reserved_ids == expected_ids
