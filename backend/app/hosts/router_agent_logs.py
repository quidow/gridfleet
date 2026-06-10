"""Agent to backend log ingest endpoint."""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI inspects path parameter annotations at runtime.

from fastapi import APIRouter, status

from app.core.dependencies import DbDep  # noqa: TC001 - FastAPI resolves dependency aliases at runtime.
from app.core.http_errors import found_or_404
from app.hosts.dependencies import HostServicesDep  # noqa: TC001 - FastAPI resolves dependency aliases at runtime.
from app.hosts.models import Host
from app.hosts.schemas import AgentLogBatchIngest, AgentLogIngestResult

router = APIRouter(prefix="/agent", tags=["agent-logs"])


@router.post(
    "/{host_id}/log-batch",
    response_model=AgentLogIngestResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a batch of agent process log lines",
)
async def ingest_agent_log_batch(
    host_id: uuid.UUID,
    payload: AgentLogBatchIngest,
    db: DbDep,
    host_services: HostServicesDep,
) -> AgentLogIngestResult:
    found_or_404(await db.get(Host, host_id), "host not found")
    return await host_services.agent_logs.write_batch(db, host_id=host_id, batch=payload)
