"""Lifecycle-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.lifecycle.services_container import LifecycleServices

get_lifecycle_services = make_services_getter("lifecycle")
LifecycleServicesDep = Annotated["LifecycleServices", Depends(get_lifecycle_services)]
