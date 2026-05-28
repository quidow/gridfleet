"""Pack-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.packs.services_container import PackServices


def get_pack_services(request: Request) -> PackServices:
    return request.app.state.services.packs  # type: ignore[no-any-return]


PackServicesDep = Annotated["PackServices", Depends(get_pack_services)]
