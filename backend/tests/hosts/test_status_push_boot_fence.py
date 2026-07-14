"""Phase 1b — the registration-bound boot fence and the per-section dedup token.

The push endpoint locks the host row, validates the incoming boot_id against the
host's registered boot, and compares each moved section's token
(boot_id, section_sequence, payload_sha256) against the host's ingest cursor to
decide whether the section is a new generation or a re-delivery.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.leader import state_store as control_plane_state_store
from app.core.observation_revision import next_observation_revision
from app.hosts import service_status_push as status_push_module
from app.hosts.models import Host, HostStatus, OSType
from app.hosts.observation_token import canonical_section_hash, extract_token
from app.hosts.router_agent import status as status_endpoint
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


async def _begin_and_finalize(
    svc: HostStatusPushService,
    db_session: AsyncSession,
    host: Host,
    push: HostStatusPush,
) -> dict[str, Any]:
    pending = await svc.begin_status_push(db_session, host, push)
    fold_payload = await svc.finalize_status_push(db_session, host, pending)
    assert fold_payload is not None
    return fold_payload


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
    first_received_at = section1["observation_received_at"]
    assert isinstance(first_received_at, str)

    # Re-delivery of the same gather (same sequence + body) reuses the revision.
    await _post(client, host.id, boot_id=boot, node_health=_node_section(sequence=1))
    section_redeliver = await _snapshot_section(db_session, host.id, "node_health")
    assert section_redeliver is not None
    assert section_redeliver["observation_revision"] == first_rev
    assert section_redeliver["observation_received_at"] == first_received_at

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
    fold_payload = await _begin_and_finalize(svc, db_session, host, first)
    await db_session.commit()
    assert "node_health" in fold_payload  # first generation is folded

    second = HostStatusPush(host_id=host.id, boot_id=boot, node_health=_node_section(sequence=1))
    fold_payload = await _begin_and_finalize(svc, db_session, host, second)
    await db_session.commit()
    assert "node_health" not in fold_payload  # re-delivery is not re-folded


async def test_boot_fence_rejected_raises(db_session: AsyncSession) -> None:
    host = await _make_host(db_session, hostname="fence-raise", boot_id=uuid.uuid4())
    svc = HostStatusPushService(publisher=Mock())
    push = HostStatusPush(host_id=host.id, boot_id=uuid.uuid4())
    try:
        await svc.begin_status_push(db_session, host, push)
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
            await _begin_and_finalize(
                svc,
                session,
                locked,
                HostStatusPush(host_id=host.id, boot_id=boot, node_health=_node_section(sequence=11)),
            )
            await session.commit()

    async def push_higher() -> None:  # section_sequence 12: blocks on the lock
        await lower_locked.wait()
        async with db_session_maker() as session:
            release_lower.set()  # let the lower push commit and free the lock
            locked = await session.get(Host, host.id, with_for_update=True)
            assert locked is not None
            await _begin_and_finalize(
                svc,
                session,
                locked,
                HostStatusPush(host_id=host.id, boot_id=boot, node_health=_node_section(sequence=12)),
            )
            await session.commit()

    await asyncio.gather(push_lower(), push_higher())

    await db_session.refresh(host)
    assert host.observation_cursors["node_health"]["section_sequence"] == 12


# --------------------------------------------------------------------------- #
# Post-convergence publication barrier
# --------------------------------------------------------------------------- #


async def test_guarded_section_is_unstamped_until_post_convergence_finalize(
    db_session: AsyncSession,
) -> None:
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="publication-barrier", boot_id=boot)
    svc = HostStatusPushService(publisher=Mock())

    pending = await svc.begin_status_push(
        db_session,
        host,
        HostStatusPush(host_id=host.id, boot_id=boot, node_health=_node_section(sequence=1)),
    )
    await db_session.commit()

    before = await _snapshot_section(db_session, host.id, "node_health")
    assert before is not None
    assert "observation_revision" not in before
    await db_session.refresh(host)
    assert host.observation_cursors.get("node_health") is None

    locked = await db_session.get(Host, host.id, with_for_update=True)
    assert locked is not None
    fold_payload = await svc.finalize_status_push(db_session, locked, pending)
    await db_session.commit()

    assert fold_payload is not None
    after = await _snapshot_section(db_session, host.id, "node_health")
    assert after is not None
    assert isinstance(after["observation_revision"], int)


async def test_convergence_failure_leaves_guarded_snapshot_unstamped(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="publication-convergence-failure", boot_id=boot)
    svc = HostStatusPushService(
        publisher=Mock(),
        session_factory=db_session_maker,
        converge_host=AsyncMock(side_effect=RuntimeError("convergence failed")),
    )
    push = HostStatusPush(host_id=host.id, boot_id=boot, node_health=_node_section(sequence=1))
    pending = await svc.begin_status_push(db_session, host, push)
    await db_session.commit()

    converged = await svc.process_prepublication(
        host_id=host.id,
        host_ip=host.ip,
        agent_port=host.agent_port,
        payload=pending.sections,
    )

    assert converged is False
    section = await _snapshot_section(db_session, host.id, "node_health")
    assert section is not None
    assert "observation_revision" not in section
    await db_session.refresh(host)
    assert host.observation_cursors.get("node_health") is None


async def test_older_pending_push_cannot_finalize_over_newer_snapshot(db_session: AsyncSession) -> None:
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="publication-superseded", boot_id=boot)
    svc = HostStatusPushService(publisher=Mock())

    older = await svc.begin_status_push(
        db_session,
        host,
        HostStatusPush(host_id=host.id, boot_id=boot, node_health=_node_section(sequence=11)),
    )
    await db_session.commit()
    newer = await svc.begin_status_push(
        db_session,
        host,
        HostStatusPush(host_id=host.id, boot_id=boot, node_health=_node_section(sequence=12)),
    )
    await db_session.commit()

    locked = await db_session.get(Host, host.id, with_for_update=True)
    assert locked is not None
    assert await svc.finalize_status_push(db_session, locked, older) is None
    await db_session.commit()

    current = await _snapshot_section(db_session, host.id, "node_health")
    assert current is not None
    assert current["section_sequence"] == 12
    assert "observation_revision" not in current

    locked = await db_session.get(Host, host.id, with_for_update=True)
    assert locked is not None
    assert await svc.finalize_status_push(db_session, locked, newer) is not None
    await db_session.commit()
    final = await _snapshot_section(db_session, host.id, "node_health")
    assert final is not None
    assert final["section_sequence"] == 12
    assert isinstance(final["observation_revision"], int)


async def test_concurrent_pushes_serialize_convergence_per_host(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """An older convergence pass cannot finish after a newer pass for one host."""
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="convergence-serialized", boot_id=boot)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def _controlled_convergence(
        *,
        host_id: uuid.UUID,
        host_ip: str,
        agent_port: int,
        payload: dict[str, Any],
    ) -> bool:
        del host_id, host_ip, agent_port
        section = payload["node_health"]
        sequence = section["section_sequence"]
        order.append(f"start:{sequence}")
        if sequence == 1:
            first_started.set()
            await release_first.wait()
        order.append(f"finish:{sequence}")
        return True

    svc = HostStatusPushService(
        publisher=Mock(),
        session_factory=db_session_maker,
        converge_host=_controlled_convergence,
    )
    host_services = SimpleNamespace(status_push=svc)
    pack_services = SimpleNamespace()

    async def _run_push(sequence: int) -> Response:
        async with db_session_maker() as session:
            return await status_endpoint(
                db=session,
                hosts=host_services,  # type: ignore[arg-type]
                packs=pack_services,  # type: ignore[arg-type]
                push=HostStatusPush(
                    host_id=host.id,
                    boot_id=boot,
                    node_health=_node_section(sequence=sequence),
                ),
            )

    older = asyncio.create_task(_run_push(1))
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    newer = asyncio.create_task(_run_push(2))
    await asyncio.sleep(0.05)
    serialized_before_release = order == ["start:1"]
    release_first.set()
    older_response, newer_response = await asyncio.gather(older, newer)

    assert serialized_before_release
    assert older_response.status_code == 204
    assert newer_response.status_code == 204
    assert order == ["start:1", "finish:1", "start:2", "finish:2"]


async def test_publication_slots_reserve_capacity_for_nested_convergence_sessions(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Only the configured number of Txn-B owners may hold pool connections."""
    first_boot = uuid.uuid4()
    second_boot = uuid.uuid4()
    first_host = await _make_host(db_session, hostname="publication-slot-1", boot_id=first_boot)
    second_host = await _make_host(db_session, hostname="publication-slot-2", boot_id=second_boot)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    order: list[uuid.UUID] = []

    async def _controlled_convergence(
        *,
        host_id: uuid.UUID,
        host_ip: str,
        agent_port: int,
        payload: dict[str, Any],
    ) -> bool:
        del host_ip, agent_port, payload
        order.append(host_id)
        if host_id == first_host.id:
            first_started.set()
            await release_first.wait()
        return True

    svc = HostStatusPushService(
        publisher=Mock(),
        session_factory=db_session_maker,
        converge_host=_controlled_convergence,
        publication_concurrency=1,
    )
    host_services = SimpleNamespace(status_push=svc)
    pack_services = SimpleNamespace()

    async def _run_push(host: Host, boot_id: uuid.UUID) -> Response:
        async with db_session_maker() as session:
            return await status_endpoint(
                db=session,
                hosts=host_services,  # type: ignore[arg-type]
                packs=pack_services,  # type: ignore[arg-type]
                push=HostStatusPush(
                    host_id=host.id,
                    boot_id=boot_id,
                    node_health=_node_section(sequence=1),
                ),
            )

    first = asyncio.create_task(_run_push(first_host, first_boot))
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    second = asyncio.create_task(_run_push(second_host, second_boot))
    await asyncio.sleep(0.05)
    second_waited_for_slot = order == [first_host.id]
    release_first.set()
    responses = await asyncio.gather(first, second)

    assert second_waited_for_slot
    assert all(response.status_code == 204 for response in responses)
    assert order == [first_host.id, second_host.id]


