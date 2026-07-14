"""Phase 1b — the registration-bound boot fence and the per-section dedup token.

The push endpoint locks the host row, validates the incoming boot_id against the
host's registered boot, and compares each moved section's token
(boot_id, section_sequence, payload_sha256) against the host's ingest cursor to
decide whether the section is a new generation or a re-delivery.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

from app.core.leader import state_store as control_plane_state_store
from app.hosts.models import Host, HostStatus, OSType
from app.hosts.observation_token import canonical_section_hash
from app.hosts.schemas import HostStatusPush
from app.hosts.service_status_push import HOST_STATUS_NAMESPACE, BootFenceError, HostStatusPushService

if TYPE_CHECKING:
    from httpx2 import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _make_host(db_session: AsyncSession, *, hostname: str, boot_id: uuid.UUID | None = None) -> Host:
    host = Host(
        hostname=hostname,
        ip="10.0.0.9",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
        current_boot_id=boot_id,
    )
    db_session.add(host)
    await db_session.commit()
    await db_session.refresh(host)
    return host


def _node_section(
    *, sequence: int, nodes: list[dict[str, Any]] | None = None, reported_at: str = "2026-07-14T00:00:00+00:00"
) -> dict[str, Any]:
    body: dict[str, Any] = {"reported_at": reported_at, "nodes": nodes or []}
    return {**body, "section_sequence": sequence, "payload_sha256": canonical_section_hash(body)}


async def _post(
    client: AsyncClient, host_id: uuid.UUID, *, boot_id: uuid.UUID | None, node_health: dict[str, Any] | None = None
) -> Response:
    body: dict[str, Any] = {"host_id": str(host_id)}
    if boot_id is not None:
        body["boot_id"] = str(boot_id)
    if node_health is not None:
        body["node_health"] = node_health
    return await client.post("/agent/hosts/status", json=body)


async def _snapshot_section(db_session: AsyncSession, host_id: uuid.UUID, name: str) -> dict[str, Any] | None:
    value = await control_plane_state_store.get_value(db_session, HOST_STATUS_NAMESPACE, str(host_id))
    if not isinstance(value, dict):
        return None
    payload = value.get("payload")
    section = payload.get(name) if isinstance(payload, dict) else None
    return section if isinstance(section, dict) else None


# --------------------------------------------------------------------------- #
# Boot-fence truth table
# --------------------------------------------------------------------------- #


async def test_fence_null_current_missing_boot_processes(client: AsyncClient, db_session: AsyncSession) -> None:
    host = await _make_host(db_session, hostname="fence-null-missing")
    resp = await _post(client, host.id, boot_id=None, node_health=_node_section(sequence=1))
    assert resp.status_code == 204
    await db_session.refresh(host)
    assert host.current_boot_id is None


async def test_fence_null_current_present_boot_adopts(client: AsyncClient, db_session: AsyncSession) -> None:
    host = await _make_host(db_session, hostname="fence-adopt")
    boot = uuid.uuid4()
    resp = await _post(client, host.id, boot_id=boot, node_health=_node_section(sequence=1))
    assert resp.status_code == 204
    await db_session.refresh(host)
    assert host.current_boot_id == boot


async def test_fence_matching_boot_processes(client: AsyncClient, db_session: AsyncSession) -> None:
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="fence-match", boot_id=boot)
    resp = await _post(client, host.id, boot_id=boot, node_health=_node_section(sequence=1))
    assert resp.status_code == 204


async def test_fence_mismatched_boot_rejected_409(client: AsyncClient, db_session: AsyncSession) -> None:
    host = await _make_host(db_session, hostname="fence-reject", boot_id=uuid.uuid4())
    resp = await _post(client, host.id, boot_id=uuid.uuid4(), node_health=_node_section(sequence=1))
    assert resp.status_code == 409
    # Rejected before liveness: last_heartbeat is not stamped by a fenced push.
    await db_session.refresh(host)
    assert host.last_heartbeat is None


async def test_fence_set_current_missing_boot_processes(client: AsyncClient, db_session: AsyncSession) -> None:
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="fence-legacy-after", boot_id=boot)
    resp = await _post(client, host.id, boot_id=None, node_health=_node_section(sequence=1))
    assert resp.status_code == 204
    await db_session.refresh(host)
    assert host.current_boot_id == boot  # unchanged; cannot fence a tokenless push


# --------------------------------------------------------------------------- #
# Per-section cursor / dedup token
# --------------------------------------------------------------------------- #


async def test_cursor_advance_draws_new_revision_and_redelivery_reuses(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="cursor-advance", boot_id=boot)

    await _post(client, host.id, boot_id=boot, node_health=_node_section(sequence=1))
    section1 = await _snapshot_section(db_session, host.id, "node_health")
    assert section1 is not None
    first_rev = section1["observation_revision"]
    assert isinstance(first_rev, int)

    # Re-delivery of the same gather (same sequence + body) reuses the revision.
    await _post(client, host.id, boot_id=boot, node_health=_node_section(sequence=1))
    section_redeliver = await _snapshot_section(db_session, host.id, "node_health")
    assert section_redeliver is not None
    assert section_redeliver["observation_revision"] == first_rev

    # A new gather (higher sequence) draws a strictly-greater revision.
    await _post(client, host.id, boot_id=boot, node_health=_node_section(sequence=2))
    section2 = await _snapshot_section(db_session, host.id, "node_health")
    assert section2 is not None
    assert section2["observation_revision"] > first_rev


async def test_cursor_stale_lower_sequence_preserves_snapshot(client: AsyncClient, db_session: AsyncSession) -> None:
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="cursor-stale", boot_id=boot)

    await _post(client, host.id, boot_id=boot, node_health=_node_section(sequence=5, nodes=[{"port": 4723}]))
    fresh = await _snapshot_section(db_session, host.id, "node_health")
    assert fresh is not None
    fresh_rev = fresh["observation_revision"]

    # A stale (lower-sequence) out-of-order delivery must not regress the snapshot.
    await _post(client, host.id, boot_id=boot, node_health=_node_section(sequence=4, nodes=[]))
    after = await _snapshot_section(db_session, host.id, "node_health")
    assert after is not None
    assert after["observation_revision"] == fresh_rev
    assert after["nodes"] == [{"port": 4723}]  # body preserved from the newer generation


async def test_same_sequence_different_payload_is_processed(client: AsyncClient, db_session: AsyncSession) -> None:
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="cursor-conflict", boot_id=boot)

    await _post(client, host.id, boot_id=boot, node_health=_node_section(sequence=3, nodes=[]))
    first = await _snapshot_section(db_session, host.id, "node_health")
    assert first is not None
    first_rev = first["observation_revision"]

    # Same sequence, different payload = contract violation → processed (latest wins).
    await _post(client, host.id, boot_id=boot, node_health=_node_section(sequence=3, nodes=[{"port": 9999}]))
    second = await _snapshot_section(db_session, host.id, "node_health")
    assert second is not None
    assert second["observation_revision"] > first_rev
    assert second["nodes"] == [{"port": 9999}]


# --------------------------------------------------------------------------- #
# apply_status_push fold-payload contract (unit)
# --------------------------------------------------------------------------- #


async def test_redelivery_is_omitted_from_the_fold_payload(db_session: AsyncSession) -> None:
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="fold-skip", boot_id=boot)
    svc = HostStatusPushService(publisher=Mock())

    first = HostStatusPush(host_id=host.id, boot_id=boot, node_health=_node_section(sequence=1))
    fold_payload = await svc.apply_status_push(db_session, host, first)
    await db_session.commit()
    assert "node_health" in fold_payload  # first generation is folded

    second = HostStatusPush(host_id=host.id, boot_id=boot, node_health=_node_section(sequence=1))
    fold_payload = await svc.apply_status_push(db_session, host, second)
    await db_session.commit()
    assert "node_health" not in fold_payload  # re-delivery is not re-folded


async def test_boot_fence_rejected_raises(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, hostname="fence-raise", boot_id=uuid.uuid4())
    svc = HostStatusPushService(publisher=Mock())
    push = HostStatusPush(host_id=host.id, boot_id=uuid.uuid4())
    try:
        await svc.apply_status_push(db_session, host, push)
    except BootFenceError:
        return
    raise AssertionError("expected BootFenceError")


def test_canonical_section_hash_matches_agent_golden() -> None:
    """Parity guard: the backend and agent canonical hashes MUST agree. This
    golden digest is asserted identically in the agent suite (test_probe_loop)."""
    section = {
        "reported_at": "2026-07-14T00:00:00+00:00",
        "nodes": [{"port": 4723, "running": True}],
        "section_sequence": 5,
        "payload_sha256": "ignored",
    }
    assert canonical_section_hash(section) == "7c50675aa686cac3e8c02272cefcf6564e5ea61873933a3cdaa519eeec27110e"


async def test_concurrent_pushes_serialize_on_host_lock(
    db_session_maker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    """B1: two concurrent pushes cannot both read the same cursor and commit in
    reverse. The host-row FOR UPDATE serializes them, so even when the lower
    sequence commits first the cursor ends at the higher sequence."""
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="b1-concurrency", boot_id=boot)
    svc = HostStatusPushService(publisher=Mock())

    lower_locked = asyncio.Event()
    release_lower = asyncio.Event()

    async def push_lower() -> None:  # section_sequence 11: acquires the lock first
        async with db_session_maker() as session:
            locked = await session.get(Host, host.id, with_for_update=True)
            assert locked is not None
            lower_locked.set()
            await release_lower.wait()
            await svc.apply_status_push(
                session, locked, HostStatusPush(host_id=host.id, boot_id=boot, node_health=_node_section(sequence=11))
            )
            await session.commit()

    async def push_higher() -> None:  # section_sequence 12: blocks on the lock
        await lower_locked.wait()
        async with db_session_maker() as session:
            release_lower.set()  # let the lower push commit and free the lock
            locked = await session.get(Host, host.id, with_for_update=True)
            assert locked is not None
            await svc.apply_status_push(
                session, locked, HostStatusPush(host_id=host.id, boot_id=boot, node_health=_node_section(sequence=12))
            )
            await session.commit()

    await asyncio.gather(push_lower(), push_higher())

    await db_session.refresh(host)
    assert host.observation_cursors["node_health"]["section_sequence"] == 12
