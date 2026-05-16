"""Tests that host status and hardware health events carry the correct severity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from app.hosts.service import _host_status_severity
from app.hosts.service_hardware_telemetry import _hardware_severity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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
# Unit tests: _hardware_severity helper
# ---------------------------------------------------------------------------


def test_hardware_health_critical_emits_critical() -> None:
    """Any → critical should produce 'critical'."""
    assert _hardware_severity("ok", "critical") == "critical"
    assert _hardware_severity("warning", "critical") == "critical"
    assert _hardware_severity(None, "critical") == "critical"


def test_hardware_health_recovery_from_warning_emits_success() -> None:
    """warning → ok recovery should produce 'success'."""
    assert _hardware_severity("warning", "ok") == "success"


def test_hardware_health_recovery_from_critical_emits_success() -> None:
    """critical → ok recovery should produce 'success'."""
    assert _hardware_severity("critical", "ok") == "success"


def test_hardware_health_warning_emits_warning() -> None:
    """ok → warning should produce 'warning' (default)."""
    assert _hardware_severity("ok", "warning") == "warning"


def test_hardware_health_no_prior_status_warning_emits_warning() -> None:
    """None → warning should produce 'warning'."""
    assert _hardware_severity(None, "warning") == "warning"


# ---------------------------------------------------------------------------
# Integration tests: approve_host passes severity to event_bus.publish
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.db


def _make_severity_capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Return a list that accumulates {type, severity} for each publish call."""
    captured: list[dict[str, Any]] = []

    async def _fake_publish(name: str, payload: dict[str, Any], *, severity: str | None = None) -> None:
        captured.append({"type": name, "severity": severity})

    monkeypatch.setattr("app.events.event_bus.publish", _fake_publish)
    return captured


@pytest.mark.db
async def test_approve_host_pending_to_online_emits_success(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """approve_host transitions pending→online and should emit severity='success'."""
    from app.hosts import service as host_service
    from app.hosts.models import Host, HostStatus, OSType

    captured = _make_severity_capture(monkeypatch)

    host = Host(
        hostname="approve-severity-host",
        ip="10.99.0.1",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.pending,
    )
    db_session.add(host)
    await db_session.flush()

    approved = await host_service.approve_host(db_session, host.id)
    assert approved is not None

    events = [e for e in captured if e["type"] == "host.status_changed"]
    assert len(events) == 1
    assert events[0]["severity"] == "success"
