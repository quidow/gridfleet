from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.appium_node import AppiumNode


class AppiumNodeResourceClaim(Base):
    __tablename__ = "appium_node_resource_claims"
    __table_args__ = (
        UniqueConstraint(
            "host_id",
            "capability_key",
            "port",
            name="uq_appium_node_resource_claims_port",
        ),
        CheckConstraint(
            "(node_id IS NOT NULL AND owner_token IS NULL AND expires_at IS NULL) "
            "OR (node_id IS NULL AND owner_token IS NOT NULL AND expires_at IS NOT NULL)",
            name="ck_appium_node_resource_claims_flavour",
        ),
        Index("ix_appium_node_resource_claims_node_id", "node_id"),
        Index("ix_appium_node_resource_claims_owner_token", "host_id", "owner_token"),
        Index("ix_appium_node_resource_claims_expires_at", "expires_at"),
        Index(
            "uq_appium_node_resource_claims_temp_owner",
            "host_id",
            "owner_token",
            "capability_key",
            unique=True,
            postgresql_where=text("owner_token IS NOT NULL"),
        ),
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
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appium_nodes.id", ondelete="CASCADE"),
        nullable=True,
    )
    owner_token: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    node: Mapped[AppiumNode | None] = relationship("AppiumNode", back_populates="resource_claims")
