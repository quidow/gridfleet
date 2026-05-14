from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.events.models import SystemEvent
from app.services.pack_audit_service import record_pack_tarball_fetched, record_pack_upload

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_record_pack_upload_writes_system_event(db_session: AsyncSession) -> None:
    await record_pack_upload(
        db_session,
        username="alice",
        pack_id="vendor-foo",
        release="0.1.0",
        artifact_sha256="a" * 64,
        origin_filename="vendor-foo-0.1.0.tar.gz",
    )
    await db_session.flush()
    rows = (
        (await db_session.execute(select(SystemEvent).where(SystemEvent.type == "driver_pack.upload"))).scalars().all()
    )
    assert len(rows) == 1
    payload = rows[0].data
    assert payload["pack_id"] == "vendor-foo"
    assert payload["release"] == "0.1.0"
    assert payload["uploaded_by"] == "alice"
    assert payload["artifact_sha256"] == "a" * 64
    assert payload["origin_filename"] == "vendor-foo-0.1.0.tar.gz"


@pytest.mark.asyncio
async def test_record_pack_upload_event_id_is_unique(db_session: AsyncSession) -> None:
    await record_pack_upload(
        db_session,
        username="bob",
        pack_id="vendor-bar",
        release="1.0.0",
        artifact_sha256="b" * 64,
        origin_filename="vendor-bar-1.0.0.tar.gz",
    )
    await record_pack_upload(
        db_session,
        username="bob",
        pack_id="vendor-bar",
        release="1.0.1",
        artifact_sha256="c" * 64,
        origin_filename="vendor-bar-1.0.1.tar.gz",
    )
    await db_session.flush()
    rows = (
        (await db_session.execute(select(SystemEvent).where(SystemEvent.type == "driver_pack.upload"))).scalars().all()
    )
    assert len(rows) == 2
    event_ids = {r.event_id for r in rows}
    assert len(event_ids) == 2


@pytest.mark.asyncio
async def test_record_pack_tarball_fetched_writes_system_event(db_session: AsyncSession) -> None:
    await record_pack_tarball_fetched(
        db_session,
        host_id="host-abc",
        pack_id="vendor-foo",
        release="0.1.0",
        artifact_sha256="a" * 64,
    )
    await db_session.flush()
    rows = (
        (await db_session.execute(select(SystemEvent).where(SystemEvent.type == "driver_pack.tarball_fetched")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    payload = rows[0].data
    assert payload["host_id"] == "host-abc"
    assert payload["pack_id"] == "vendor-foo"
    assert payload["release"] == "0.1.0"
    assert payload["artifact_sha256"] == "a" * 64


@pytest.mark.asyncio
async def test_record_pack_tarball_fetched_event_id_is_unique(db_session: AsyncSession) -> None:
    await record_pack_tarball_fetched(
        db_session,
        host_id="host-1",
        pack_id="vendor-foo",
        release="0.1.0",
        artifact_sha256="d" * 64,
    )
    await record_pack_tarball_fetched(
        db_session,
        host_id="host-2",
        pack_id="vendor-foo",
        release="0.1.0",
        artifact_sha256="d" * 64,
    )
    await db_session.flush()
    rows = (
        (await db_session.execute(select(SystemEvent).where(SystemEvent.type == "driver_pack.tarball_fetched")))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    event_ids = {r.event_id for r in rows}
    assert len(event_ids) == 2
