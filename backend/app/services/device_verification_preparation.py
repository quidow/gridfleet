import sys

from app.devices.services import verification_preparation as _verification_preparation
from app.devices.services.verification_preparation import *  # noqa: F403

sys.modules[__name__] = _verification_preparation
