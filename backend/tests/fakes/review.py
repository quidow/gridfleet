"""Helper constructing a real ReviewService for tests.

``ReviewService`` is a thin stateless wrapper around the ``review_required``
shelving flag; tests that only need a constructed dependency use this factory
rather than instantiating it by hand.
"""

from __future__ import annotations

from app.devices.services.review import ReviewService


def build_review_service() -> ReviewService:
    return ReviewService()
