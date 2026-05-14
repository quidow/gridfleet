import httpx
from fastapi import APIRouter

from app.devices.routers import (
    control as devices_control,
)
from app.devices.routers import (
    core as devices_core,
)
from app.devices.routers import (
    test_data as devices_test_data,
)
from app.devices.routers import (
    verification as devices_verification,
)
from app.devices.services import lifecycle_policy
from app.devices.services import service as device_service
from app.services import session_viability

router = APIRouter()

router.include_router(devices_verification.router, prefix="/api/devices", tags=["devices"])
router.include_router(devices_core.router, prefix="/api/devices", tags=["devices"])
router.include_router(devices_control.router, prefix="/api/devices", tags=["devices"])
router.include_router(devices_test_data.router, prefix="/api/devices", tags=["devices"])

__all__ = ["device_service", "devices_test_data", "httpx", "lifecycle_policy", "router", "session_viability"]
