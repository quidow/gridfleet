from __future__ import annotations

import enum
import uuid
from datetime import datetime  # noqa: TC003 - SQLAlchemy resolves Mapped annotations at runtime.
from typing import Any

from sqlalchemy import DateTime, Enum, Integer, String, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


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

    devices: Mapped[list[Any]] = relationship("Device", back_populates="host")
    terminal_sessions: Mapped[list[Any]] = relationship(
        "HostTerminalSession", back_populates="host", cascade="all, delete-orphan"
    )

    @property
    def missing_prerequisites(self) -> list[str]:
        capabilities = self.capabilities if isinstance(self.capabilities, dict) else {}
        missing = capabilities.get("missing_prerequisites")
        if not isinstance(missing, list):
            return []
        return [item for item in missing if isinstance(item, str)]