async def test_post_convergence_stamp_keeps_txn_a_revision_order(db_session: AsyncSession) -> None:
    """Publication is deferred, but guard ordering is still ingest-time.

    A synchronous restart/convergence writer between Txn A and Txn B must draw
    the higher revision so the later async fold cannot regress its health fact.
    """
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="publication-revision-order", boot_id=boot)
    svc = HostStatusPushService(publisher=Mock())
    pending = await svc.begin_status_push(
        db_session,
        host,
        HostStatusPush(host_id=host.id, boot_id=boot, node_health=_node_section(sequence=1)),
    )
    await db_session.commit()

    synchronous_racer_revision = await next_observation_revision(db_session)
    await db_session.commit()
    locked = await db_session.get(Host, host.id, with_for_update=True)
    assert locked is not None
    assert await svc.finalize_status_push(db_session, locked, pending) is not None
    await db_session.commit()

    section = await _snapshot_section(db_session, host.id, "node_health")
    assert section is not None
    assert section["observation_revision"] < synchronous_racer_revision


# --------------------------------------------------------------------------- #
# Token integrity
# --------------------------------------------------------------------------- #


async def test_hash_mismatch_rejected_before_liveness(client: AsyncClient, db_session: AsyncSession) -> None:
    boot = uuid.uuid4()
    host = await _make_host(db_session, hostname="hash-mismatch", boot_id=boot)
    section = _node_section(sequence=1)
    section["payload_sha256"] = "0" * 64

    response = await _post(client, host.id, boot_id=boot, node_health=section)

    assert response.status_code == 422
    await db_session.refresh(host)
    assert host.last_heartbeat is None
    assert host.observation_cursors == {}
    assert await _snapshot_section(db_session, host.id, "node_health") is None


def test_malformed_section_sequence_degrades_to_tokenless() -> None:
    boot = uuid.uuid4()
    for sequence in (True, -1):
        section = _node_section(sequence=1)
        section["section_sequence"] = sequence
        assert extract_token(section, boot_id=boot) is None


def test_default_publication_slots_require_nested_pool_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_push_module, "CONFIGURED_DB_POOL_CAPACITY", 1)

    with pytest.raises(RuntimeError, match="at least two database connections"):
        HostStatusPushService(publisher=Mock())
