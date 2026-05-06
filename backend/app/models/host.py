from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, Integer, String, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.host_terminal_session import HostTerminalSession

from app.services.host_versioning import (
    AgentVersionStatus,
    get_agent_version_status,
    get_recommended_agent_version,
    get_required_agent_version,
    is_agent_update_available,
)


class OSType(enum.StrEnum):
    linux = "linux"
    macos = "macos"


class HostStatus(enum.StrEnum):
    online = "online"
    offline = "offline"
    pending = "pending"


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hostname: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    ip: Mapped[str] = mapped_column(String, nullable=False)
    os_type: Mapped[OSType] = mapped_column(Enum(OSType), nullable=False)
    agent_port: Mapped[int] = mapped_column(Integer, default=5100, nullable=False)
    status: Mapped[HostStatus] = mapped_column(Enum(HostStatus), default=HostStatus.offline, nullable=False)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String, nullable=True)
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    devices: Mapped[list[Device]] = relationship("Device", back_populates="host")
    terminal_sessions: Mapped[list[HostTerminalSession]] = relationship(
        "HostTerminalSession", back_populates="host", cascade="all, delete-orphan"
    )

    @property
    def required_agent_version(self) -> str | None:
        return get_required_agent_version()

    @property
    def agent_version_status(self) -> AgentVersionStatus:
        return get_agent_version_status(self)

    @property
    def recommended_agent_version(self) -> str | None:
        return get_recommended_agent_version()

    @property
    def agent_update_available(self) -> bool:
        return is_agent_update_available(self.agent_version)

    @property
    def missing_prerequisites(self) -> list[str]:
        capabilities = self.capabilities if isinstance(self.capabilities, dict) else {}
        missing = capabilities.get("missing_prerequisites")
        if not isinstance(missing, list):
            return []
        return [item for item in missing if isinstance(item, str)]
