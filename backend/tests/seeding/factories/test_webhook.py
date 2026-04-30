import uuid

from app.seeding.context import SeedContext
from app.seeding.factories.webhook import make_webhook, make_webhook_delivery


def test_make_webhook_defaults_enabled() -> None:
    ctx = SeedContext.build(session=None, seed=1)  # type: ignore[arg-type]
    hook = make_webhook(ctx, name="slack_alerts", url="https://example.com", event_types=["run.failed"])
    assert hook.enabled is True


def test_make_webhook_delivery_retrying_has_next_retry_at() -> None:
    ctx = SeedContext.build(session=None, seed=1)  # type: ignore[arg-type]
    delivery = make_webhook_delivery(
        ctx,
        webhook_id=uuid.uuid4(),
        system_event_id=100,
        event_type="run.failed",
        status="retrying",
        attempts=2,
        max_attempts=5,
        last_http_status=502,
    )
    assert delivery.next_retry_at is not None
    assert delivery.status == "retrying"
