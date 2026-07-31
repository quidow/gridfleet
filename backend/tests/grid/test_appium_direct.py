from __future__ import annotations

import httpx2 as httpx
import pytest

from app.grid import appium_direct


class _Resp:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> object:
        return self._payload


class _Client:
    def __init__(self, outcome: object) -> None:
        self._outcome = outcome

    async def get(self, url: str, timeout: float) -> _Resp:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        assert isinstance(self._outcome, _Resp)
        return self._outcome


@pytest.mark.asyncio
async def test_list_sessions_reports_transport_error_when_node_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(appium_direct, "_get_client", lambda: _Client(httpx.ConnectError("connection refused")))

    assert await appium_direct.list_sessions("http://node.invalid:4723") == (None, True)


@pytest.mark.asyncio
async def test_list_sessions_refusal_is_not_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node that answers is alive even when it cannot enumerate — a node without
    the ``session_discovery`` insecure feature answers 404 forever. Treating that as
    unreachable would park healthy devices offline."""
    monkeypatch.setattr(appium_direct, "_get_client", lambda: _Client(_Resp(404, {})))

    assert await appium_direct.list_sessions("http://node.invalid:4723") == (None, False)


@pytest.mark.asyncio
async def test_list_sessions_returns_ids_and_no_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        appium_direct, "_get_client", lambda: _Client(_Resp(200, {"value": [{"id": "abc"}, {"id": "def"}]}))
    )

    assert await appium_direct.list_sessions("http://node.invalid:4723") == (["abc", "def"], False)
