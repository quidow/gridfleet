"""Agent-comm FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.agent_comm.services_container import AgentCommServices

get_agent_comm_services = make_services_getter("agent_comm")
AgentCommServicesDep = Annotated["AgentCommServices", Depends(get_agent_comm_services)]
