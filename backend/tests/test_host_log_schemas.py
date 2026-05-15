from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.hosts.schemas import (
    AgentLogBatchIngest,
    AgentLogLine,
    AgentLogPage,
    HostEventEntry,
    HostEventsPage,
    ShippedLogLineIngest,
)


def test_shipped_log_line_round_trip() -> None:
    line = ShippedLogLineIngest(
        ts=datetime.now(UTC),
        level="INFO",
        logger_name="agent.foo",
        message="hello",
        sequence_no=1,
    )
    assert line.sequence_no == 1


def test_ingest_batch_requires_valid_boot_id() -> None:
    AgentLogBatchIngest(boot_id=uuid4(), lines=[])
    with pytest.raises(ValidationError):
        AgentLogBatchIngest(boot_id="not-a-uuid", lines=[])  # type: ignore[arg-type]


def test_agent_log_page_shape() -> None:
    page = AgentLogPage(lines=[], total=0, has_more=False)
    assert page.has_more is False


def test_host_events_page_shape() -> None:
    page = HostEventsPage(events=[], total=0)
    assert page.total == 0


def test_host_event_entry_shape() -> None:
    entry = HostEventEntry(
        event_id="abc",
        type="host.status_changed",
        ts=datetime.now(UTC),
        data={"host_id": "x", "old_status": "online", "new_status": "degraded"},
    )
    assert entry.type.startswith("host.")


def test_agent_log_line_validates_level() -> None:
    AgentLogLine(
        ts=datetime.now(UTC),
        level="WARNING",
        logger_name="x",
        message="m",
        sequence_no=1,
        boot_id=uuid4(),
    )
