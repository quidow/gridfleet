from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.host import Host


class HostRuntimeInstallation(Base):
    __tablename__ = "host_runtime_installations"
    __table_args__ = (
        UniqueConstraint(
            "host_id",
            "runtime_id",
            name="host_runtime_installations_host_runtime_uq",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
    )
    runtime_id: Mapped[str] = mapped_column(String, nullable=False)
    appium_server_package: Mapped[str] = mapped_column(String, nullable=False)
    appium_server_version: Mapped[str] = mapped_column(String, nullable=False)
    driver_specs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    plugin_specs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False, server_default=text("'[]'::jsonb")
    )
    appium_home: Mapped[str | None] = mapped_column(String, nullable=True)
    refcount: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String, default="pending", nullable=False, server_default="pending")
    blocked_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    host: Mapped[Host] = relationship("Host")
