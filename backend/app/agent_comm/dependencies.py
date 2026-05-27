"""Agent-comm FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.agent_comm.services_container import AgentCommServices


def get_agent_comm_services(request: Request) -> AgentCommServices:
    return request.app.state.services.agent_comm  # type: ignore[no-any-return]


AgentCommServicesDep = Annotated["AgentCommServices", Depends(get_agent_comm_services)]
