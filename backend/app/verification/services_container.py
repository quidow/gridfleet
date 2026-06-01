"""Verification domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.jobs.protocols import VerificationJobRunner
    from app.verification.protocols import VerificationProtocol


@dataclass(frozen=True, slots=True)
class VerificationServices:
    service: VerificationProtocol
    runner: VerificationJobRunner
