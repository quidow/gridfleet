from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.core.dependencies import DbDep
from app.core.observability import get_logger
from app.hosts import service as host_service
from app.hosts.dependencies import HostServicesDep
from app.hosts.models import Host
from app.hosts.schemas import HostStatusPush
from app.hosts.service_status_push import BootFenceError
from app.packs.dependencies import PackServicesDep

router = APIRouter(prefix="/agent/hosts", tags=["agent-hosts"])
logger = get_logger(__name__)


@router.post("/status", status_code=204)
async def status(db: DbDep, hosts: HostServicesDep, packs: PackServicesDep, push: HostStatusPush) -> Response:
    # Lock the host row: the boot fence and per-section cursor advance must be
    # atomic against a concurrent push for the same host (no lost cursor update).
    host = await db.get(Host, push.host_id, with_for_update=True)
    if host is None:
        raise HTTPException(status_code=404, detail="Unknown host_id")
    if push.capabilities is not None:
        try:
            host_service.validate_orchestration_contract(push.capabilities, host_label=f"{host.hostname} ({host.id})")
        except ValueError as exc:
            raise HTTPException(status_code=426, detail=str(exc)) from exc
    host_id, host_ip, agent_port = host.id, host.ip, host.agent_port
    # apply_status_push fences the boot, stamps the ingest-time observation
    # revision onto advanced sections, and returns the payload to fold this push
    # (re-deliveries omitted). The revision is drawn before restart-ingest/
    # convergence run in process_observations.
    try:
        sections = await hosts.status_push.apply_status_push(db, host, push)
    except BootFenceError as exc:
        raise HTTPException(status_code=409, detail="Stale or superseded boot_id") from exc
    if push.packs is not None:
        await packs.status.apply_status(db, {"host_id": str(push.host_id), **push.packs})
    await db.commit()
    try:
        await hosts.status_push.process_observations(
            host_id=host_id,
            host_ip=host_ip,
            agent_port=agent_port,
            payload=sections,
        )
    except Exception:
        logger.exception("push_observation_processing_failed", host_id=str(host_id))
    return Response(status_code=204)
