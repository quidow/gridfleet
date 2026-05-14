import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services import webhook_dispatcher


class _Db:
    def __init__(self, *values: object) -> None:
        self.values = list(values)
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.refresh = AsyncMock()
        self.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))

    async def __aenter__(self) -> "_Db":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(self, model: object, key: object) -> object | None:
        if not self.values:
            return None
        return self.values.pop(0)


class _Factory:
    def __init__(self, *sessions: _Db) -> None:
        self.sessions = list(sessions)

    def __call__(self) -> _Db:
        return self.sessions.pop(0)


async def test_claim_retry_and_pending_no_work_paths() -> None:
    db = _Db()
    factory = _Factory(db)
    assert await webhook_dispatcher.claim_next_delivery(factory) is None
    db.rollback.assert_awaited_once()

    assert await webhook_dispatcher.run_pending_webhook_deliveries_once(_Factory(_Db())) is False

    wrong_webhook = uuid.uuid4()
    delivery = SimpleNamespace(webhook_id=uuid.uuid4())
    db = _Db(delivery)
    assert await webhook_dispatcher.retry_delivery(db, wrong_webhook, uuid.uuid4()) is None

    retry_id = uuid.uuid4()
    reset_delivery = SimpleNamespace(
        webhook_id=wrong_webhook,
        status="failed",
        attempts=2,
        last_attempt_at="old",
        next_retry_at=None,
        last_error="down",
        last_http_status=500,
    )
    db = _Db(reset_delivery)
    assert await webhook_dispatcher.retry_delivery(db, wrong_webhook, retry_id) is reset_delivery
    assert reset_delivery.status == "pending"
    assert reset_delivery.attempts == 0
    db.refresh.assert_awaited_once_with(reset_delivery)


async def test_list_deliveries_counts_rows() -> None:
    first = SimpleNamespace(id=uuid.uuid4())
    second = SimpleNamespace(id=uuid.uuid4())

    class ItemsResult:
        def scalars(self) -> "ItemsResult":
            return self

        def all(self) -> list[object]:
            return [first, second]

    class CountResult:
        def scalar_one(self) -> int:
            return 2

    db = _Db()
    db.execute = AsyncMock(side_effect=[ItemsResult(), CountResult()])

    items, total = await webhook_dispatcher.list_deliveries(db, uuid.uuid4())

    assert items == [first, second]
    assert total == 2


async def test_handle_system_event_no_factory_and_missing_event_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_dispatcher, "_session_factory", None)
    await webhook_dispatcher.handle_system_event(SimpleNamespace(id=uuid.uuid4(), type="device.created"))

    db = _Db()
    monkeypatch.setattr(webhook_dispatcher, "_session_factory", _Factory(db))
    await webhook_dispatcher.handle_system_event(SimpleNamespace(id=uuid.uuid4(), type="device.created"))
    db.execute.assert_awaited_once()


