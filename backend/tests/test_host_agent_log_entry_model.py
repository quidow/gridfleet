from __future__ import annotations

from sqlalchemy import UniqueConstraint, inspect

from app.hosts.models import HostAgentLogEntry


def test_table_name() -> None:
    assert HostAgentLogEntry.__tablename__ == "host_agent_log_entry"


def test_columns_present() -> None:
    mapper = inspect(HostAgentLogEntry)
    cols = {column.key for column in mapper.columns}
    assert cols >= {
        "id",
        "host_id",
        "boot_id",
        "sequence_no",
        "ts",
        "received_at",
        "level",
        "logger_name",
        "message",
    }


def test_constraints() -> None:
    table = HostAgentLogEntry.__table__
    uq_names = {constraint.name for constraint in table.constraints if isinstance(constraint, UniqueConstraint)}
    assert "uq_agent_log_seq" in uq_names
    idx_names = {index.name for index in table.indexes}
    assert "ix_agent_log_host_ts" in idx_names
