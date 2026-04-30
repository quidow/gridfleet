from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class HostPackFeatureStatus(Base):
    """Per-host, per-pack, per-feature health snapshot.

    Updated whenever the agent reports the result of a feature probe or action;
    the service layer compares the new ``ok`` against the persisted value to
    decide whether to publish a ``pack_feature.degraded`` /
    ``pack_feature.recovered`` SystemEvent.
    """

    __tablename__ = "host_pack_feature_status"
    __table_args__ = (
        UniqueConstraint(
            "host_id",
            "pack_id",
            "feature_id",
            name="host_pack_feature_status_uq",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pack_id: Mapped[str] = mapped_column(String, nullable=False)
    feature_id: Mapped[str] = mapped_column(String, nullable=False)
    ok: Mapped[bool] = mapped_column(nullable=False)
    detail: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
