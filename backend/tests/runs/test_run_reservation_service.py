from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from app.devices.models import ExclusionKind
from app.runs import service_reservation as run_reservation_service
from app.runs.service_reservation import RunReservationService
from tests.fakes import build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import pytest


def _fake_locked(device_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(device=SimpleNamespace(id=device_id or uuid.uuid4()), assert_active=lambda db: None)


def test_reservation_entry_lookup_and_exclusion_helpers() -> None:
    device_id = uuid.uuid4()
    other_id = uuid.uuid4()
    released = SimpleNamespace(device_id=device_id, released_at=datetime.now(UTC))
    active = SimpleNamespace(device_id=device_id, released_at=None)
    run = SimpleNamespace(device_reservations=[SimpleNamespace(device_id=other_id, released_at=None), released, active])

    assert run_reservation_service.get_reservation_entry_for_device(run, device_id) is active
    assert (
        run_reservation_service.get_reservation_entry_for_device(SimpleNamespace(device_reservations=[]), device_id)
        is None
    )
    assert run_reservation_service.reservation_entry_is_excluded(None) is False
    assert (
        run_reservation_service.reservation_entry_is_excluded(SimpleNamespace(excluded=False, excluded_until=None))
        is False
    )
    assert (
        run_reservation_service.reservation_entry_is_excluded(SimpleNamespace(excluded=True, excluded_until=None))
        is True
    )
    assert (
        run_reservation_service.reservation_entry_is_excluded(
            SimpleNamespace(excluded=True, excluded_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        is False
    )


async def test_exclude_device_from_run_updates_entry_transaction_local(monkeypatch: pytest.MonkeyPatch) -> None:
    device_id = uuid.uuid4()
    run = SimpleNamespace(id=uuid.uuid4())
    entry = SimpleNamespace(
        id=uuid.uuid4(), excluded=False, exclusion_reason=None, excluded_at=None, excluded_until=None
    )
    monkeypatch.setattr(
        run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(run, entry)),
    )
    monkeypatch.setattr(
        run_reservation_service,
        "_lock_active_reservation_entry_by_id",
        AsyncMock(return_value=entry),
    )

    svc = RunReservationService(review=build_review_service())
    result = await svc.exclude_device_from_run(AsyncMock(), device_id, reason="bad")

    assert result is run
    assert entry.excluded is True
    assert entry.exclusion_reason == "bad"
    assert entry.excluded_at is not None
    assert entry.excluded_until is None
    assert entry.exclusion_kind is ExclusionKind.exclusion


async def test_exclude_device_from_run_noops_when_already_excluded_for_same_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id=uuid.uuid4())
    entry = SimpleNamespace(excluded=True, exclusion_reason="bad", excluded_until=None)
    monkeypatch.setattr(
        run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(run, entry)),
    )

    svc = RunReservationService(review=build_review_service())
    result = await svc.exclude_device_from_run(AsyncMock(), uuid.uuid4(), reason="bad")

    assert result is run


async def test_restore_device_to_run_clears_exclusion_transaction_local(monkeypatch: pytest.MonkeyPatch) -> None:
    device_id = uuid.uuid4()
    run = SimpleNamespace(id=uuid.uuid4())
    entry = SimpleNamespace(
        id=uuid.uuid4(),
        excluded=True,
        exclusion_kind=ExclusionKind.exclusion,
        exclusion_reason="bad",
        excluded_at=datetime.now(UTC),
        excluded_until=None,
        cooldown_count=2,
    )
    monkeypatch.setattr(
        run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(run, entry)),
    )
    monkeypatch.setattr(
        run_reservation_service,
        "_lock_active_reservation_entry_by_id",
        AsyncMock(return_value=entry),
    )
    # No `.execute` on the stub session -> AttributeError path -> device is None,
    # exercising the "reservation cleared, no device to un-review" branch.
    svc = RunReservationService(review=build_review_service())
    result = await svc.restore_device_to_run(SimpleNamespace(), device_id)

    assert result is run
    assert entry.excluded is False
    assert entry.exclusion_kind is None
    assert entry.cooldown_count == 0


