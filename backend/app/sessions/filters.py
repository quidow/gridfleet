from typing import Any

from sqlalchemy import or_
from sqlalchemy.sql import Select

from app.sessions.models import Session
from app.sessions.probe_constants import PROBE_TEST_NAME

RESERVED_SESSION_ID = "reserved"


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


def only_probe_sessions(stmt: Select[Any]) -> Select[Any]:
    return exclude_reserved_sessions(stmt).where(Session.test_name == PROBE_TEST_NAME)
