"""Verification-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.verification.services_container import VerificationServices


def get_verification_services(request: Request) -> VerificationServices:
    return request.app.state.services.verification  # type: ignore[no-any-return]


VerificationServicesDep = Annotated["VerificationServices", Depends(get_verification_services)]
