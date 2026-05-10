from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx

if TYPE_CHECKING:
    import pytest

from app.services import grid_service


class DummyClient:
    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.get = AsyncMock(side_effect=self._get)
        self.delete = AsyncMock(side_effect=self._delete)

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


async def test_get_grid_status_success(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(200, request=httpx.Request("GET", "http://grid/status"), json={"ready": True})
    monkeypatch.setattr("app.services.grid_service.settings_service.get", lambda key: "http://grid")
    dummy = DummyClient(response=response)
    monkeypatch.setattr("app.services.grid_service._get_client", lambda: dummy)

    result = await grid_service.get_grid_status()

    assert result == {"ready": True}


async def test_get_grid_status_returns_error_payload_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("GET", "http://grid/status")
    monkeypatch.setattr("app.services.grid_service.settings_service.get", lambda key: "http://grid")
    dummy = DummyClient(error=httpx.ConnectError("boom", request=request))
    monkeypatch.setattr("app.services.grid_service._get_client", lambda: dummy)

    result = await grid_service.get_grid_status()

    assert result == {"ready": False, "error": "grid_unreachable"}


async def test_terminate_grid_session_success(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(200, request=httpx.Request("DELETE", "http://grid/session/sid-1"), json={"value": None})
    dummy = DummyClient(response=response)
    monkeypatch.setattr("app.services.grid_service.settings_service.get", lambda key: "http://grid")
    monkeypatch.setattr("app.services.grid_service._get_client", lambda: dummy)

    assert await grid_service.terminate_grid_session("sid-1") is True
    dummy.delete.assert_awaited_once_with("http://grid/session/sid-1", timeout=10)


async def test_terminate_grid_session_treats_404_as_already_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(404, request=httpx.Request("DELETE", "http://grid/session/sid-missing"))
    monkeypatch.setattr("app.services.grid_service.settings_service.get", lambda key: "http://grid")
    dummy = DummyClient(response=response)
    monkeypatch.setattr("app.services.grid_service._get_client", lambda: dummy)

    assert await grid_service.terminate_grid_session("sid-missing") is True


async def test_terminate_grid_session_returns_false_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("DELETE", "http://grid/session/sid-err")
    monkeypatch.setattr("app.services.grid_service.settings_service.get", lambda key: "http://grid")
    dummy = DummyClient(error=httpx.ConnectError("down", request=request))
    monkeypatch.setattr("app.services.grid_service._get_client", lambda: dummy)

    assert await grid_service.terminate_grid_session("sid-err") is False
