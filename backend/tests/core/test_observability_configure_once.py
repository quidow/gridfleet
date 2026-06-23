"""Repeated get_logger() calls must not re-run the expensive logging setup."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from app.core import observability

if TYPE_CHECKING:
    import pytest


def test_get_logger_does_not_reconfigure_once_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Start from a configured state. get_logger() calls configure_logging() on
    # every call now; the early-return guard must short-circuit the expensive
    # reconfigure (structlog.configure / handler churn) once already set up.
    observability.configure_logging(force=True)
    call_count = {"n": 0}
    real_configure = structlog.configure

    def counting(*args: object, **kwargs: object) -> None:
        call_count["n"] += 1
        real_configure(*args, **kwargs)

    monkeypatch.setattr(structlog, "configure", counting)

    for i in range(20):
        observability.get_logger(f"tests.observability.guard.{i}")

    assert call_count["n"] == 0, (
        f"get_logger should not re-run structlog.configure once configured; got {call_count['n']} calls"
    )
