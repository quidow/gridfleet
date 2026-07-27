"""Tests that host status events carry the correct severity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from app.hosts.service import _host_status_severity
from tests.helpers import dispatch_committed_events
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.events import Event

# ---------------------------------------------------------------------------
# Unit tests: _host_status_severity helper
# ---------------------------------------------------------------------------


def test_host_status_offline_emits_warning() -> None:
    """online → offline should produce 'warning'."""
    assert _host_status_severity("online", "offline") == "warning"


def test_host_status_pending_to_offline_emits_warning() -> None:
    """pending → offline should produce 'warning'."""
    assert _host_status_severity("pending", "offline") == "warning"


def test_host_status_already_offline_emits_info() -> None:
    """offline → offline (no change) should produce 'info'."""
    assert _host_status_severity("offline", "offline") == "info"


def test_host_status_back_online_emits_success() -> None:
    """offline → online should produce 'success'."""
    assert _host_status_severity("offline", "online") == "success"


def test_host_status_pending_to_online_emits_success() -> None:
    """pending → online (approval) should produce 'success'."""
    assert _host_status_severity("pending", "online") == "success"


def test_host_status_already_online_emits_info() -> None:
    """online → online (no change) should produce 'info'."""
    assert _host_status_severity("online", "online") == "info"


def test_host_status_none_old_to_online_emits_info() -> None:
    """None → online (first registration) should produce 'info'."""
    assert _host_status_severity(None, "online") == "info"


def test_host_status_none_old_to_offline_emits_info() -> None:
    """None → offline (born offline) should produce 'info'."""
    assert _host_status_severity(None, "offline") == "info"


# ---------------------------------------------------------------------------
# Integration tests: approve_host stages the severity on its source transaction
# (queue_for_session); the capture handler sees it after dispatch_committed_events.
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.db


def _make_severity_capture() -> list[dict[str, Any]]:
    """Return a list that accumulates {type, severity} for each dispatched event."""
    captured: list[dict[str, Any]] = []

    async def capture(event: Event) -> None:
        captured.append({"type": event.type, "severity": event.severity})

    event_bus.register_handler(capture)
    return captured


@pytest.mark.db
async def test_approve_host_pending_to_online_emits_success(
    db_session: AsyncSession,
) -> None:
    """approve_host transitions pending→online and should emit severity='success'."""
    from app.hosts.models import Host, HostStatus, OSType
    from app.hosts.service import HostCrudService
    from tests.fakes import FakeSettingsReader

    captured = _make_severity_capture()

    host = Host(
        hostname="approve-severity-host",
        ip="10.99.0.1",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.pending,
    )
    db_session.add(host)
    await db_session.flush()

    approved = await HostCrudService(publisher=event_bus, settings=FakeSettingsReader({})).approve_host(
        db_session, host.id
    )
    assert approved is not None
    # approve_host is transaction-local now: the staged row only becomes
    # deliverable once the caller's transaction commits.
    await db_session.commit()

    await dispatch_committed_events()
    events = [e for e in captured if e["type"] == "host.status_changed"]
    assert len(events) == 1
    assert events[0]["severity"] == "success"
