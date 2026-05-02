from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_app import cli

if TYPE_CHECKING:
    import pytest


def test_serve_runs_uvicorn_with_default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    def fake_run(app: str, *, host: str, port: int) -> None:
        recorded.update({"app": app, "host": host, "port": port})

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    assert cli.main(["serve"]) == 0
    assert recorded == {"app": "agent_app.main:app", "host": "0.0.0.0", "port": 5100}


def test_serve_allows_host_and_port_override(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    def fake_run(app: str, *, host: str, port: int) -> None:
        recorded.update({"app": app, "host": host, "port": port})

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    assert cli.main(["serve", "--host", "127.0.0.1", "--port", "5200"]) == 0
    assert recorded == {"app": "agent_app.main:app", "host": "127.0.0.1", "port": 5200}


def test_version_prints_public_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0

    assert capsys.readouterr().out == f"gridfleet-agent {cli.__version__}\n"
