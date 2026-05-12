import contextlib
import uuid
from collections.abc import Iterator
from typing import Any, get_type_hints

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.device import Device
from app.models.test_run import TestRun
from app.schemas.run import ReservedDeviceInfo, RunCreate, UnavailableInclude
from app.services import run_service
from app.services.run_service import _build_device_info
from tests.helpers import create_device, create_reserved_run


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
    sync_engine = bind.sync_engine if hasattr(bind, "sync_engine") else bind
    event.listen(sync_engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(sync_engine, "before_cursor_execute", listener)


pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def test_unavailable_include_round_trip() -> None:
    item = UnavailableInclude(include="capabilities", reason="device_offline")
    dumped = item.model_dump()
    assert dumped == {"include": "capabilities", "reason": "device_offline"}
    rebuilt = UnavailableInclude.model_validate(dumped)
    assert rebuilt == item


def test_unavailable_include_requires_both_fields() -> None:
    with pytest.raises(ValueError):
        UnavailableInclude(include="config")  # type: ignore[call-arg]


def test_reserved_device_info_has_tier1_and_tier2_fields() -> None:
    hints = get_type_hints(ReservedDeviceInfo)
    for field in (
        "name",
        "device_type",
        "connection_type",
        "manufacturer",
        "model",
        "config",
        "live_capabilities",
        "unavailable_includes",
    ):
        assert field in hints, f"{field} missing from ReservedDeviceInfo"


def test_reserved_device_info_construction_without_tier1_still_valid() -> None:
    info = ReservedDeviceInfo(
        device_id="d",
        identity_value="i",
        pack_id="p",
        platform_id="pl",
        os_version="1",
    )
    assert info.name is None
    assert info.device_type is None


def test_reserved_device_info_has_tags_field() -> None:
    hints = get_type_hints(ReservedDeviceInfo)
    assert "tags" in hints


def test_reserved_device_info_round_trips_with_tags() -> None:
    info = ReservedDeviceInfo(
        device_id="d-1",
        identity_value="iv-1",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        os_version="12",
        tags={"screen_type": "4k"},
    )
    dumped = info.model_dump()
    assert dumped["tags"] == {"screen_type": "4k"}
    rebuilt = ReservedDeviceInfo.model_validate(dumped)
    assert rebuilt.tags == {"screen_type": "4k"}


@pytest.mark.db
@pytest.mark.asyncio
async def test_build_device_info_populates_tier1_fields(db_session: AsyncSession, default_host_id: str) -> None:
    created = await create_device(
        db_session,
        host_id=default_host_id,
        name="emu-pixel7-1",
        device_type="emulator",
        connection_type="virtual",
        manufacturer="Google",
        model="Pixel 7",
    )
    # Reload with host eagerly loaded, matching production query pattern.
    result = await db_session.execute(select(Device).where(Device.id == created.id).options(selectinload(Device.host)))
    device = result.scalar_one()
    info = _build_device_info(device, platform_label="Android 14")
    assert info.name == "emu-pixel7-1"
    assert info.device_type == "emulator"
    assert info.connection_type == "virtual"
    assert info.manufacturer == "Google"
    assert info.model == "Pixel 7"


@pytest.mark.db
@pytest.mark.asyncio
async def test_build_device_info_populates_tags(db_session: AsyncSession, default_host_id: str) -> None:
    created = await create_device(
        db_session,
        host_id=default_host_id,
        name="tagged-device",
    )
    created.tags = {"screen_type": "4k", "rack": "A1"}
    await db_session.flush()
    result = await db_session.execute(select(Device).where(Device.id == created.id).options(selectinload(Device.host)))
    device = result.scalar_one()
    info = _build_device_info(device, platform_label="Android")
    assert info.tags == {"screen_type": "4k", "rack": "A1"}


@pytest.mark.db
@pytest.mark.asyncio
async def test_run_detail_devices_expose_tier1_fields(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="run-detail-device",
        device_type="emulator",
        connection_type="virtual",
    )
    run = await create_reserved_run(db_session, name="rd", devices=[device])
    response = await client.get(f"/api/runs/{run.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["devices"][0]["name"] == "run-detail-device"
    assert body["devices"][0]["device_type"] == "emulator"
    assert body["devices"][0]["connection_type"] == "virtual"


@pytest.mark.db
@pytest.mark.asyncio
async def test_run_list_reserved_devices_expose_tier1_fields(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="run-list-device",
        device_type="real_device",
        connection_type="usb",
    )
    await create_reserved_run(db_session, name="rl", devices=[device])
    response = await client.get("/api/runs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert any(
        item["reserved_devices"] and item["reserved_devices"][0]["name"] == "run-list-device" for item in body["items"]
    )


def test_parse_includes_none_returns_empty_set() -> None:
    assert run_service.parse_includes(None, allowed={"config", "capabilities"}) == set()


def test_parse_includes_empty_string_returns_empty_set() -> None:
    assert run_service.parse_includes("", allowed={"config", "capabilities"}) == set()


def test_parse_includes_strips_whitespace_and_skips_empty_tokens() -> None:
    assert run_service.parse_includes(" config , ", allowed={"config", "capabilities"}) == {"config"}


def test_parse_includes_accepts_multiple_tokens() -> None:
    assert run_service.parse_includes("config,capabilities", allowed={"config", "capabilities"}) == {
        "config",
        "capabilities",
    }


def test_parse_includes_rejects_unknown_token_with_machine_readable_detail() -> None:
    with pytest.raises(HTTPException) as exc:
        run_service.parse_includes("config,garbage", allowed={"config", "capabilities"})
    assert exc.value.status_code == 422
    detail: Any = exc.value.detail
    assert detail == {
        "code": "unknown_include",
        "values": ["garbage"],
    }


@pytest.mark.db
@pytest.mark.asyncio
async def test_hydrate_reserved_device_info_attaches_config(db_session: AsyncSession, default_host_id: str) -> None:
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="cfg-device",
    )
    device.device_config = {"app_username": "alice", "credentials_secret": "shh"}
    await db_session.flush()
    device = (
        await db_session.execute(select(Device).options(selectinload(Device.appium_node)).where(Device.id == device.id))
    ).scalar_one()

    info = ReservedDeviceInfo(
        device_id=str(device.id),
        identity_value=device.identity_value,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
    )
    await run_service.hydrate_reserved_device_info(db_session, info, device, includes={"config"})

    assert info.config is not None
    assert info.config["app_username"] == "alice"
    assert info.config["credentials_secret"] == "shh"
    assert info.live_capabilities is None
    assert info.unavailable_includes is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_hydrate_reserved_device_info_capabilities_uses_capability_service(
    db_session: AsyncSession, default_host_id: str
) -> None:
    device = await create_device(db_session, host_id=default_host_id, name="cap-device")
    device = (
        await db_session.execute(select(Device).options(selectinload(Device.appium_node)).where(Device.id == device.id))
    ).scalar_one()

    info = ReservedDeviceInfo(
        device_id=str(device.id),
        identity_value=device.identity_value,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
    )
    await run_service.hydrate_reserved_device_info(db_session, info, device, includes={"capabilities"})

    assert info.live_capabilities is not None
    assert info.unavailable_includes is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_hydrate_reserved_device_infos_batches_lookup(db_session: AsyncSession, default_host_id: str) -> None:
    devices: list[Device] = []
    for i in range(5):
        d = await create_device(db_session, host_id=default_host_id, name=f"batch-{i}")
        d.device_config = {"app_username": f"u{i}"}
        devices.append(d)
    await db_session.flush()
    reloaded = (
        (
            await db_session.execute(
                select(Device).options(selectinload(Device.appium_node)).where(Device.id.in_([d.id for d in devices]))
            )
        )
        .scalars()
        .all()
    )
    pairs: list[tuple[ReservedDeviceInfo, Device]] = [
        (
            ReservedDeviceInfo(
                device_id=str(d.id),
                identity_value=d.identity_value,
                pack_id=d.pack_id,
                platform_id=d.platform_id,
                os_version=d.os_version,
            ),
            d,
        )
        for d in reloaded
    ]

    with _capture_statements(db_session) as statements:
        await run_service.hydrate_reserved_device_infos(db_session, pairs, includes={"config"})

    distinct_pairs = {(d.pack_id, d.platform_id) for d in reloaded}
    driver_pack_statements = [s for s in statements if "driver_packs" in s]
    assert len(driver_pack_statements) <= len(distinct_pairs), (
        f"expected ≤{len(distinct_pairs)} driver_packs queries, got {len(driver_pack_statements)}"
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_reserve_include_config_marks_missing_device_unavailable_after_commit(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="deleted-before-reserve-hydration",
        operational_state="available",
    )
    device.device_config = {"credentials_secret": "shh"}
    await db_session.commit()

    original_create_run = run_service.create_run

    async def create_run_then_delete_device(
        db: AsyncSession, data: RunCreate
    ) -> tuple[TestRun, list[ReservedDeviceInfo]]:
        run, infos = await original_create_run(db, data)
        await db.execute(sa_delete(Device).where(Device.id == uuid.UUID(infos[0].device_id)))
        await db.commit()
        return run, infos

    monkeypatch.setattr(run_service, "create_run", create_run_then_delete_device)

    response = await client.post(
        "/api/runs?include=config",
        json={
            "name": "missing-device-reserve",
            "requirements": [{"pack_id": device.pack_id, "platform_id": device.platform_id, "count": 1}],
        },
    )

    assert response.status_code == 201, response.text
    entry = response.json()["devices"][0]
    assert entry["config"] is None
    assert entry["unavailable_includes"] == [{"include": "config", "reason": "device_not_found"}]


@pytest.mark.db
@pytest.mark.asyncio
async def test_reserve_with_include_config_attaches_config_per_device(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    devices = []
    for i in range(2):
        d = await create_device(
            db_session,
            host_id=default_host_id,
            name=f"res-cfg-{i}",
            operational_state="available",
        )
        d.device_config = {"k": f"v{i}", "credentials_secret": "shh"}
        devices.append(d)
    await db_session.commit()

    payload = {
        "name": "res-cfg",
        "requirements": [{"pack_id": d.pack_id, "platform_id": d.platform_id, "count": 1} for d in devices],
    }
    response = await client.post("/api/runs?include=config", json=payload)

    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["devices"]) == 2
    for entry in body["devices"]:
        assert entry["config"] is not None
        assert entry["config"]["credentials_secret"] == "shh"


@pytest.mark.db
@pytest.mark.asyncio
async def test_reserve_with_include_capabilities_returns_wrapped_422(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="res-caps",
        operational_state="available",
    )
    payload = {
        "name": "res-caps-run",
        "requirements": [{"pack_id": device.pack_id, "platform_id": device.platform_id, "count": 1}],
    }
    response = await client.post("/api/runs?include=capabilities", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["details"]["code"] == "reserve_capabilities_unsupported"


@pytest.mark.db
@pytest.mark.asyncio
async def test_reservation_context_lookup_does_not_load_reserved_device_rows(
    db_session: AsyncSession, default_host_id: str
) -> None:
    devices = [
        await create_device(
            db_session,
            host_id=default_host_id,
            name=f"context-{index}",
            operational_state="available",
        )
        for index in range(3)
    ]
    await create_reserved_run(db_session, name="context-run", devices=devices)

    with _capture_statements(db_session) as statements:
        run, entry = await run_service.get_device_reservation_with_entry(db_session, devices[0].id)

    assert run is not None
    assert entry is not None
    device_selects = [statement for statement in statements if "FROM devices" in statement]
    assert device_selects == []


@pytest.mark.db
@pytest.mark.asyncio
async def test_reserve_with_include_config_adds_o1_queries(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    devices: list[Device] = []
    for i in range(10):
        d = await create_device(
            db_session,
            host_id=default_host_id,
            name=f"perf-{i}",
            operational_state="available",
        )
        d.device_config = {"app_username": f"u{i}"}
        devices.append(d)
    await db_session.commit()

    requirements = [{"pack_id": d.pack_id, "platform_id": d.platform_id, "count": 1} for d in devices]

    payload_baseline = {"name": "perf-base", "requirements": requirements}
    with _capture_statements(db_session) as baseline_statements:
        baseline = await client.post("/api/runs", json=payload_baseline)
    assert baseline.status_code == 201, baseline.text
    baseline_driver_pack_count = sum(1 for s in baseline_statements if "driver_packs" in s)

    run_id = baseline.json()["id"]
    await client.post(f"/api/runs/{run_id}/force-release")

    payload_include = {"name": "perf-inc", "requirements": requirements}
    with _capture_statements(db_session) as include_statements:
        included = await client.post("/api/runs?include=config", json=payload_include)
    assert included.status_code == 201, included.text
    include_driver_pack_count = sum(1 for s in include_statements if "driver_packs" in s)

    distinct_pairs = {(d.pack_id, d.platform_id) for d in devices}
    delta = include_driver_pack_count - baseline_driver_pack_count
    assert delta <= len(distinct_pairs), (
        f"include=config added {delta} driver_packs queries beyond baseline "
        f"({baseline_driver_pack_count} → {include_driver_pack_count}); "
        f"expected ≤{len(distinct_pairs)} (one per distinct pack/platform pair)"
    )
