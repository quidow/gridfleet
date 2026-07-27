from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, select
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceIntent
from app.devices.services import intent as intent_module
from app.devices.services.intent_types import CommandKind, release_rollout_intent_source
from app.packs.models import DriverPack, DriverPackRelease
from app.packs.services import release_rollout
from app.packs.services.release_rollout import run_release_rollout_stage
from app.packs.services.start_shim import selected_release_id
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.devices.services.intent_types import IntentRegistration
    from app.hosts.models import Host


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> _Result:
        return self


class _Session:
    """Fake session covering the bounded-inventory-read + per-candidate-commit shape.

    Three canned results are consumed, in order, by the stage's single
    inventory transaction: the device/node join, the stored release-rollout
    intents, and the batched ``DriverPack`` read. A fourth call raises
    ``StopIteration`` (surfaced as a ``RuntimeError`` from inside the
    coroutine), which is what would happen if a regression went back to
    issuing one release query per pack.
    """

    def __init__(self, results: list[_Result], timeline: list[str]) -> None:
        self._results = iter(results)
        self.timeline = timeline

    async def execute(self, _statement: object) -> _Result:
        return next(self._results)

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_Session]:
        yield self
        self.timeline.append("commit")


class _IntentService:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.registrations: list[tuple[uuid.UUID, IntentRegistration]] = []
        self.revocations: list[uuid.UUID] = []

    async def register_intents_and_reconcile(
        self, *, device_id: uuid.UUID, intents: list[IntentRegistration], **_: object
    ) -> None:
        self.timeline.append(f"register:{device_id}")
        self.registrations.append((device_id, intents[0]))

    async def revoke_intents_and_reconcile(self, *, device_id: uuid.UUID, **_: object) -> None:
        self.timeline.append(f"revoke:{device_id}")
        self.revocations.append(device_id)


async def test_stage_classifies_snapshot_via_one_bounded_inventory_read(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = {
        name: uuid.uuid4()
        for name in (
            "same",
            "changed",
            "converged",
            "stopped",
            "partial",
            "legacy",
            "no_node",
            "missing",
            "null_pack",
        )
    }
    intents = [
        DeviceIntent(
            device_id=device_id,
            source=release_rollout_intent_source(device_id),
            kind=CommandKind.release_rollout.value,
            payload={"target_release": "selected-a", "restart_requested_at": f"stamp-{name}"},
        )
        for name, device_id in ids.items()
    ]
    intents[1].payload["target_release"] = "previous-target"  # "changed" -> stamped for a different target

    # "pack-missing" is deliberately absent from the driver-pack result: a
    # device pointing at an unresolved pack must fall back to no target release
    # via a dict miss, not a second query.
    pack_a = DriverPack(
        id="pack-a",
        display_name="pack-a",
        current_release=None,
        releases=[DriverPackRelease(pack_id="pack-a", release="selected-a", manifest_json={})],
    )

    timeline: list[str] = []
    db = _Session(
        [
            _Result(
                [
                    (ids["same"], "pack-a", 11, "target-a", "old"),
                    (ids["changed"], "pack-a", 12, "target-b", "old"),
                    (ids["converged"], "pack-a", 13, "target-c", "selected-a"),
                    (ids["stopped"], "pack-a", None, "target-d", "old"),
                    (ids["partial"], "pack-a", 14, None, "old"),
                    (ids["legacy"], "pack-a", 15, "target-e", None),
                    (ids["no_node"], "pack-a", None, None, None),
                    (ids["missing"], "pack-missing", 16, "target-f", "old"),
                    (ids["null_pack"], None, 17, "target-g", "old"),
                ]
            ),
            _Result(intents),
            _Result([pack_a]),
        ],
        timeline,
    )
    service = _IntentService(timeline)
    monkeypatch.setattr(release_rollout, "IntentService", lambda _db: service)

    await run_release_rollout_stage(db, publisher=object())  # type: ignore[arg-type]

    # "same" is already stamped for the same target — finding 4 caps the
    # refresh so the TTL safety valve can expire; it is NOT re-registered.
    # "changed" is stamped for a different target — re-registered without a
    # stamp so the reconciler mints a fresh idle-safe stamp for the new release.
    assert [device_id for device_id, _ in service.registrations] == [ids["changed"]]
    assert service.registrations[0][1].payload == {"target_release": "selected-a"}
    assert set(service.revocations) == {
        ids["converged"],
        ids["stopped"],
        ids["partial"],
        ids["legacy"],
        ids["no_node"],
        ids["missing"],
        ids["null_pack"],
    }
    # One commit closes the bounded inventory read; one more per applied
    # candidate — never one shared commit for the whole batch.
    assert timeline.count("commit") == 1 + 8


async def _running_device(
    db: AsyncSession,
    host: Host,
    *,
    name: str,
    pack_id: str = "appium-xcuitest",
    observed_release: str | None = "0000.01.1",
    pid: int | None = 123,
    device_id: uuid.UUID | None = None,
) -> tuple[Device, AppiumNode]:
    overrides: dict[str, Any] = {"id": device_id} if device_id is not None else {}
    device = await create_device(
        db,
        host_id=host.id,
        name=name,
        pack_id=pack_id,
        platform_id="ios",
        **overrides,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=pid,
        active_connection_target=device.connection_target,
        observed_pack_release=observed_release,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db.add(node)
    await db.commit()
    return device, node


async def _intent(db: AsyncSession, device_id: uuid.UUID) -> DeviceIntent | None:
    return (
        await db.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device_id,
                DeviceIntent.source == release_rollout_intent_source(device_id),
            )
        )
    ).scalar_one_or_none()


