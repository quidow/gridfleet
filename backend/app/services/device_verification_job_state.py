import sys

from app.devices.services import verification_job_state as _verification_job_state
from app.devices.services.verification_job_state import *  # noqa: F403

sys.modules[__name__] = _verification_job_state
