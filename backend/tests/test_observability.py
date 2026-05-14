import json
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_comm.client import request as agent_request
from app.core.health import check_readiness
from app.core.metrics import render_metrics
from app.core.metrics_recorders import record_background_loop_run
from app.core.observability import (
    BACKGROUND_LOOP_NAMES,
    REQUEST_ID_HEADER,
    bind_request_context,
    build_background_loop_snapshot,
    clear_request_context,
    configure_logging,
    get_logger,
    set_background_loop_snapshot,
)


async def _seed_ready_loops(db_session: AsyncSession) -> None:
    for loop_name in BACKGROUND_LOOP_NAMES:
        await set_background_loop_snapshot(
            db_session,
            loop_name,
            build_background_loop_snapshot(loop_name, interval_seconds=60.0),
        )
    await db_session.commit()


async def test_health_live_returns_ok_and_request_id(client: AsyncClient) -> None:
    resp = await client.get("/health/live")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert resp.headers[REQUEST_ID_HEADER]


async def test_request_middleware_echoes_request_id(client: AsyncClient) -> None:
    resp = await client.get("/health/live", headers={REQUEST_ID_HEADER: "req-123"})

    assert resp.status_code == 200
    assert resp.headers[REQUEST_ID_HEADER] == "req-123"


async def test_ready_health_endpoints_report_ready_when_loops_are_fresh(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_ready_loops(db_session)

    ready_resp = await client.get("/health/ready")
    alias_resp = await client.get("/api/health")

    assert ready_resp.status_code == 200
    assert ready_resp.json()["checks"]["control_plane_leader"] is True
    assert alias_resp.status_code == 200
    assert alias_resp.json()["checks"]["control_plane_leader"] is True


async def test_ready_health_endpoints_report_unhealthy_when_loop_heartbeats_missing(client: AsyncClient) -> None:
    ready_resp = await client.get("/health/ready")
    alias_resp = await client.get("/api/health")

    assert ready_resp.status_code == 503
    assert ready_resp.json()["checks"]["control_plane_leader"] is False
    assert alias_resp.status_code == 503
    assert alias_resp.json()["checks"]["control_plane_leader"] is False


async def test_check_readiness_reports_db_failure() -> None:
    failing_db = AsyncMock()
    failing_db.execute.side_effect = RuntimeError("db unavailable")

    payload, status_code = await check_readiness(failing_db)

    assert status_code == 503
    assert payload["status"] == "unhealthy"
    assert "db unavailable" in payload["checks"]["database"]


async def test_metrics_endpoint_returns_prometheus_payload(client: AsyncClient, db_session: AsyncSession) -> None:
    await _seed_ready_loops(db_session)
    record_background_loop_run("heartbeat", 0.05)

    await client.get("/health/live")
    resp = await client.get("/metrics")
    body = resp.text

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in body
    assert "background_loop_runs_total" in body
    assert "pending_jobs" in body
    assert "active_sessions" in body
    assert "active_sse_connections" in body


async def test_agent_request_forwards_request_id_and_records_metrics() -> None:
    bind_request_context(request_id="req-agent-1", method="GET", path="/health/live")
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = httpx.Response(200, json={"status": "ok"})

    response = await agent_request(
        "GET",
        "http://10.0.0.1:5100/agent/health",
        endpoint="agent_health",
        host="10.0.0.1",
        client=client,
        timeout=5,
    )

    clear_request_context()

    assert response.status_code == 200
    client.get.assert_awaited_once()
    assert client.get.await_args.kwargs["headers"][REQUEST_ID_HEADER] == "req-agent-1"
    metrics_text = render_metrics().decode()
    expected_label = 'agent_calls_total{client_mode="fresh",endpoint="agent_health",host="10.0.0.1",outcome="success"}'
    assert expected_label in metrics_text


def test_structured_logs_include_request_and_loop_context(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(force=True)
    logger = get_logger("tests.observability")
    bind_request_context(request_id="req-log-1", method="GET", path="/health/live")

    with structlog.contextvars.bound_contextvars(loop_name="heartbeat", loop_owner="owner-1"):
        logger.info("structured test", device="demo")

    clear_request_context()
    captured = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(captured)

    assert payload["message"] == "structured test"
    assert payload["request_id"] == "req-log-1"
    assert payload["loop_name"] == "heartbeat"
    assert payload["loop_owner"] == "owner-1"
    assert payload["device"] == "demo"


def test_leader_keepalive_is_required_for_readiness() -> None:
    assert "control_plane_leader_keepalive" in BACKGROUND_LOOP_NAMES
