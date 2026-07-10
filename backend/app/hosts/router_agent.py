from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.core.dependencies import DbDep
from app.hosts import service as host_service
from app.hosts.dependencies import HostServicesDep
from app.hosts.models import Host
from app.hosts.schemas import HostStatusPush
from app.packs.dependencies import PackServicesDep

router = APIRouter(prefix="/agent/hosts", tags=["agent-hosts"])


@router.post("/status", status_code=204)
async def status(db: DbDep, hosts: HostServicesDep, packs: PackServicesDep, push: HostStatusPush) -> Response:
    host = await db.get(Host, push.host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Unknown host_id")
    if push.capabilities is not None:
        try:
            host_service.validate_orchestration_contract(push.capabilities, host_label=f"{host.hostname} ({host.id})")
        except ValueError as exc:
            raise HTTPException(status_code=426, detail=str(exc)) from exc
    await hosts.status_push.apply_status_push(db, host, push)
    if push.packs is not None:
        await packs.status.apply_status(db, {"host_id": str(push.host_id), **push.packs})
    await db.commit()
    return Response(status_code=204)
