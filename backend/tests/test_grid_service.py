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

    async def __aenter__(self) -> DummyClient:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
        return False

    async def _get(self, url: str, *, timeout: int) -> httpx.Response:
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


async def test_get_grid_status_success(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(200, request=httpx.Request("GET", "http://grid/status"), json={"ready": True})
    monkeypatch.setattr("app.services.grid_service.settings_service.get", lambda key: "http://grid")
    monkeypatch.setattr("app.services.grid_service.httpx.AsyncClient", lambda: DummyClient(response=response))

    result = await grid_service.get_grid_status()

    assert result == {"ready": True}


async def test_get_grid_status_returns_error_payload_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("GET", "http://grid/status")
    monkeypatch.setattr("app.services.grid_service.settings_service.get", lambda key: "http://grid")
    monkeypatch.setattr(
        "app.services.grid_service.httpx.AsyncClient",
        lambda: DummyClient(error=httpx.ConnectError("boom", request=request)),
    )

    result = await grid_service.get_grid_status()

    assert result["ready"] is False
    assert "boom" in result["error"]
