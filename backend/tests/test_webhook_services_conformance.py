from app.webhooks.dispatcher import WebhookDispatchService
from app.webhooks.protocols import WebhookCrudProtocol, WebhookDispatchProtocol
from app.webhooks.service import WebhookCrudService


def test_webhook_services_satisfy_protocols() -> None:
    assert isinstance(WebhookCrudService.__new__(WebhookCrudService), WebhookCrudProtocol)
    assert isinstance(WebhookDispatchService.__new__(WebhookDispatchService), WebhookDispatchProtocol)
