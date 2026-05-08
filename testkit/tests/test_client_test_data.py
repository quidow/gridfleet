from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

from gridfleet_testkit.client import GridFleetClient


class _DummyResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


def test_get_device_test_data_calls_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> _DummyResponse:
        captured["url"] = url
        return _DummyResponse({"k": "v"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.get", fake_get)
    client = GridFleetClient(base_url="http://test/api")
    assert client.get_device_test_data("abc") == {"k": "v"}
    assert captured["url"].endswith("/devices/abc/test_data")


def test_replace_device_test_data_uses_put(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_put(url: str, **kwargs: Any) -> _DummyResponse:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _DummyResponse({"k": "v"})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.put", fake_put)
    client = GridFleetClient(base_url="http://test/api")
    assert client.replace_device_test_data("abc", {"k": "v"}) == {"k": "v"}
    assert captured["url"].endswith("/devices/abc/test_data")
    assert captured["json"] == {"k": "v"}


def test_merge_device_test_data_uses_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_patch(url: str, **kwargs: Any) -> _DummyResponse:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _DummyResponse({"a": 1, "b": 2})

    monkeypatch.setattr("gridfleet_testkit.client.httpx.patch", fake_patch)
    client = GridFleetClient(base_url="http://test/api")
    assert client.merge_device_test_data("abc", {"b": 2}) == {"a": 1, "b": 2}
    assert captured["url"].endswith("/devices/abc/test_data")
    assert captured["json"] == {"b": 2}
