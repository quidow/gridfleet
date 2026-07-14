from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from sqlalchemy import select

from app.core.dependencies import DbDep
from app.core.observability import get_logger
from app.hosts import service as host_service
from app.hosts.dependencies import HostServicesDep
from app.hosts.models import Host
from app.hosts.schemas import HostStatusPush
from app.hosts.service_status_push import BootFenceError, SectionHashMismatchError
from app.packs.dependencies import PackServicesDep

router = APIRouter(prefix="/agent/hosts", tags=["agent-hosts"])
logger = get_logger(__name__)


@router.post("/status", status_code=204)
async def status(db: DbDep, hosts: HostServicesDep, packs: PackServicesDep, push: HostStatusPush) -> Response:
    # Txn A locks the host row so the initial boot fence and liveness publication
    # are atomic against registration and concurrent pushes.
    host = await db.get(Host, push.host_id, with_for_update=True)
    if host is None:
        raise HTTPException(status_code=404, detail="Unknown host_id")
    if push.capabilities is not None:
        try:
            host_service.validate_orchestration_contract(push.capabilities, host_label=f"{host.hostname} ({host.id})")
        except ValueError as exc:
            raise HTTPException(status_code=426, detail=str(exc)) from exc
    host_id, host_ip, agent_port = host.id, host.ip, host.agent_port
    # Txn A: fence before liveness, then publish the snapshot without guarded
    # revisions. The status-fold loop cannot consume it until Txn B below.
    try:
        pending = await hosts.status_push.begin_status_push(db, host, push)
    except BootFenceError as exc:
        raise HTTPException(status_code=409, detail="Stale or superseded boot_id") from exc
    except SectionHashMismatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if push.packs is not None:
        await packs.status.apply_status(db, {"host_id": str(push.host_id), **push.packs})
    await db.commit()

    # Reserve pool headroom before checking out the Txn-B connection: each owner
    # holds that connection while convergence uses one nested session at a time.
    async with hosts.status_push.publication_slot():
        # Hold the host lock across convergence and finalization. Concurrent
        # pushes for one host therefore cannot apply observed process identity
        # out of order; a superseded request does no convergence.
        locked_host = (await db.execute(select(Host).where(Host.id == host_id).with_for_update())).scalar_one_or_none()
        if locked_host is None:
            return Response(status_code=204)
        try:
            if not await hosts.status_push.pending_is_current(db, locked_host, pending):
                await db.commit()
                return Response(status_code=204)
            converged = await hosts.status_push.process_prepublication(
                host_id=host_id,
                host_ip=host_ip,
                agent_port=agent_port,
                payload=pending.sections,
            )
            if not converged:
                await db.commit()
                return Response(status_code=204)
            sections = await hosts.status_push.finalize_status_push(db, locked_host, pending)
        except BootFenceError as exc:
            raise HTTPException(status_code=409, detail="Stale or superseded boot_id") from exc
        await db.commit()
    if sections is None:
        return Response(status_code=204)
    try:
        await hosts.status_push.process_observation_folds(host_id=host_id, payload=sections)
    except Exception:
        logger.exception("push_observation_processing_failed", host_id=str(host_id))
    return Response(status_code=204)