async def test_run_pending_delivery_uses_owned_client(monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(webhook_dispatcher, "claim_next_delivery", AsyncMock(return_value=delivery))
    process = AsyncMock()
    monkeypatch.setattr(webhook_dispatcher, "_process_delivery", process)

    assert await webhook_dispatcher.run_pending_webhook_deliveries_once(_Factory(_Db())) is True
    process.assert_awaited_once()


async def test_webhook_delivery_loop_logs_and_sleeps_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Observation:
        @asynccontextmanager
        async def cycle(self):  # noqa: ANN202
            yield None

    class Client:
        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(webhook_dispatcher.httpx, "AsyncClient", Client)
    monkeypatch.setattr(webhook_dispatcher, "observe_background_loop", MagicMock(return_value=Observation()))
    monkeypatch.setattr(
        webhook_dispatcher,
        "run_pending_webhook_deliveries_once",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr(webhook_dispatcher.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    log_exception = MagicMock()
    monkeypatch.setattr(webhook_dispatcher.logger, "exception", log_exception)

    with pytest.raises(asyncio.CancelledError):
        await webhook_dispatcher.webhook_delivery_loop(_Factory(_Db()))

    log_exception.assert_called_once_with("Webhook dispatcher error")


async def test_webhook_delivery_loop_sleeps_when_no_work(monkeypatch: pytest.MonkeyPatch) -> None:
    class Observation:
        @asynccontextmanager
        async def cycle(self):  # noqa: ANN202
            yield None

    class Client:
        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(webhook_dispatcher.httpx, "AsyncClient", Client)
    monkeypatch.setattr(webhook_dispatcher, "observe_background_loop", MagicMock(return_value=Observation()))
    monkeypatch.setattr(webhook_dispatcher, "run_pending_webhook_deliveries_once", AsyncMock(return_value=False))
    monkeypatch.setattr(webhook_dispatcher.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await webhook_dispatcher.webhook_delivery_loop(_Factory(_Db()))


async def test_process_delivery_missing_records_marks_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = SimpleNamespace(
        id=uuid.uuid4(),
        webhook_id=uuid.uuid4(),
        system_event_id=uuid.uuid4(),
        status="pending",
        next_retry_at="lease",
        last_error=None,
        last_attempt_at=None,
    )
    db = _Db(delivery, None, None)

    await webhook_dispatcher._process_delivery(delivery.id, _Factory(db), MagicMock())

    assert delivery.status == "exhausted"
    assert delivery.next_retry_at is None
    assert delivery.last_error == "Webhook or event record is no longer available"
    db.commit.assert_awaited_once()


async def test_process_delivery_success_and_failure_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    delivery_id = uuid.uuid4()
    webhook_id = uuid.uuid4()
    event_id = uuid.uuid4()
    delivery = SimpleNamespace(id=delivery_id, webhook_id=webhook_id, system_event_id=event_id)
    webhook = SimpleNamespace(id=webhook_id, url="https://example.test/hook", enabled=True)
    system_event = SimpleNamespace(to_dict=lambda: {"type": "device.created"})
    final_delivery = SimpleNamespace(
        id=delivery_id,
        attempts=0,
        status="pending",
        last_attempt_at=None,
        next_retry_at="lease",
        last_error="old",
        last_http_status=None,
    )
    response = httpx.Response(200, request=httpx.Request("POST", webhook.url))
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    metric = MagicMock()
    monkeypatch.setattr(webhook_dispatcher, "record_webhook_delivery", metric)

    await webhook_dispatcher._process_delivery(
        delivery_id,
        _Factory(_Db(delivery, webhook, system_event), _Db(final_delivery)),
        client,
    )

    assert final_delivery.status == "delivered"
    assert final_delivery.attempts == 1
    assert final_delivery.last_http_status == 200
    metric.assert_called_with("delivered")

    failure = AsyncMock()
    monkeypatch.setattr(webhook_dispatcher, "_record_failure", failure)
    bad_response = httpx.Response(500, request=httpx.Request("POST", webhook.url))
    status_client = SimpleNamespace(post=AsyncMock(return_value=bad_response))
    await webhook_dispatcher._process_delivery(
        delivery_id,
        _Factory(_Db(delivery, webhook, system_event)),
        status_client,
    )
    assert failure.await_args.kwargs["http_status"] == 500

    transport_client = SimpleNamespace(post=AsyncMock(side_effect=httpx.ConnectError("down")))
    await webhook_dispatcher._process_delivery(
        delivery_id,
        _Factory(_Db(delivery, webhook, system_event)),
        transport_client,
    )
    assert failure.await_args.kwargs["http_status"] is None

    await webhook_dispatcher._process_delivery(
        delivery_id,
        _Factory(_Db(delivery, webhook, system_event), _Db()),
        client,
    )


async def test_handle_system_event_no_matching_webhooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 77: system_event found but no enabled webhook subscribes to the event type."""

    class _ScalarResult:
        def scalar_one_or_none(self) -> SimpleNamespace:
            return SimpleNamespace(id=uuid.uuid4(), type="device.created")

        def scalars(self) -> "_ScalarResult":
            return self

        def all(self) -> list[object]:
            # Return a webhook that does NOT subscribe to "device.created"
            return [SimpleNamespace(id=uuid.uuid4(), enabled=True, event_types=["webhook.test"])]

    call_count = 0

    async def _execute(_stmt: object) -> _ScalarResult:
        nonlocal call_count
        call_count += 1
        return _ScalarResult()

    db = _Db()
    db.execute = AsyncMock(side_effect=_execute)
    monkeypatch.setattr(webhook_dispatcher, "_session_factory", _Factory(db))
    await webhook_dispatcher.handle_system_event(SimpleNamespace(id=uuid.uuid4(), type="device.created"))
    # Two executes: one for SystemEvent lookup, one for Webhook list
    assert call_count == 2
    db.commit.assert_not_awaited()


async def test_process_delivery_delivery_gone_returns_early() -> None:
    """Line 210: delivery row is None at the start of _process_delivery — silent early exit."""
    db = _Db()  # no values → db.get returns None on first call
    await webhook_dispatcher._process_delivery(uuid.uuid4(), _Factory(db), MagicMock())
    db.commit.assert_not_awaited()


async def test_record_failure_delivery_gone_returns_early() -> None:
    """Line 279: delivery row is None inside _record_failure — silent early exit."""
    db = _Db()  # no values → db.get returns None
    await webhook_dispatcher._record_failure(uuid.uuid4(), _Factory(db), error="gone", http_status=None, retryable=True)
    db.commit.assert_not_awaited()


async def test_record_failure_failed_and_exhausted_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    metric = MagicMock()
    monkeypatch.setattr(webhook_dispatcher, "record_webhook_delivery", metric)
    delivery = SimpleNamespace(
        attempts=0,
        max_attempts=2,
        status="pending",
        last_attempt_at=None,
        last_error=None,
        last_http_status=None,
        next_retry_at=None,
    )
    db = _Db(delivery)
    await webhook_dispatcher._record_failure(uuid.uuid4(), _Factory(db), error="down", http_status=None, retryable=True)
    assert delivery.status == "failed"
    assert delivery.next_retry_at is not None
    metric.assert_called_with("failed")

    exhausted = SimpleNamespace(
        attempts=1,
        max_attempts=2,
        status="pending",
        last_attempt_at=None,
        last_error=None,
        last_http_status=None,
        next_retry_at=None,
    )
    await webhook_dispatcher._record_failure(
        uuid.uuid4(), _Factory(_Db(exhausted)), error="down", http_status=503, retryable=True
    )
    assert exhausted.status == "exhausted"
    assert exhausted.next_retry_at is None
    metric.assert_called_with("exhausted")
