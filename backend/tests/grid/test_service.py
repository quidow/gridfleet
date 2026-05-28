from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx

if TYPE_CHECKING:
    import pytest

from app.grid.protocols import GridServiceProtocol
from app.grid.service import GridService
from tests.fakes.settings import FakeSettingsReader


class DummyClient:
    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.get = AsyncMock(side_effect=self._get)
        self.delete = AsyncMock(side_effect=self._delete)

    @property
    def is_closed(self) -> bool:
        return False

    async def _get(self, url: str, *, timeout: int) -> httpx.Response:
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response

    async def _delete(self, url: str, *, timeout: int) -> httpx.Response:
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _make_service(settings_overrides: dict[str, str] | None = None) -> GridService:
    return GridService(settings=FakeSettingsReader(settings_overrides or {}))


def test_grid_service_satisfies_protocol() -> None:
    assert issubclass(GridService, GridServiceProtocol)


async def test_get_status_success(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(200, request=httpx.Request("GET", "http://grid/status"), json={"ready": True})
    svc = _make_service({"grid.hub_url": "http://grid"})
    svc._client = DummyClient(response=response)  # type: ignore[assignment]

    result = await svc.get_status()

    assert result == {"ready": True}


async def test_get_status_returns_error_payload_on_http_error() -> None:
    request = httpx.Request("GET", "http://grid/status")
    svc = _make_service({"grid.hub_url": "http://grid"})
    svc._client = DummyClient(error=httpx.ConnectError("boom", request=request))  # type: ignore[assignment]

    result = await svc.get_status()

    assert result == {"ready": False, "error": "grid_unreachable"}


async def test_terminate_session_success() -> None:
    response = httpx.Response(200, request=httpx.Request("DELETE", "http://grid/session/sid-1"), json={"value": None})
    svc = _make_service({"grid.hub_url": "http://grid"})
    svc._client = DummyClient(response=response)  # type: ignore[assignment]

    assert await svc.terminate_session("sid-1") is True
    svc._client.delete.assert_awaited_once_with("http://grid/session/sid-1", timeout=10)  # type: ignore[union-attr]


async def test_terminate_session_treats_404_as_already_gone() -> None:
    response = httpx.Response(404, request=httpx.Request("DELETE", "http://grid/session/sid-missing"))
    svc = _make_service({"grid.hub_url": "http://grid"})
    svc._client = DummyClient(response=response)  # type: ignore[assignment]

    assert await svc.terminate_session("sid-missing") is True


async def test_terminate_session_returns_false_on_transport_error() -> None:
    request = httpx.Request("DELETE", "http://grid/session/sid-err")
    svc = _make_service({"grid.hub_url": "http://grid"})
    svc._client = DummyClient(error=httpx.ConnectError("down", request=request))  # type: ignore[assignment]

    assert await svc.terminate_session("sid-err") is False