async def test_restore_device_to_run_noops_for_temporary_or_active_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    run = SimpleNamespace(id=uuid.uuid4())
    temporary = SimpleNamespace(excluded=True, excluded_until=datetime.now(UTC) + timedelta(minutes=5))
    monkeypatch.setattr(
        run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(run, temporary)),
    )
    svc = RunReservationService(review=build_review_service())
    assert await svc.restore_device_to_run(AsyncMock(), uuid.uuid4()) is run

    active = SimpleNamespace(excluded=False, excluded_until=None)
    run_reservation_service.get_device_reservation_with_entry.return_value = (run, active)
    assert await svc.restore_device_to_run(AsyncMock(), uuid.uuid4()) is run


async def test_exclude_locked_sets_fields_and_returns_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid.uuid4()
    entry = SimpleNamespace(
        run_id=run_id, excluded=False, exclusion_kind=None, exclusion_reason=None, excluded_at=None, excluded_until=None
    )
    monkeypatch.setattr(run_reservation_service, "lock_active_reservation", AsyncMock(return_value=entry))
    db = AsyncMock()
    svc = RunReservationService(review=build_review_service())

    assert await svc.exclude_locked(db, _fake_locked(), reason="bad") == run_id
    assert entry.excluded is True
    assert entry.exclusion_kind is ExclusionKind.exclusion
    assert entry.exclusion_reason == "bad"
    assert entry.excluded_until is None
    db.flush.assert_awaited()


async def test_locked_helpers_return_none_when_reservation_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_reservation_service, "lock_active_reservation", AsyncMock(return_value=None))
    db = AsyncMock()
    svc = RunReservationService(review=build_review_service())

    assert await svc.exclude_locked(db, _fake_locked(), reason="bad") is None
    assert await svc.restore_locked(db, _fake_locked()) is None
    assert await svc.release_locked(db, _fake_locked(), reason="gone", publisher=event_bus) is None


async def test_restore_locked_clears_fields_and_calls_review(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid.uuid4()
    entry = SimpleNamespace(
        run_id=run_id,
        excluded=True,
        exclusion_kind=ExclusionKind.exclusion,
        exclusion_reason="bad",
        excluded_at=datetime.now(UTC),
        excluded_until=None,
        cooldown_count=3,
    )
    monkeypatch.setattr(run_reservation_service, "lock_active_reservation", AsyncMock(return_value=entry))
    review = AsyncMock()
    db = AsyncMock()
    svc = RunReservationService(review=review)

    assert await svc.restore_locked(db, _fake_locked()) == run_id
    assert entry.excluded is False
    assert entry.exclusion_kind is None
    assert entry.cooldown_count == 0
    review.clear_review_required.assert_awaited()


async def test_release_locked_marks_released_and_reconciles(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid.uuid4()
    entry = SimpleNamespace(
        run_id=run_id,
        released_at=None,
        excluded=True,
        exclusion_kind=ExclusionKind.exclusion,
        exclusion_reason="old",
        excluded_at=datetime.now(UTC),
        excluded_until=None,
    )
    monkeypatch.setattr(run_reservation_service, "lock_active_reservation", AsyncMock(return_value=entry))
    reconcile = AsyncMock()
    monkeypatch.setattr(run_reservation_service, "reconcile_locked_device", reconcile)
    db = AsyncMock()
    svc = RunReservationService(review=build_review_service())

    assert await svc.release_locked(db, _fake_locked(), reason="health", publisher=event_bus) == run_id
    assert entry.released_at is not None
    assert entry.exclusion_reason == "health"
    assert entry.excluded is False
    assert entry.exclusion_kind is None
    reconcile.assert_awaited()


async def test_get_run_and_device_reservation_query_result_shapes() -> None:
    run = SimpleNamespace(id=uuid.uuid4())
    reservation = SimpleNamespace(run=run)

    class ScalarResult:
        def scalar_one_or_none(self) -> object:
            return run

        def scalars(self) -> ScalarResult:
            return self

        def first(self) -> object:
            return reservation

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, *_args: object, **_kwargs: object) -> ScalarResult:
            self.calls += 1
            return ScalarResult()

    db = Session()
    assert await run_reservation_service.get_run(db, run.id) is run  # type: ignore[arg-type]
    assert await run_reservation_service.get_device_reservation_with_entry(db, uuid.uuid4()) == (run, reservation)  # type: ignore[arg-type]
