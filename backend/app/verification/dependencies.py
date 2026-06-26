"""Verification-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.verification.services_container import VerificationServices

get_verification_services = make_services_getter("verification")
VerificationServicesDep = Annotated["VerificationServices", Depends(get_verification_services)]
