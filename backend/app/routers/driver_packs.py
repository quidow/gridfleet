from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.database import get_db
from app.models.driver_pack import PackState
from app.schemas.driver_pack import (
    DriverPackHostsOut,
    PackCatalog,
    PackOut,
    PackPatch,
    PackPlatforms,
    RuntimePolicyPatch,
)
from app.services.auth_dependencies import require_admin
from app.services.pack_delete_service import delete_pack
from app.services.pack_lifecycle_service import transition_pack_state
from app.services.pack_policy_service import set_runtime_policy
from app.services.pack_service import build_pack_out, get_pack_detail, get_platforms, list_catalog
from app.services.pack_status_service import get_driver_pack_host_status

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/driver-packs", tags=["driver-packs"])


@router.get("/catalog", response_model=PackCatalog)
async def catalog(session: AsyncSession = Depends(get_db)) -> PackCatalog:
    return await list_catalog(session)


@router.get("/{pack_id}", response_model=PackOut)
async def get_pack(pack_id: str, session: AsyncSession = Depends(get_db)) -> PackOut:
    result = await get_pack_detail(session, pack_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Pack {pack_id!r} not found")
    return result


@router.get("/{pack_id}/platforms", response_model=PackPlatforms)
async def platforms(pack_id: str, session: AsyncSession = Depends(get_db)) -> PackPlatforms:
    result = await get_platforms(session, pack_id)
    if result is None:
        raise HTTPException(status_code=404, detail="pack not found")
    return result


@router.get("/{pack_id}/hosts", response_model=DriverPackHostsOut)
async def hosts(pack_id: str, session: AsyncSession = Depends(get_db)) -> DriverPackHostsOut:
    if await get_pack_detail(session, pack_id) is None:
        raise HTTPException(status_code=404, detail=f"Pack {pack_id!r} not found")
    return DriverPackHostsOut.model_validate(await get_driver_pack_host_status(session, pack_id))


@router.patch("/{pack_id}", response_model=PackOut)
async def update_pack(
    pack_id: str,
    body: PackPatch,
    override: bool = False,
    _username: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> PackOut:
    try:
        target = PackState(body.state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid state: {body.state!r}") from exc
    try:
        pack = await transition_pack_state(session, pack_id, target, override=override)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"Pack {pack_id!r} not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return build_pack_out(pack)


@router.patch("/{pack_id}/policy", response_model=PackOut)
async def update_runtime_policy(
    pack_id: str,
    body: RuntimePolicyPatch,
    _username: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> PackOut:
    try:
        pack = await set_runtime_policy(session, pack_id, body.runtime_policy)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"Pack {pack_id!r} not found") from exc
    return build_pack_out(pack)


@router.delete("/{pack_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_driver_pack(
    pack_id: str,
    _username: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> Response:
    try:
        await delete_pack(session, pack_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
