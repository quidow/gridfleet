from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from app.auth.dependencies import AdminDep
from app.core.dependencies import DbDep
from app.core.http_errors import found_or_404
from app.packs.dependencies import PackServicesDep
from app.packs.models import PackState
from app.packs.schemas import (
    DriverPackHostsOut,
    PackCatalog,
    PackOut,
    PackPatch,
    RuntimePolicyPatch,
)
from app.packs.services.service import PackNotFound, PackTransitionError, unlink_pack_artifact
from app.settings.dependencies import SettingsServicesDep

router = APIRouter(prefix="/api/driver-packs", tags=["driver-packs"])


@router.get("/catalog", response_model=PackCatalog)
async def catalog(session: DbDep, packs: PackServicesDep) -> PackCatalog:
    return await packs.catalog.list_catalog(session)


@router.get("/{pack_id}", response_model=PackOut)
async def get_pack(pack_id: str, session: DbDep, packs: PackServicesDep) -> PackOut:
    return found_or_404(await packs.catalog.get_pack_detail(session, pack_id), f"Pack {pack_id!r} not found")


@router.get("/{pack_id}/hosts", response_model=DriverPackHostsOut)
async def hosts(
    pack_id: str,
    session: DbDep,
    packs: PackServicesDep,
    settings_services: SettingsServicesDep,
) -> DriverPackHostsOut:
    found_or_404(await packs.catalog.get_pack_detail(session, pack_id), f"Pack {pack_id!r} not found")
    offline_after = settings_services.service.get_float("general.host_offline_after_sec")
    return DriverPackHostsOut.model_validate(
        await packs.status.get_driver_pack_host_status(session, pack_id, offline_after_sec=offline_after)
    )


@router.patch("/{pack_id}", response_model=PackOut)
async def update_pack(
    pack_id: str,
    body: PackPatch,
    _username: AdminDep,
    packs: PackServicesDep,
) -> PackOut:
    try:
        target = PackState(body.state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid state: {body.state!r}") from exc
    # Caught by name, not as LookupError/ValueError: the command builds its
    # response snapshot inside the transaction, and build_pack_out raises
    # KeyError (malformed manifest/platform data) and pydantic ValidationError
    # (malformed persisted policy) from in there. Those are 500s, not a 404 and
    # not a 400 handed to a caller whose request was fine.
    try:
        async with packs.session_factory.begin() as db:
            return await packs.lifecycle.transition_pack_state_txn(db, pack_id, target)
    except PackNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Pack {pack_id!r} not found") from exc
    except PackTransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{pack_id}/policy", response_model=PackOut)
async def update_runtime_policy(
    pack_id: str,
    body: RuntimePolicyPatch,
    _username: AdminDep,
    packs: PackServicesDep,
) -> PackOut:
    try:
        async with packs.session_factory.begin() as db:
            return await packs.catalog.set_runtime_policy(db, pack_id, body.runtime_policy)
    except PackNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Pack {pack_id!r} not found") from exc


@router.delete("/{pack_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_driver_pack(
    pack_id: str,
    _username: AdminDep,
    packs: PackServicesDep,
) -> Response:
    try:
        async with packs.session_factory.begin() as db:
            artifact_paths = await packs.catalog.delete_pack(db, pack_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Post-commit, so no pack row lock spans the filesystem. The deletion the
    # caller asked for is durable either way, so a failing unlink is logged and
    # the success status still returned (see unlink_pack_artifact).
    for artifact_path in artifact_paths:
        unlink_pack_artifact(artifact_path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
