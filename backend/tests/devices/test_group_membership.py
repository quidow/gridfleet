from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import event

from app.devices.group_keys import is_valid_group_key
from app.devices.models import Device, DeviceGroup, GroupType
from app.devices.services.group_membership import (
    DeviceGroupFacts,
    evaluate_group_memberships,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def _static(key: str, name: str | None = None) -> DeviceGroup:
    group = DeviceGroup(key=key, name=name or key, group_type=GroupType.static)
    group.id = uuid.uuid4()
    return group


def _dynamic(
    key: str,
    *,
    name: str | None = None,
    filters: dict[str, Any] | None = None,
    member_of: list[str] | None = None,
) -> DeviceGroup:
    payload: dict[str, Any] = dict(filters or {})
    if member_of is not None:
        payload["member_of"] = member_of
    group = DeviceGroup(
        key=key,
        name=name or key,
        group_type=GroupType.dynamic,
        filters=payload or None,
    )
    group.id = uuid.uuid4()
    return group


def _device(
    key: str,
    *,
    device_type: str = "real_device",
    pack_id: str = "appium-uiautomator2",
    platform_id: str = "android_mobile",
) -> Device:
    device = Device(
        pack_id=pack_id,
        platform_id=platform_id,
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=key,
        connection_target=key,
        name=key,
        os_version="14",
        host_id=uuid.uuid4(),
        device_type=device_type,  # type: ignore[arg-type]
        connection_type="usb",  # type: ignore[arg-type]
    )
    device.id = uuid.uuid4()
    return device


def _facts(
    *,
    static_group_keys: set[str] | None = None,
    operational_state: str = "available",
    is_reserved: bool = False,
    readiness_state: str = "verified",
    hardware_telemetry_state: str = "ok",
    needs_attention: bool = False,
) -> DeviceGroupFacts:
    return DeviceGroupFacts(
        operational_state=operational_state,  # type: ignore[arg-type]
        is_reserved=is_reserved,
        readiness_state=readiness_state,
        hardware_telemetry_state=hardware_telemetry_state,  # type: ignore[arg-type]
        needs_attention=needs_attention,
        static_group_keys=frozenset(static_group_keys or ()),
    )


def _facts_map(devices: list[Device], **per_device: set[str]) -> dict[uuid.UUID, DeviceGroupFacts]:
    """Build a facts map. Pass keyword args like ``device_id_key=static_group_keys``..."""

    result: dict[uuid.UUID, DeviceGroupFacts] = {}
    for device in devices:
        keys = per_device.get(device.identity_value, set())
        result[device.id] = _facts(static_group_keys=keys)
    return result


def test_member_of_and_native_filters_are_anded() -> None:
    east = _static("east")
    tv = _static("tv")
    east_tvs = _dynamic(
        "east-tvs",
        member_of=["east", "tv"],
        filters={"platform_id": "tv"},
    )
    east_tv = _device("east-tv", platform_id="tv")
    east_phone = _device("east-phone", platform_id="android_mobile")

    groups = [east, tv, east_tvs]
    devices = [east_tv, east_phone]
    facts = {
        east_tv.id: _facts(static_group_keys={"east", "tv"}),
        east_phone.id: _facts(static_group_keys={"east"}),
    }
    index = evaluate_group_memberships(groups=groups, devices=devices, facts_by_device_id=facts)

    assert index.device_ids("east") == {east_tv.id, east_phone.id}
    assert index.device_ids("tv") == {east_tv.id}
    assert index.device_ids("east-tvs") == {east_tv.id}


def test_unknown_member_of_keys_resolve_to_empty_membership() -> None:
    group = _dynamic("missing", member_of=["does-not-exist"])
    device = _device("d1")
    index = evaluate_group_memberships(
        groups=[group],
        devices=[device],
        facts_by_device_id={device.id: _facts(static_group_keys=set())},
    )
    assert index.device_ids("missing") == set()


def test_dynamic_to_dynamic_member_of_is_ignored() -> None:
    static_a = _static("a")
    dyn_b = _dynamic("b", member_of=["a"])
    dyn_c = _dynamic("c", member_of=["b"])  # references a dynamic group
    device = _device("d1")

    index = evaluate_group_memberships(
        groups=[static_a, dyn_b, dyn_c],
        devices=[device],
        facts_by_device_id={device.id: _facts(static_group_keys={"a"})},
    )
    # b matches (member_of=[a], no native filters)
    assert device.id in index.device_ids("b")
    # c references a dynamic group (b); membership must be empty
    assert index.device_ids("c") == set()


def test_duplicate_member_of_references_normalized_once() -> None:
    static_a = _static("a")
    group = _dynamic("g", member_of=["a", "a"])
    device = _device("d1")
    index = evaluate_group_memberships(
        groups=[static_a, group],
        devices=[device],
        facts_by_device_id={device.id: _facts(static_group_keys={"a"})},
    )
    assert device.id in index.device_ids("g")


def test_matches_all_helper() -> None:
    static_a = _static("a")
    static_b = _static("b")
    device = _device("d1")
    index = evaluate_group_memberships(
        groups=[static_a, static_b],
        devices=[device],
        facts_by_device_id={device.id: _facts(static_group_keys={"a", "b"})},
    )
    assert index.matches_all(device.id, ["a", "b"]) is True
    assert index.matches_all(device.id, ["a", "missing"]) is False


def test_evaluate_group_memberships_performs_no_database_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pure evaluator must not touch the DB session."""
    from app.devices.services import group_membership as mod

    def _no_async_call(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pure evaluator must not issue async DB calls")

    monkeypatch.setattr(mod, "load_group_membership_index", _no_async_call)
    static_a = _static("a")
    device = _device("d1")

    class _ExplodingSession:
        def execute(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("pure evaluator must not use the session")

    index = evaluate_group_memberships(
        groups=[static_a],
        devices=[device],
        facts_by_device_id={device.id: _facts(static_group_keys={"a"})},
    )
    assert index.device_ids("a") == {device.id}


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


def _count_reads(statements: list[str]) -> int:
    return sum(stmt.lstrip().upper().startswith(("SELECT", "WITH")) for stmt in statements)


async def _seed_groups_and_devices(
    db_session: AsyncSession,
    *,
    dynamic_groups: int,
    devices: int,
    host_id: uuid.UUID,
) -> None:
    """Seed ``dynamic_groups`` dynamic groups and ``devices`` devices."""
    # One static group referenced by every dynamic group; ensures member_of joins
    # are exercised rather than a trivial empty-filter dynamic group.
    static = DeviceGroup(key=f"static-ref-{uuid.uuid4().hex[:6]}", name="static ref", group_type=GroupType.static)
    db_session.add(static)
    for i in range(dynamic_groups):
        dg = DeviceGroup(
            key=f"dyn-{uuid.uuid4().hex[:6]}",
            name=f"Dyn {i}",
            group_type=GroupType.dynamic,
            filters={"member_of": [static.key], "device_type": "real_device"},
        )
        db_session.add(dg)
    for j in range(devices):
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=f"gd-{uuid.uuid4().hex[:8]}",
            connection_target=f"gd-{j}",
            name=f"GD {j}",
            os_version="14",
            host_id=host_id,
            device_type="real_device",
            connection_type="usb",
        )
        db_session.add(device)
    await db_session.commit()


@pytest.mark.db
async def test_group_list_reads_do_not_scale_with_dynamic_group_count(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    await _seed_groups_and_devices(db_session, dynamic_groups=1, devices=2, host_id=db_host.id)
    with _capture_statements(db_session) as statements:
        response = await client.get("/api/device-groups")
        assert response.status_code == 200
    one = _count_reads(statements)

    await _seed_groups_and_devices(db_session, dynamic_groups=20, devices=40, host_id=db_host.id)
    with _capture_statements(db_session) as statements:
        response = await client.get("/api/device-groups")
        assert response.status_code == 200
    many = _count_reads(statements)
    assert many == one, f"group list reads scaled with group count: {one} -> {many}"


@pytest.mark.db
async def test_group_detail_reads_do_not_scale_beyond_device_list_serialization(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    """Group-detail serialization must not add per-member queries beyond what
    the device-list endpoint already issues for the same ``serialize_device``
    path. Both endpoints share the presenter, so the per-member delta must
    match.
    """
    create = await client.post(
        "/api/device-groups",
        json={"key": "members-scale", "name": "Members scale", "group_type": "static"},
    )
    assert create.status_code == 201

    async def _add_device(identity: str, name: str) -> Device:
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=identity,
            connection_target=identity,
            name=name,
            os_version="14",
            host_id=db_host.id,
            device_type="real_device",
            connection_type="usb",
        )
        db_session.add(device)
        return device

    one_device = await _add_device("scale-1", "Scale 1")
    await db_session.commit()
    add = await client.post(
        "/api/device-groups/members-scale/members",
        json={"device_ids": [str(one_device.id)]},
    )
    assert add.status_code == 200

    with _capture_statements(db_session) as group_one:
        assert (await client.get("/api/device-groups/members-scale")).status_code == 200
    with _capture_statements(db_session) as list_one:
        assert (await client.get("/api/devices")).status_code == 200

    extras: list[Device] = []
    for j in range(2, 11):
        extra = await _add_device(f"scale-{j}-{uuid.uuid4().hex[:4]}", f"Scale {j}")
        extras.append(extra)
    await db_session.commit()
    add_more = await client.post(
        "/api/device-groups/members-scale/members",
        json={"device_ids": [str(d.id) for d in extras]},
    )
    assert add_more.status_code == 200

    with _capture_statements(db_session) as group_many:
        assert (await client.get("/api/device-groups/members-scale")).status_code == 200
    with _capture_statements(db_session) as list_many:
        assert (await client.get("/api/devices")).status_code == 200

    group_delta = _count_reads(group_many) - _count_reads(group_one)
    list_delta = _count_reads(list_many) - _count_reads(list_one)
    assert group_delta == list_delta, (
        f"group-detail per-member read delta ({group_delta}) differs from device-list "
        f"per-member read delta ({list_delta}); group-detail reintroduced per-member queries"
    )


def test_group_key_pattern_helper_matches_spec() -> None:
    assert is_valid_group_key("east-lab")
    assert not is_valid_group_key("East")
    assert not is_valid_group_key("-east")
    assert not is_valid_group_key("east-")
    assert not is_valid_group_key("east_lab")
    assert not is_valid_group_key("a" * 65)
    assert is_valid_group_key("a" * 63)
