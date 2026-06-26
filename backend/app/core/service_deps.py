"""Factory for the per-domain `request.app.state.services.<attr>` dependency providers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request  # noqa: TC002  # FastAPI resolves _get's annotation at runtime via get_type_hints()

if TYPE_CHECKING:
    from collections.abc import Callable


def make_services_getter(attr: str) -> Callable[[Request], object]:
    """Build a FastAPI dependency returning `request.app.state.services.<attr>`."""

    def _get(request: Request) -> object:
        return getattr(request.app.state.services, attr)

    return _get
