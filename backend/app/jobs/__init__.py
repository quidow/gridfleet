from app.jobs.config import JobsConfig
from app.jobs.kinds import JOB_KIND_DEVICE_RECOVERY, JOB_KIND_DEVICE_VERIFICATION
from app.jobs.statuses import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_PENDING, JOB_STATUS_RUNNING

jobs_settings = JobsConfig()

__all__ = [
    "JOB_KIND_DEVICE_RECOVERY",
    "JOB_KIND_DEVICE_VERIFICATION",
    "JOB_STATUS_COMPLETED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_PENDING",
    "JOB_STATUS_RUNNING",
    "JobsConfig",
    "jobs_settings",
]
