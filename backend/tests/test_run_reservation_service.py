from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import run_reservation_service


class FakeSession:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


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


async def test_exclude_device_from_run_updates_entry_without_commit(monkeypatch) -> None:  # noqa: ANN001
    device_id = uuid.uuid4()
    run = SimpleNamespace(id=uuid.uuid4())
    entry = SimpleNamespace(excluded=False, exclusion_reason=None, excluded_at=None, excluded_until=None)
    monkeypatch.setattr(
        run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(run, entry)),
    )

    result = await run_reservation_service.exclude_device_from_run(FakeSession(), device_id, reason="bad", commit=False)

    assert result is run
    assert entry.excluded is True
    assert entry.exclusion_reason == "bad"
    assert entry.excluded_at is not None
    assert entry.excluded_until is None


async def test_exclude_device_from_run_noops_when_already_excluded_for_same_reason(monkeypatch) -> None:  # noqa: ANN001
    run = SimpleNamespace(id=uuid.uuid4())
    entry = SimpleNamespace(excluded=True, exclusion_reason="bad", excluded_until=None)
    db = FakeSession()
    monkeypatch.setattr(
        run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(run, entry)),
    )

    result = await run_reservation_service.exclude_device_from_run(db, uuid.uuid4(), reason="bad")

    assert result is run
    assert db.committed is False


async def test_restore_device_to_run_updates_excluded_entry(monkeypatch) -> None:  # noqa: ANN001
    run = SimpleNamespace(id=uuid.uuid4())
    refreshed = SimpleNamespace(id=run.id)
    entry = SimpleNamespace(
        excluded=True,
        exclusion_reason="bad",
        excluded_at=datetime.now(UTC),
        excluded_until=None,
    )
    db = FakeSession()
    monkeypatch.setattr(
        run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(run, entry)),
    )
    monkeypatch.setattr(run_reservation_service, "get_run", AsyncMock(return_value=refreshed))

    result = await run_reservation_service.restore_device_to_run(db, uuid.uuid4())

    assert result is refreshed
    assert db.committed is True
    assert entry.excluded is False
    assert entry.exclusion_reason is None
    assert entry.excluded_at is None


async def test_restore_device_to_run_noops_for_temporary_or_active_entries(monkeypatch) -> None:  # noqa: ANN001
    run = SimpleNamespace(id=uuid.uuid4())
    temporary = SimpleNamespace(excluded=True, excluded_until=datetime.now(UTC) + timedelta(minutes=5))
    monkeypatch.setattr(
        run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(run, temporary)),
    )
    assert await run_reservation_service.restore_device_to_run(FakeSession(), uuid.uuid4()) is run

    active = SimpleNamespace(excluded=False, excluded_until=None)
    run_reservation_service.get_device_reservation_with_entry.return_value = (run, active)
    assert await run_reservation_service.restore_device_to_run(FakeSession(), uuid.uuid4()) is run


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


async def test_exclude_and_restore_commit_refresh_paths(monkeypatch) -> None:  # noqa: ANN001
    run = SimpleNamespace(id=uuid.uuid4())
    refreshed = SimpleNamespace(id=run.id)
    entry = SimpleNamespace(excluded=False, exclusion_reason=None, excluded_at=None, excluded_until=None)
    db = FakeSession()
    monkeypatch.setattr(
        run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(run, entry)),
    )
    monkeypatch.setattr(run_reservation_service, "get_run", AsyncMock(return_value=refreshed))

    assert await run_reservation_service.exclude_device_from_run(db, uuid.uuid4(), reason="bad") is refreshed
    assert db.committed is True

    entry.excluded = True
    db.committed = False
    assert await run_reservation_service.restore_device_to_run(db, uuid.uuid4()) is refreshed
    assert db.committed is True
