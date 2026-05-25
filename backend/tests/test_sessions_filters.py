from sqlalchemy import select

from app.sessions.filters import (
    exclude_non_success_metric_sessions,
    exclude_non_test_sessions,
)
from app.sessions.models import Session


def test_exclude_non_test_sessions_drops_probe_marker() -> None:
    stmt = exclude_non_test_sessions(select(Session))
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "__gridfleet_probe__" in compiled


def test_exclude_non_success_metric_sessions_drops_probe_marker() -> None:
    stmt = exclude_non_success_metric_sessions(select(Session))
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "__gridfleet_probe__" in compiled
