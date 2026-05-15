"""Agent to backend log ingest endpoint."""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI inspects path parameter annotations at runtime.

from fastapi import APIRouter, HTTPException, status

from app.core.dependencies import DbDep  # noqa: TC001 - FastAPI resolves dependency aliases at runtime.
from app.hosts.models import Host
from app.hosts.schemas import AgentLogBatchIngest, AgentLogIngestResult
from app.hosts.service_agent_logs import write_batch

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
) -> AgentLogIngestResult:
    host = await db.get(Host, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="host not found")
    return await write_batch(db, host_id=host_id, batch=payload)
