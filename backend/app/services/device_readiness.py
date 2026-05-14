import sys

from app.devices.services import readiness as _readiness
from app.devices.services.readiness import *  # noqa: F403

sys.modules[__name__] = _readiness
