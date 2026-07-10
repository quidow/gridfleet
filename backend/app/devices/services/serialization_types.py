from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.devices.models import DeviceOperationalState
    from app.devices.services.readiness import DeviceReadiness


@dataclass(frozen=True)
class DeviceSerializationContext:
    """Per-device values precomputed in batch by
    ``DevicePresenterService.build_serialization_contexts`` so ``serialize_device``
    can skip its per-device pack-catalog queries.

    Lives in this leaf module (rather than ``presenter``) so ``protocols`` can
    reference the type without importing ``presenter`` and forming an import cycle.
    """

    readiness: DeviceReadiness
    blocked_reason: str | None
    operational_state: DeviceOperationalState
