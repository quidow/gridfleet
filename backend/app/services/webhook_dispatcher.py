from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from tenacity import RetryCallState, Retrying
from tenacity.wait import wait_exponential_jitter

from app.metrics import record_webhook_delivery
from app.models.system_event import SystemEvent
from app.models.webhook import Webhook
from app.models.webhook_delivery import WebhookDelivery
from app.observability import get_logger, observe_background_loop

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.services.event_bus import Event

logger = get_logger(__name__)


_RETRY_WAITER = wait_exponential_jitter(initial=1, exp_base=4, jitter=2, max=64)


def _compute_retry_delay(attempt_number: int) -> float:
    state = RetryCallState(retry_object=Retrying(), fn=None, args=(), kwargs={})
    state.attempt_number = attempt_number
    return float(_RETRY_WAITER(state))


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, httpx.NetworkError | httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


MAX_RETRIES = 3
DELIVERY_TIMEOUT_SEC = 10
CLAIM_LEASE_SEC = 30
POLL_INTERVAL_SEC = 1
LOOP_NAME = "webhook_delivery"

_session_factory: async_sessionmaker[AsyncSession] | None = None


def utcnow() -> datetime:
    return datetime.now(UTC)


def configure(session_factory: async_sessionmaker[AsyncSession]) -> None:
    global _session_factory
    _session_factory = session_factory


async def handle_system_event(event: Event) -> None:
    if _session_factory is None:
        return

    async with _session_factory() as db:
        row_result = await db.execute(select(SystemEvent).where(SystemEvent.event_id == event.id))
        system_event = row_result.scalar_one_or_none()
        if system_event is None:
            return

        webhook_result = await db.execute(select(Webhook).where(Webhook.enabled.is_(True)).order_by(Webhook.name.asc()))
        webhooks = [webhook for webhook in webhook_result.scalars().all() if event.type in webhook.event_types]
        if not webhooks:
            return

        stmt = insert(WebhookDelivery).values(
            [
                {
                    "webhook_id": webhook.id,
                    "system_event_id": system_event.id,
                    "event_type": system_event.type,
                    "status": "pending",
                    "attempts": 0,
                    "max_attempts": MAX_RETRIES,
                    "next_retry_at": utcnow(),
                }
                for webhook in webhooks
            ]
        )
        returning_stmt = stmt.on_conflict_do_nothing(constraint="uq_webhook_deliveries_webhook_system_event").returning(
            WebhookDelivery.id
        )
        result = await db.execute(returning_stmt)
        await db.commit()
        record_webhook_delivery("pending", len(result.scalars().all()))


async def list_deliveries(
    db: AsyncSession,
    webhook_id: uuid.UUID,
    *,
    limit: int = 10,
) -> tuple[list[WebhookDelivery], int]:
    items_result = await db.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.webhook_id == webhook_id)
        .order_by(
            WebhookDelivery.created_at.desc(),
            WebhookDelivery.system_event_id.desc(),
            WebhookDelivery.id.desc(),
        )
        .limit(limit)
    )
    total_result = await db.execute(
        select(func.count()).select_from(WebhookDelivery).where(WebhookDelivery.webhook_id == webhook_id)
    )
    return list(items_result.scalars().all()), int(total_result.scalar_one())


async def retry_delivery(
    db: AsyncSession,
    webhook_id: uuid.UUID,
    delivery_id: uuid.UUID,
) -> WebhookDelivery | None:
    delivery = await db.get(WebhookDelivery, delivery_id)
    if delivery is None or delivery.webhook_id != webhook_id:
        return None

    delivery.status = "pending"
    delivery.attempts = 0
    delivery.last_attempt_at = None
    delivery.next_retry_at = utcnow()
    delivery.last_error = None
    delivery.last_http_status = None
    await db.commit()
    await db.refresh(delivery)
    return delivery


