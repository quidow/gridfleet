from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from sqlalchemy import select

from app.core.observability import get_logger
from app.hosts import service as host_service
from app.hosts.dependencies import HostServicesDep
from app.hosts.models import Host
from app.hosts.schemas import HostStatusPush
from app.hosts.service_status_push import (
    BootFenceError,
    BootFenceSupersededError,
    SectionHashMismatchError,
    StatusPushTarget,
)
from app.packs.dependencies import PackServicesDep

router = APIRouter(prefix="/agent/hosts", tags=["agent-hosts"])
logger = get_logger(__name__)


@router.post("/status", status_code=204)
async def status(hosts: HostServicesDep, packs: PackServicesDep, push: HostStatusPush) -> Response:
    # Txn A locks the host row so the initial boot fence and liveness publication
    # are atomic against registration and concurrent pushes. It commits on exit;
    # any failure inside rolls the whole liveness phase back.
    async with hosts.session_factory.begin() as db:
        host = await db.get(Host, push.host_id, with_for_update=True)
        if host is None:
            raise HTTPException(status_code=404, detail="Unknown host_id")
        if push.capabilities is not None:
            try:
                host_service.validate_orchestration_contract(
                    push.capabilities, host_label=f"{host.hostname} ({host.id})"
                )
            except ValueError as exc:
                raise HTTPException(status_code=426, detail=str(exc)) from exc
        # Detached identity: the stages below open their own sessions and must
        # never carry a Host row belonging to a transaction that has closed.
        target = StatusPushTarget(host.id, host.ip, host.agent_port)
        # Fence before liveness, then publish the snapshot without guarded
        # revisions. The status-fold loop cannot consume it until Txn B below.
        try:
            pending = await hosts.status_push.begin_status_push(db, host, push)
        except BootFenceError as exc:
            raise BootFenceSupersededError from exc
        except SectionHashMismatchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if push.packs is not None:
            await packs.status.apply_status(db, {"host_id": str(push.host_id), **push.packs})

    # Reserve pool headroom before checking out the Txn-B connection: each owner
    # holds that connection while convergence uses one nested session at a time.
    sections: dict[str, Any] | None = None
    async with hosts.status_push.publication_slot():
        try:
            # Txn B holds the host lock across convergence and finalization.
            # Concurrent pushes for one host therefore cannot apply observed
            # process identity out of order; a superseded request converges
            # nothing. Both suppression returns below exit the context normally
            # and therefore commit, as the pre-split code did.
            async with hosts.session_factory.begin() as db:
                locked_host = await db.scalar(select(Host).where(Host.id == target.host_id).with_for_update())
                if locked_host is None or not await hosts.status_push.pending_is_current(db, locked_host, pending):
                    return Response(status_code=204)
                converged = await hosts.status_push.process_prepublication(target=target, payload=pending.sections)
                if not converged:
                    return Response(status_code=204)
                sections = await hosts.status_push.finalize_status_push(db, locked_host, pending)
        except BootFenceError as exc:
            # Raising out of the context rolled Txn B back: a superseded boot
            # publishes nothing.
            raise BootFenceSupersededError from exc
    if sections is None:
        return Response(status_code=204)
    try:
        await hosts.status_push.process_observation_folds(host_id=target.host_id, payload=sections)
    except Exception:
        logger.exception("push_observation_processing_failed", host_id=str(target.host_id))
    return Response(status_code=204)
