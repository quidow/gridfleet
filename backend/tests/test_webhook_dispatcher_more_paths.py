import uuid
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
    await webhook_dispatcher._record_failure(uuid.uuid4(), _Factory(db), error="down", http_status=None)
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
    await webhook_dispatcher._record_failure(uuid.uuid4(), _Factory(_Db(exhausted)), error="down", http_status=503)
    assert exhausted.status == "exhausted"
    assert exhausted.next_retry_at is None
    metric.assert_called_with("exhausted")
