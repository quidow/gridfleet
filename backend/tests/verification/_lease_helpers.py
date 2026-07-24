from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    VERIFICATION_OPERATION_ID_KEY,
    CommandKind,
    IntentRegistration,
    verification_intent_source,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.devices.models import Device
    from app.events.protocols import EventPublisher


async def register_verification_node_intent(
    db: AsyncSession,
    device: Device,
    *,
    settings: SettingsReader,
    publisher: EventPublisher,
    operation_id: uuid.UUID | None = None,
) -> None:
    """Test helper: seed a standing verification-start intent (verification lease).

    Relocated verbatim from the former production `_register_verification_node_intent`,
    which lost all production callers after the Phase 4 verification refactor.
    """
    startup_timeout = settings.get_int("appium.startup_timeout_sec")
    viability_timeout = settings.get_int("general.session_viability_timeout_sec")
    deadline = now_utc() + timedelta(seconds=startup_timeout + viability_timeout + 60)
    payload: dict[str, Any] = {"action": "start"}
    if operation_id is not None:
        payload[VERIFICATION_OPERATION_ID_KEY] = str(operation_id)
    intent_service = IntentService(db)
    await device_locking.lock_device(db, device.id)
    await intent_service.register_intents_and_reconcile(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=verification_intent_source(device.id),
                kind=CommandKind.verification_start,
                payload=payload,
                expires_at=deadline,
            )
        ],
        publisher=publisher,
    )
