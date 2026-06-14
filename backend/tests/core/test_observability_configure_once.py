"""configure_logging() must run at most once across repeated get_logger() calls."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core import observability

if TYPE_CHECKING:
    import pytest


def test_configure_logging_called_at_most_once_across_many_get_logger_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reset the module-level guard so this test starts from a clean state
    observability.configure_logging(force=True)
    call_count = {"n": 0}
    original = observability.configure_logging

    def counting(*args: object, **kwargs: object) -> None:
        call_count["n"] += 1
        original(*args, **kwargs)

    monkeypatch.setattr(observability, "configure_logging", counting)

    for i in range(20):
        observability.get_logger(f"tests.observability.guard.{i}")

    assert call_count["n"] == 0, (
        f"get_logger should not re-trigger configure_logging once configured; got {call_count['n']} calls"
    )
