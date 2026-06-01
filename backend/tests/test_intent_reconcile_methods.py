import inspect

from app.devices.services.intent import IntentService


def test_intent_service_has_reconcile_methods() -> None:
    assert inspect.iscoroutinefunction(IntentService.register_intents_and_reconcile)
    assert inspect.iscoroutinefunction(IntentService.revoke_intents_and_reconcile)
    reg = inspect.signature(IntentService.register_intents_and_reconcile)
    assert list(reg.parameters) == ["self", "device_id", "intents", "reason"]
    rev = inspect.signature(IntentService.revoke_intents_and_reconcile)
    assert list(rev.parameters) == ["self", "device_id", "sources", "reason", "publisher"]
