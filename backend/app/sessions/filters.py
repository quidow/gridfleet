from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_

from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.sql import Select

RESERVED_SESSION_ID = "reserved"


@dataclass(frozen=True, slots=True)
class SessionFilters:
    """Shared session-list filter parameters.

    Bundles the where-clause inputs common to ``list_sessions`` and
    ``list_sessions_cursor`` so each signature stays under the argument limit;
    purely a parameter container — it carries no behavior.
    """

    device_id: uuid.UUID | None = None
    status: SessionStatus | None = None
    pack_id: str | None = None
    platform_id: str | None = None
    started_after: datetime | None = None
    started_before: datetime | None = None
    run_id: uuid.UUID | None = None
    active: bool = False


def exclude_reserved_sessions(stmt: Select[Any]) -> Select[Any]:
    return stmt.where(Session.session_id != RESERVED_SESSION_ID)


def exclude_non_test_sessions(stmt: Select[Any]) -> Select[Any]:
    return exclude_reserved_sessions(stmt).where(or_(Session.test_name.is_(None), Session.test_name != PROBE_TEST_NAME))


def exclude_non_success_metric_sessions(stmt: Select[Any]) -> Select[Any]:
    return exclude_non_test_sessions(stmt).where(
        or_(
            Session.test_name.is_not(None),
            Session.run_id.is_not(None),
        )
    )