async def _seed_intent(db: AsyncSession, device_id: uuid.UUID, *, target_release: str = "old") -> None:
    db.add(
        DeviceIntent(
            device_id=device_id,
            source=release_rollout_intent_source(device_id),
            kind=CommandKind.release_rollout.value,
            payload={"target_release": target_release},
        )
    )
    await db.commit()


async def test_candidates_apply_in_sorted_uuid_order_with_isolated_middle_failure(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three revoke candidates seeded in reverse UUID order; the middle one's
    reconcile fails after its intent delete is staged. The stage must sort
    candidates itself (not rely on scan order), apply low/high in one fresh
    Device transaction each, and roll the middle one back in full rather than
    leaving a partially-applied revoke."""
    low, mid, high = sorted(uuid.uuid4() for _ in range(3))
    for device_id, name in ((high, "rollout-high"), (mid, "rollout-mid"), (low, "rollout-low")):
        device, _ = await _running_device(db_session, db_host, name=name, pid=None, device_id=device_id)
        await _seed_intent(db_session, device.id)

    real_reconcile = intent_module.reconcile_locked_device

    async def _flaky_reconcile(db: AsyncSession, locked: Any, *, publisher: object) -> None:  # noqa: ANN401
        if locked.device.id == mid:
            raise RuntimeError("boom-mid")
        await real_reconcile(db, locked, publisher=publisher)

    monkeypatch.setattr(intent_module, "reconcile_locked_device", _flaky_reconcile)

    assert db_session.bind is not None
    engine = db_session.bind.sync_engine
    entries: list[tuple[str, object]] = []

    def _listener(
        conn: object, cursor: object, statement: str, parameters: object, context: object, executemany: bool
    ) -> None:
        entries.append((" ".join(statement.split()), parameters))

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        await run_release_rollout_stage(db_session, publisher=event_bus)
    finally:
        event.remove(engine, "before_cursor_execute", _listener)

    assert await _intent(db_session, low) is None
    assert await _intent(db_session, high) is None
    mid_intent = await _intent(db_session, mid)
    assert mid_intent is not None  # rolled back: the pre-seeded intent survives untouched
    assert mid_intent.payload == {"target_release": "old"}

    id_labels = {str(low): "low", str(mid): "mid", str(high): "high"}
    lock_order: list[str] = []
    for statement, parameters in entries:
        if "FROM devices" in statement and "FOR UPDATE" in statement:
            raw_values = parameters.values() if isinstance(parameters, dict) else (parameters or ())
            values = {str(value) for value in raw_values}
            matched = values & set(id_labels)
            if matched:
                lock_order.append(id_labels[next(iter(matched))])
    assert lock_order == ["low", "mid", "high"]


async def test_detector_registers_stale_node_and_refreshes_stamp(db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    device, _ = await _running_device(db_session, db_host, name="rollout-stale")

    await run_release_rollout_stage(db_session, publisher=event_bus)
    row = await _intent(db_session, device.id)
    assert row is not None
    assert row.payload["target_release"] != "0000.01.1"
    first_stamp = row.payload["restart_requested_at"]
    first_expiry = row.expires_at
    # The stage now opens its own transaction on entry; a session that still
    # holds an open implicit transaction from the read above would make that
    # `db.begin()` raise `InvalidRequestError`. Production never hits this —
    # each janitor cycle gets a fresh session — but this test drives two
    # cycles on one shared session, so it must close the read's transaction
    # itself.
    await db_session.commit()

    await run_release_rollout_stage(db_session, publisher=event_bus)
    row = await _intent(db_session, device.id)
    assert row is not None
    assert row.payload["restart_requested_at"] == first_stamp
    assert row.expires_at is not None and first_expiry is not None and row.expires_at >= first_expiry


async def test_detector_resets_stamp_when_target_changes(db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    device, _ = await _running_device(db_session, db_host, name="rollout-target-change")
    await run_release_rollout_stage(db_session, publisher=event_bus)
    row = await _intent(db_session, device.id)
    assert row is not None
    old_stamp = row.payload["restart_requested_at"]

    pack = (
        (
            await db_session.execute(
                select(DriverPack).where(DriverPack.id == device.pack_id).options(selectinload(DriverPack.releases))
            )
        ).scalar_one()
        if device.pack_id is not None
        else None
    )
    assert pack is not None
    manifest = pack.releases[0].manifest_json
    pack.current_release = "9999.01.1"
    pack.releases.append(DriverPackRelease(pack_id=pack.id, release=pack.current_release, manifest_json=manifest))
    await db_session.commit()

    await run_release_rollout_stage(db_session, publisher=event_bus)
    row = await _intent(db_session, device.id)
    assert row is not None
    assert row.payload["target_release"] == "9999.01.1"
    assert row.payload["restart_requested_at"] != old_stamp


async def test_detector_revokes_converged_intent(db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    device, node = await _running_device(db_session, db_host, name="rollout-converged")
    await run_release_rollout_stage(db_session, publisher=event_bus)
    assert await _intent(db_session, device.id) is not None

    node.observed_pack_release = await selected_release_id(db_session, device.pack_id)
    await db_session.commit()
    await run_release_rollout_stage(db_session, publisher=event_bus)

    assert await _intent(db_session, device.id) is None


async def test_detector_cleans_stopped_legacy_and_unselected_intents(db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    stopped, _ = await _running_device(db_session, db_host, name="rollout-stopped", pid=None)
    partial, partial_node = await _running_device(db_session, db_host, name="rollout-partial")
    partial_node.active_connection_target = None
    await db_session.commit()
    legacy, _ = await _running_device(db_session, db_host, name="rollout-legacy", observed_release=None)
    unselected = await create_device(
        db_session,
        host_id=db_host.id,
        name="rollout-unselected",
        pack_id="missing-pack",
        platform_id="unknown",
    )
    for device in (stopped, partial, legacy, unselected):
        await _seed_intent(db_session, device.id)

    await run_release_rollout_stage(db_session, publisher=event_bus)

    assert [await _intent(db_session, device.id) for device in (stopped, partial, legacy, unselected)] == [
        None,
        None,
        None,
        None,
    ]
