import httpx
from fastapi import APIRouter

from app.routers import devices_control, devices_core, devices_test_data, devices_verification
from app.services import device_service, lifecycle_policy, session_viability

router = APIRouter()

router.include_router(devices_verification.router, prefix="/api/devices", tags=["devices"])
router.include_router(devices_core.router, prefix="/api/devices", tags=["devices"])
router.include_router(devices_control.router, prefix="/api/devices", tags=["devices"])
router.include_router(devices_test_data.router, prefix="/api/devices", tags=["devices"])

__all__ = ["device_service", "devices_test_data", "httpx", "lifecycle_policy", "router", "session_viability"]
