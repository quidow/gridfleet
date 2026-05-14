import sys

from app.devices.services import recovery_job as _recovery_job
from app.devices.services.recovery_job import *  # noqa: F403

sys.modules[__name__] = _recovery_job
