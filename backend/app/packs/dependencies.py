"""Pack-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.packs.services_container import PackServices

get_pack_services = make_services_getter("packs")
PackServicesDep = Annotated["PackServices", Depends(get_pack_services)]
