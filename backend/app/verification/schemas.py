"""Verification domain schemas."""

import uuid
from typing import Literal

from pydantic import BaseModel

VerificationJobStatus = Literal["pending", "running", "completed", "failed"]
VerificationStageStatus = Literal["pending", "running", "failed", "passed", "skipped"]


class DeviceVerificationJobRead(BaseModel):
    job_id: str
    status: VerificationJobStatus
    current_stage: str | None = None
    current_stage_status: VerificationStageStatus | None = None
    detail: str | None = None
    error: str | None = None
    device_id: uuid.UUID | None = None
    started_at: str
    finished_at: str | None = None
