"""Device-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.devices.services_container import DeviceServices


def get_device_services(request: Request) -> DeviceServices:
    return request.app.state.services.devices  # type: ignore[no-any-return]


DeviceServicesDep = Annotated["DeviceServices", Depends(get_device_services)]
