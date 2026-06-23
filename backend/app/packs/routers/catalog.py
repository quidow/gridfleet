from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from app.auth.dependencies import AdminDep
from app.core.dependencies import DbDep
from app.core.http_errors import convert_not_found, found_or_404
from app.packs.dependencies import PackServicesDep
from app.packs.models import PackState
from app.packs.schemas import (
    DriverPackHostsOut,
    PackCatalog,
    PackOut,
    PackPatch,
    RuntimePolicyPatch,
)
from app.packs.services.service import build_pack_out

router = APIRouter(prefix="/api/driver-packs", tags=["driver-packs"])


@router.get("/catalog", response_model=PackCatalog)
async def catalog(session: DbDep, packs: PackServicesDep) -> PackCatalog:
    return await packs.catalog.list_catalog(session)


@router.get("/{pack_id}", response_model=PackOut)
async def get_pack(pack_id: str, session: DbDep, packs: PackServicesDep) -> PackOut:
    return found_or_404(await packs.catalog.get_pack_detail(session, pack_id), f"Pack {pack_id!r} not found")


@router.get("/{pack_id}/hosts", response_model=DriverPackHostsOut)
async def hosts(pack_id: str, session: DbDep, packs: PackServicesDep) -> DriverPackHostsOut:
    found_or_404(await packs.catalog.get_pack_detail(session, pack_id), f"Pack {pack_id!r} not found")
    return DriverPackHostsOut.model_validate(await packs.status.get_driver_pack_host_status(session, pack_id))


@router.patch("/{pack_id}", response_model=PackOut)
async def update_pack(
    pack_id: str,
    body: PackPatch,
    _username: AdminDep,
    session: DbDep,
    packs: PackServicesDep,
    override: bool = False,
) -> PackOut:
    try:
        target = PackState(body.state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid state: {body.state!r}") from exc
    try:
        pack = await packs.lifecycle.transition_pack_state(session, pack_id, target, override=override)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"Pack {pack_id!r} not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return build_pack_out(pack)


@router.patch("/{pack_id}/policy", response_model=PackOut)
async def update_runtime_policy(
    pack_id: str,
    body: RuntimePolicyPatch,
    _username: AdminDep,
    session: DbDep,
    packs: PackServicesDep,
) -> PackOut:
    with convert_not_found(f"Pack {pack_id!r} not found"):
        pack = await packs.catalog.set_runtime_policy(session, pack_id, body.runtime_policy)
    return build_pack_out(pack)


@router.delete("/{pack_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_driver_pack(
    pack_id: str,
    _username: AdminDep,
    session: DbDep,
    packs: PackServicesDep,
) -> Response:
    try:
        await packs.catalog.delete_pack(session, pack_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
