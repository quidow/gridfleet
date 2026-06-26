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

router = APIRouter()

router.include_router(devices_core.router, prefix="/api/devices", tags=["devices"])
router.include_router(devices_control.router, prefix="/api/devices", tags=["devices"])
router.include_router(devices_test_data.router, prefix="/api/devices", tags=["devices"])

__all__ = ["router"]
