"""Create-orchestrator tests: Appium outcomes resolve claimed allocation rows."""

from __future__ import annotations

import json
import uuid
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.grid import session_create
from app.grid.allocation import AllocationService
from app.grid.models import GridSessionQueueTicket
from app.sessions.models import Session, SessionStatus
from tests.helpers import seed_host_and_running_node

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection

    from app.devices.models import Device
    from app.grid.allocation import AllocationResult


class _TxTracker:
    """Counts open DB context managers (``factory()`` and ``factory.begin()``).

    A patched Appium call asserts ``active == 0`` to prove no transaction is
    pinned across the remote I/O boundary.
    """

    def __init__(self) -> None:
        self.active = 0

    def _enter(self) -> None:
        self.active += 1

    def _exit(self) -> None:
        self.active -= 1


class _TrackingCtx(AbstractAsyncContextManager[AsyncSession]):
    def __init__(self, inner: AbstractAsyncContextManager[AsyncSession], tracker: _TxTracker) -> None:
        self._inner = inner
        self._tracker = tracker

    async def __aenter__(self) -> AsyncSession:
        db = await self._inner.__aenter__()
        self._tracker._enter()
        return db

    async def __aexit__(self, *exc: object) -> bool | None:
        try:
            return await self._inner.__aexit__(*exc)
        finally:
            self._tracker._exit()


class _TrackingFactory:
    def __init__(self, inner: async_sessionmaker[AsyncSession], tracker: _TxTracker) -> None:
        self._inner = inner
        self._tracker = tracker

    def __call__(self) -> _TrackingCtx:
        return _TrackingCtx(self._inner(), self._tracker)

    def begin(self) -> _TrackingCtx:
        return _TrackingCtx(self._inner.begin(), self._tracker)


pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

W3C_OK = {"value": {"sessionId": "app-1", "capabilities": {"platformName": "x"}}}


async def _stereotype_stub(
    db: AsyncSession, device: Device, *, template_cache: object | None = None, matching_group_keys: Collection[str] = ()
) -> dict[str, Any]:
    return {
        "platformName": "Android",
        "appium:udid": device.connection_target,
        "gridfleet:deviceId": str(device.id),
    }


@pytest_asyncio.fixture
async def claimed_allocation(db_session: AsyncSession) -> AllocationResult:
    from app.devices.services.intent import IntentService
    from tests.helpers import test_event_bus as event_bus

    _, _device, _ = await seed_host_and_running_node(db_session, identity=f"grid-create-{uuid.uuid4().hex[:8]}")
    allocation_service = AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=_stereotype_stub,
    )
    ticket = GridSessionQueueTicket(requested_body={"capabilities": {"alwaysMatch": {"platformName": "Android"}}})
    db_session.add(ticket)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=ticket)
    assert result is not None
    await db_session.commit()
    return result


@pytest.fixture
def db_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def tracker() -> _TxTracker:
    return _TxTracker()


def _tracking_factory(db_factory: async_sessionmaker[AsyncSession], tracker: _TxTracker) -> _TrackingFactory:
    return _TrackingFactory(db_factory, tracker)


def _asserting_create_raw(
    inner: Callable[..., Awaitable[tuple[int, bytes, str | None]]], tracker: _TxTracker
) -> Callable[..., Awaitable[tuple[int, bytes, str | None]]]:
    async def fake(target: str, raw: bytes, *, timeout: float) -> tuple[int, bytes, str | None]:
        assert tracker.active == 0, "Appium create issued with an open transaction"
        return await inner(target, raw, timeout=timeout)

    return fake


def _asserting_terminate(
    tracker: _TxTracker,
) -> Callable[..., Awaitable[bool]]:
    async def fake(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        assert tracker.active == 0, "Appium terminate issued with an open transaction"
        return True

    return fake


@pytest.fixture
def allocation_service(db_session: AsyncSession) -> AllocationService:
    from app.devices.services.intent import IntentService
    from tests.helpers import test_event_bus as event_bus

    return AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=_stereotype_stub,
    )


def _ok_raw(
    status: int = 200, body: dict[str, Any] | None = None
) -> Callable[..., Awaitable[tuple[int, bytes, str | None]]]:
    payload = json.dumps(body if body is not None else W3C_OK).encode()

    async def fake(target: str, raw: bytes, *, timeout: float) -> tuple[int, bytes, str | None]:
        return status, payload, None

    return fake


def _raw_result(
    status: int,
    body: bytes,
    transport_error: str | None = None,
) -> Callable[..., Awaitable[tuple[int, bytes, str | None]]]:
    async def fake(target: str, raw: bytes, *, timeout: float) -> tuple[int, bytes, str | None]:
        return status, body, transport_error

    return fake


