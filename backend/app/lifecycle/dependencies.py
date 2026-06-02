"""Lifecycle-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.lifecycle.services_container import LifecycleServices


def get_lifecycle_services(request: Request) -> LifecycleServices:
    return request.app.state.services.lifecycle  # type: ignore[no-any-return]


LifecycleServicesDep = Annotated["LifecycleServices", Depends(get_lifecycle_services)]
