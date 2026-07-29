from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PackArtifactState(enum.StrEnum):
    pending = "pending"
    active = "active"
    orphaned = "orphaned"


class PackArtifact(Base):
    """One row per artifact file the system has ever intended to write.

    The durable record of intent that lets a crash be told apart from a
    mid-upload: a file with a ``pending`` row past its grace window is garbage,
    a file with an ``active`` row is wanted, and an ``orphaned`` row is an
    unlink the delete path could not finish. Nothing reads this table on the
    serving path -- ``DriverPackRelease.artifact_path`` still names the file.
    """

    __tablename__ = "pack_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    path: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[PackArtifactState] = mapped_column(
        Enum(PackArtifactState, native_enum=False, create_constraint=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    state_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
