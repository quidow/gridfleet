"""Device-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.devices.services_container import DeviceServices

get_device_services = make_services_getter("devices")
DeviceServicesDep = Annotated["DeviceServices", Depends(get_device_services)]
