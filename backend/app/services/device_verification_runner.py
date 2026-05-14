import sys

from app.devices.services import verification_runner as _verification_runner
from app.devices.services.verification_runner import *  # noqa: F403

sys.modules[__name__] = _verification_runner
