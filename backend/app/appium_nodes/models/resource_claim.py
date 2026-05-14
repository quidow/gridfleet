from __future__ import annotations

import uuid
from datetime import datetime  # noqa: TC003 - SQLAlchemy resolves mapped annotations at runtime.
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.appium_nodes.models import AppiumNode


class AppiumNodeResourceClaim(Base):
    __tablename__ = "appium_node_resource_claims"
    __table_args__ = (
        UniqueConstraint(
            "host_id",
            "capability_key",
            "port",
            name="uq_appium_node_resource_claims_port",
        ),
        Index("ix_appium_node_resource_claims_node_id", "node_id"),
        Index(
            "uq_appium_node_resource_claims_managed_node",
            "node_id",
            "capability_key",
            unique=True,
            postgresql_where=text("node_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    host_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    capability_key: Mapped[str] = mapped_column(String, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appium_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    node: Mapped[AppiumNode] = relationship("AppiumNode", back_populates="resource_claims")