def _attempt_metric_value(outcome: str) -> float:
    return session_create.GRID_CREATE_ATTEMPT_TOTAL.labels(outcome=outcome)._value.get()  # type: ignore[attr-defined]


@pytest.mark.db
async def test_created_promotes_row_to_running(
    db_session: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    allocation_service: AllocationService,
    claimed_allocation: AllocationResult,
    monkeypatch: pytest.MonkeyPatch,
    tracker: _TxTracker,
) -> None:
    factory = _tracking_factory(db_factory, tracker)
    monkeypatch.setattr(session_create.appium_direct, "create_session_raw", _asserting_create_raw(_ok_raw(), tracker))
    outcome = await session_create.create_and_promote(
        factory, allocation_service, allocation=claimed_allocation, raw_body=b"{}", claim_window_sec=120
    )
    assert outcome.kind == "created" and outcome.session_id == "app-1"
    assert outcome.appium_status == 200 and outcome.appium_body == W3C_OK
    row = await db_session.get(Session, claimed_allocation.allocation_id)
    assert row is not None
    assert row.status == SessionStatus.running and row.session_id == "app-1"
    assert row.actual_capabilities == {"platformName": "x"}


@pytest.mark.db
async def test_appium_http_error_fails_row_and_relays(
    db_session: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    allocation_service: AllocationService,
    claimed_allocation: AllocationResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {"value": {"error": "session not created", "message": "no device"}}
    monkeypatch.setattr(session_create.appium_direct, "create_session_raw", _ok_raw(500, body))
    outcome = await session_create.create_and_promote(
        db_factory, allocation_service, allocation=claimed_allocation, raw_body=b"{}", claim_window_sec=120
    )
    assert outcome.kind == "w3c_rejected" and outcome.appium_status == 500 and outcome.appium_body == body
    row = await db_session.get(Session, claimed_allocation.allocation_id)
    assert row is not None
    assert row.status == SessionStatus.error and row.ended_at is not None


@pytest.mark.db
async def test_transport_error_fails_row(
    db_session: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    allocation_service: AllocationService,
    claimed_allocation: AllocationResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dead(target: str, raw: bytes, *, timeout: float) -> tuple[int, bytes, str | None]:
        return 0, b"", "connect timeout"

    monkeypatch.setattr(session_create.appium_direct, "create_session_raw", dead)
    outcome = await session_create.create_and_promote(
        db_factory, allocation_service, allocation=claimed_allocation, raw_body=b"{}", claim_window_sec=120
    )
    assert outcome.kind == "target_unreachable" and "connect timeout" in outcome.message
    row = await db_session.get(Session, claimed_allocation.allocation_id)
    assert row is not None
    assert row.status == SessionStatus.error


@pytest.mark.db
async def test_2xx_missing_session_id_sweeps_and_fails(
    db_session: AsyncSession,
    db_factory: async_sessionmaker[AsyncSession],
    allocation_service: AllocationService,
    claimed_allocation: AllocationResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_create.appium_direct, "create_session_raw", _ok_raw(200, {"value": {}}))
    listed: list[str] = []
    killed: list[str] = []

    async def fake_list(target: str, *, timeout: float = 10.0) -> list[str] | None:
        listed.append(target)
        return ["stray-1"]

    async def fake_kill(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        killed.append(session_id)
        return True

    monkeypatch.setattr(session_create.appium_direct, "list_sessions", fake_list)
    monkeypatch.setattr(session_create.appium_direct, "terminate_session", fake_kill)
    outcome = await session_create.create_and_promote(
        db_factory, allocation_service, allocation=claimed_allocation, raw_body=b"{}", claim_window_sec=120
    )
    assert outcome.kind == "target_protocol_error" and "sessionId" in outcome.message
    assert listed and killed == ["stray-1"]


@pytest.mark.db
async def test_non_json_error_body_becomes_target_protocol_error(
    db_factory: async_sessionmaker[AsyncSession],
    allocation_service: AllocationService,
    claimed_allocation: AllocationResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def html(target: str, raw: bytes, *, timeout: float) -> tuple[int, bytes, str | None]:
        return 502, b"<html>bad gateway</html>", None

    monkeypatch.setattr(session_create.appium_direct, "create_session_raw", html)
    outcome = await session_create.create_and_promote(
        db_factory, allocation_service, allocation=claimed_allocation, raw_body=b"{}", claim_window_sec=120
    )
    assert outcome.kind == "target_protocol_error" and "502" in outcome.message


@pytest.mark.db
async def test_non_w3c_json_error_body_becomes_target_protocol_error(
    db_factory: async_sessionmaker[AsyncSession],
    allocation_service: AllocationService,
    claimed_allocation: AllocationResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_create.appium_direct,
        "create_session_raw",
        _raw_result(502, json.dumps({"message": "bad gateway"}).encode()),
    )

    outcome = await session_create.create_and_promote(
        db_factory, allocation_service, allocation=claimed_allocation, raw_body=b"{}", claim_window_sec=120
    )

    assert outcome.kind == "target_protocol_error"
    assert "502" in outcome.message
    assert "bad gateway" in outcome.message


@pytest.mark.db
@pytest.mark.parametrize(
    ("status", "body", "transport_error", "expected_kind"),
    [
        (200, json.dumps(W3C_OK).encode(), None, "created"),
        (
            500,
            json.dumps({"value": {"error": "session not created", "message": "no device"}}).encode(),
            None,
            "w3c_rejected",
        ),
        (0, b"", "connection refused", "target_unreachable"),
        (502, b"<html>bad gateway</html>", None, "target_protocol_error"),
        (200, json.dumps({"value": {}}).encode(), None, "target_protocol_error"),
    ],
    ids=[
        "w3c-ok-created",
        "json-w3c-error-rejected",
        "transport-unreachable",
        "html-protocol-error",
        "missing-session-id-protocol-error",
    ],
)
async def test_create_outcome_truth_table_records_attempt_metric(
    db_factory: async_sessionmaker[AsyncSession],
    allocation_service: AllocationService,
    claimed_allocation: AllocationResult,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
    transport_error: str | None,
    expected_kind: str,
) -> None:
    monkeypatch.setattr(
        session_create.appium_direct,
        "create_session_raw",
        _raw_result(status=status, body=body, transport_error=transport_error),
    )

    async def fake_list(target: str, *, timeout: float = 10.0) -> list[str] | None:
        return []

    async def fake_kill(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        return True

    monkeypatch.setattr(session_create.appium_direct, "list_sessions", fake_list)
    monkeypatch.setattr(session_create.appium_direct, "terminate_session", fake_kill)
    before = _attempt_metric_value(expected_kind)
    outcome = await session_create.create_and_promote(
        db_factory, allocation_service, allocation=claimed_allocation, raw_body=b"{}", claim_window_sec=120
    )
    assert outcome.kind == expected_kind
    assert _attempt_metric_value(expected_kind) == before + 1


@pytest.mark.db
async def test_allocation_not_pending_rolls_back_created_session_as_promotion_failed(
    db_factory: async_sessionmaker[AsyncSession],
    allocation_service: AllocationService,
    claimed_allocation: AllocationResult,
    monkeypatch: pytest.MonkeyPatch,
    tracker: _TxTracker,
) -> None:
    factory = _tracking_factory(db_factory, tracker)
    monkeypatch.setattr(session_create.appium_direct, "create_session_raw", _asserting_create_raw(_ok_raw(), tracker))
    killed: list[str] = []

    async def fake_kill(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        assert tracker.active == 0, "Appium terminate issued with an open transaction"
        killed.append(session_id)
        return True

    async def fail_promote(
        db: AsyncSession,
        *,
        allocation_id: uuid.UUID,
        appium_session_id: str,
        appium_capabilities: dict[str, Any] | None = None,
    ) -> None:
        raise session_create.AllocationNotPendingError(allocation_id)

    monkeypatch.setattr(session_create.appium_direct, "terminate_session", fake_kill)
    monkeypatch.setattr(allocation_service, "promote_to_running", fail_promote)
    before = _attempt_metric_value("promotion_failed")
    outcome = await session_create.create_and_promote(
        factory, allocation_service, allocation=claimed_allocation, raw_body=b"{}", claim_window_sec=120
    )
    assert outcome.kind == "promotion_failed"
    assert killed == ["app-1"]
    assert _attempt_metric_value("promotion_failed") == before + 1


@pytest.mark.db
async def test_create_and_promote_caps_upstream_timeout_when_requested(
    db_factory: async_sessionmaker[AsyncSession],
    allocation_service: AllocationService,
    claimed_allocation: AllocationResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_timeouts: list[float] = []

    async def fake_create(target: str, raw: bytes, *, timeout: float) -> tuple[int, bytes, str | None]:
        seen_timeouts.append(timeout)
        return 200, json.dumps(W3C_OK).encode(), None

    monkeypatch.setattr(session_create.appium_direct, "create_session_raw", fake_create)
    outcome = await session_create.create_and_promote(
        db_factory,
        allocation_service,
        allocation=claimed_allocation,
        raw_body=b"{}",
        claim_window_sec=120,
        max_create_timeout_sec=10.0,
    )
    assert outcome.kind == "created"
    assert seen_timeouts == [10.0]


def test_effective_create_timeout_derivation() -> None:
    assert session_create.effective_create_timeout(120) == 115.0
    assert session_create.effective_create_timeout(600) == 240.0
    assert session_create.effective_create_timeout(30) == 25.0
