import sys

from app.devices.services import verification_execution as _verification_execution
from app.devices.services.verification_execution import *  # noqa: F403

sys.modules[__name__] = _verification_execution