async def claim_next_delivery(
    session_factory: async_sessionmaker[AsyncSession],
) -> WebhookDelivery | None:
    async with session_factory() as db:
        stmt = (
            select(WebhookDelivery)
            .where(
                WebhookDelivery.status.in_(("pending", "failed")),
                or_(WebhookDelivery.next_retry_at.is_(None), WebhookDelivery.next_retry_at <= utcnow()),
            )
            .order_by(WebhookDelivery.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        result = await db.execute(stmt)
        delivery = result.scalar_one_or_none()
        if delivery is None:
            await db.rollback()
            return None

        delivery.next_retry_at = utcnow() + timedelta(seconds=CLAIM_LEASE_SEC)
        await db.commit()
        await db.refresh(delivery)
        return delivery


async def run_pending_webhook_deliveries_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    delivery = await claim_next_delivery(session_factory)
    if delivery is None:
        return False

    if client is not None:
        await _process_delivery(delivery.id, session_factory, client)
        return True

    async with httpx.AsyncClient() as owned_client:
        await _process_delivery(delivery.id, session_factory, owned_client)
    return True


async def webhook_delivery_loop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with httpx.AsyncClient() as client:
        while True:
            try:
                async with observe_background_loop(LOOP_NAME, float(POLL_INTERVAL_SEC)).cycle():
                    worked = await run_pending_webhook_deliveries_once(session_factory, client=client)
                if not worked:
                    await asyncio.sleep(POLL_INTERVAL_SEC)
            except Exception:
                logger.exception("Webhook dispatcher error")
                await asyncio.sleep(POLL_INTERVAL_SEC)


async def _process_delivery(
    delivery_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
) -> None:
    async with session_factory() as db:
        delivery = await db.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return
        webhook = await db.get(Webhook, delivery.webhook_id)
        system_event = await db.get(SystemEvent, delivery.system_event_id)
        if webhook is None or system_event is None or not webhook.enabled:
            delivery.status = "exhausted"
            delivery.next_retry_at = None
            delivery.last_error = "Webhook or event record is no longer available"
            delivery.last_attempt_at = utcnow()
            await db.commit()
            return

        payload = system_event.to_dict()

    try:
        response = await client.post(webhook.url, json=payload, timeout=DELIVERY_TIMEOUT_SEC)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        await _record_failure(
            delivery_id,
            session_factory,
            error=str(exc),
            http_status=exc.response.status_code,
            retryable=_is_retryable_exception(exc),
        )
        return
    except (httpx.NetworkError, httpx.TimeoutException) as exc:
        await _record_failure(
            delivery_id,
            session_factory,
            error=str(exc),
            http_status=None,
            retryable=True,
        )
        return
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        await _record_failure(
            delivery_id,
            session_factory,
            error=str(exc),
            http_status=None,
            retryable=False,
        )
        return

    async with session_factory() as db:
        delivery = await db.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return
        delivery.attempts += 1
        delivery.status = "delivered"
        delivery.last_attempt_at = utcnow()
        delivery.next_retry_at = None
        delivery.last_error = None
        delivery.last_http_status = response.status_code
        await db.commit()
    record_webhook_delivery("delivered")


async def _record_failure(
    delivery_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    error: str,
    http_status: int | None,
    retryable: bool,
) -> None:
    async with session_factory() as db:
        delivery = await db.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return
        delivery.attempts += 1
        delivery.last_attempt_at = utcnow()
        delivery.last_error = error
        delivery.last_http_status = http_status

        if not retryable or delivery.attempts >= delivery.max_attempts:
            delivery.status = "exhausted"
            delivery.next_retry_at = None
            metric_status = "exhausted"
        else:
            delivery.status = "failed"
            delay = _compute_retry_delay(delivery.attempts)
            delivery.next_retry_at = utcnow() + timedelta(seconds=delay)
            metric_status = "failed"
        await db.commit()
    record_webhook_delivery(metric_status)
